from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from h2h_dev_tools import code_review

ROOT = Path(__file__).resolve().parents[1]

FAKE_CODEX = r'''
import json
import os
import subprocess
import sys
import time
from pathlib import Path

if sys.argv[1:] == ["--version"]:
    print("codex-cli test-fixture")
    raise SystemExit(0)

state = Path(os.environ["FAKE_CODEX_STATE"])
with (state / "calls.jsonl").open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({"args": sys.argv[1:], "prompt": sys.stdin.read()}) + "\n")

mode = os.environ.get("FAKE_CODEX_MODE", "pass")
if mode in {"hold-pass", "hold-failure"}:
    (state / "held").write_text("review started", encoding="utf-8")
    while not (state / "release").exists():
        time.sleep(0.01)
    if mode == "hold-failure":
        raise SystemExit(7)
if mode == "nonzero":
    raise SystemExit(7)
if mode == "timeout":
    child_code = """
import fcntl
import os
import sys
import time
from pathlib import Path
state = Path(sys.argv[1])
lock = (state / 'child-lock').open('a', encoding='utf-8')
fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
(state / 'child-pid').write_text(str(os.getpid()), encoding='utf-8')
while True:
    time.sleep(0.1)
"""
    subprocess.Popen([sys.executable, "-c", child_code, str(state)])
    while not (state / "child-pid").exists():
        time.sleep(0.01)
    time.sleep(120)
    raise SystemExit(0)
if mode == "change-tree":
    Path("feature.py").write_text("value = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature.py"], check=True)

output = Path(sys.argv[sys.argv.index("--output-last-message") + 1])
if mode == "missing":
    raise SystemExit(0)
if mode == "malformed":
    output.write_text("not JSON", encoding="utf-8")
    raise SystemExit(0)

result = {"verdict": "pass", "summary": "Reviewed the candidate.", "findings": []}
if mode in {"p0", "p1", "p2", "contradictory"}:
    result["verdict"] = "pass" if mode == "contradictory" else "changes_requested"
    result["findings"] = [{
        "priority": 1 if mode == "contradictory" else int(mode[1]),
        "title": "Changed behavior loses data",
        "body": "The changed path discards a required value.",
        "file": "feature.py",
        "line_start": 1,
        "line_end": 1,
    }]
elif mode == "incomplete":
    result["verdict"] = "incomplete"
elif mode == "empty-summary":
    result["summary"] = "  "
elif mode == "unknown-verdict":
    result["verdict"] = "approved"
elif mode == "missing-findings":
    del result["findings"]
output.write_text(json.dumps(result), encoding="utf-8")
print(json.dumps({"type": "turn.completed"}))
'''


@dataclass(frozen=True)
class ReviewRepository:
    path: Path
    state: Path
    environment: dict[str, str]
    implementation: str

    def git(self, *arguments: str) -> str:
        return subprocess.run(
            ("git", *arguments),
            cwd=self.path,
            env=self.environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout.strip()

    def review(
        self, *arguments: str, mode: str = "pass"
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            (sys.executable, "scripts/review-code.py", *arguments),
            cwd=self.path,
            env={**self.environment, "FAKE_CODEX_MODE": mode},
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )

    def calls(self) -> list[dict[str, object]]:
        path = self.state / "calls.jsonl"
        if not path.exists():
            return []
        return [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
        ]

    def commit_file(self, name: str, content: str) -> str:
        (self.path / name).write_text(content, encoding="utf-8")
        self.git("add", name)
        self.git("commit", "-m", f"test: update {name}")
        return self.git("rev-parse", "HEAD")


def _make_review_repository(tmp_path: Path, layout: str = "source") -> ReviewRepository:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_ALLOW_PROTOCOL="file",
        GIT_TERMINAL_PROMPT="0",
        GIT_EDITOR="true",
    )
    local = tmp_path / "repository"
    local.mkdir()
    state = tmp_path / "fake-codex-state"
    state.mkdir()
    executable_directory = tmp_path / "bin"
    executable_directory.mkdir()
    executable = executable_directory / "codex"
    executable.write_text(f"#!{sys.executable}\n{FAKE_CODEX}", encoding="utf-8")
    executable.chmod(0o755)
    environment["PATH"] = (
        str(executable_directory) + os.pathsep + environment.get("PATH", "")
    )
    environment["FAKE_CODEX_STATE"] = str(state)
    package_parent = "src" if layout == "source" else "scripts/_shared"
    implementation = f"{package_parent}/h2h_dev_tools/code_review.py"
    repo = ReviewRepository(local, state, environment, implementation)
    repo.git("init", "--initial-branch", "main")
    repo.git("config", "user.name", "Review Test")
    repo.git("config", "user.email", "review@example.invalid")
    repo.git("config", "workflow.primaryBranch", "main")
    package = local / package_parent / "h2h_dev_tools"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    shutil.copy2(ROOT / "src/h2h_dev_tools/code_review.py", package / "code_review.py")
    (local / "scripts").mkdir(exist_ok=True)
    (local / "scripts/review-code.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "sys.dont_write_bytecode = True\n"
        "root = Path(__file__).resolve().parents[1]\n"
        f"sys.path.insert(0, str(root / {package_parent!r}))\n"
        "from h2h_dev_tools.code_review import main\n"
        "main(root)\n",
        encoding="utf-8",
    )
    (local / "AGENTS.md").write_text(
        "# Review policy\n\nReport correctness defects.\n", encoding="utf-8"
    )
    repo.git("add", ".")
    repo.git("commit", "-m", "test: initialize review fixture")
    repo.git("switch", "-c", "task/example")
    repo.commit_file("feature.py", "value = 1\n")
    repo.git("switch", "main")
    repo.commit_file("primary.txt", "Concurrent primary work.\n")
    repo.git("merge", "--no-ff", "--no-commit", "task/example")
    return repo


@pytest.fixture(params=["source", "snapshot"])
def review_repository(
    tmp_path: Path, request: pytest.FixtureRequest
) -> ReviewRepository:
    if os.name != "posix":
        pytest.skip("The fake Codex executable and process-group oracle require POSIX")
    return _make_review_repository(tmp_path, str(request.param))


def _assert_passed(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, result.stdout + result.stderr


def _assert_blocked(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode != 0, result.stdout + result.stderr


@contextmanager
def _held_review(repo: ReviewRepository, mode: str) -> Iterator[subprocess.Popen[str]]:
    process = subprocess.Popen(
        (sys.executable, "scripts/review-code.py", "run", "--index"),
        cwd=repo.path,
        env={**repo.environment, "FAKE_CODEX_MODE": mode},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not (repo.state / "held").exists() and time.monotonic() < deadline:
            assert process.poll() is None, "The review exited before synchronization"
            time.sleep(0.01)
        assert (repo.state / "held").exists(), "The fake reviewer never started"
        yield process
    finally:
        (repo.state / "release").write_text("release review", encoding="utf-8")
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.communicate(timeout=5)


def test_review_binds_the_real_merge_candidate_and_verifies_after_commit(
    review_repository: ReviewRepository,
) -> None:
    repo = review_repository
    base = repo.git("rev-parse", "HEAD")
    tree = repo.git("write-tree")
    _assert_passed(repo.review("run", "--index", "--model", "fixture-model"))
    calls = repo.calls()
    assert len(calls) == 1
    arguments = calls[0]["args"]
    assert isinstance(arguments, list)
    assert arguments[0] == "exec"
    assert arguments[arguments.index("--sandbox") + 1] == "read-only"
    assert arguments[arguments.index("--model") + 1] == "fixture-model"
    assert {
        "--json",
        "--output-schema",
        "--output-last-message",
        "--ephemeral",
        "-",
    } <= set(arguments)
    prompt = str(calls[0]["prompt"])
    assert base in prompt
    assert tree in prompt
    _assert_passed(repo.review("verify", "--index", mode="nonzero"))
    repo.git("commit", "-m", "Merge task/example")
    _assert_passed(repo.review("verify", "--revision", "HEAD", mode="nonzero"))
    assert repo.calls() == calls, "Offline verification must never invoke Codex"
    assert repo.git("status", "--porcelain") == ""


@pytest.mark.parametrize(
    "mode",
    [
        "p0",
        "p1",
        "p2",
        "contradictory",
        "incomplete",
        "malformed",
        "missing",
        "nonzero",
        "empty-summary",
        "unknown-verdict",
        "missing-findings",
    ],
)
def test_review_fails_closed_without_a_complete_clean_result(
    review_repository: ReviewRepository, mode: str
) -> None:
    repo = review_repository
    _assert_blocked(repo.review("run", "--index", mode=mode))
    _assert_blocked(repo.review("verify", "--index"))
    assert len(repo.calls()) == 1


@pytest.mark.parametrize("untracked", [False, True])
def test_review_rejects_dirty_worktrees_before_invoking_codex(
    review_repository: ReviewRepository, untracked: bool
) -> None:
    repo = review_repository
    name = "untracked.txt" if untracked else "primary.txt"
    (repo.path / name).write_text("Unreviewed local changes.\n", encoding="utf-8")
    _assert_blocked(repo.review("run", "--index"))
    assert repo.calls() == []


@pytest.mark.parametrize(
    "name",
    ["feature.py", "AGENTS.md", "scripts/review-code.py", "shared-implementation"],
)
def test_review_receipt_does_not_cover_changed_code_policy_or_runner(
    review_repository: ReviewRepository, name: str
) -> None:
    repo = review_repository
    _assert_passed(repo.review("run", "--index"))
    name = repo.implementation if name == "shared-implementation" else name
    path = repo.path / name
    with path.open("a", encoding="utf-8") as stream:
        stream.write("\n# Candidate changed after review.\n")
    repo.git("add", name)
    _assert_blocked(repo.review("verify", "--index"))
    assert len(repo.calls()) == 1


def test_review_receipt_does_not_cover_a_new_base_with_the_same_tree(
    review_repository: ReviewRepository,
) -> None:
    repo = review_repository
    _assert_passed(repo.review("run", "--index"))
    candidate_tree = repo.git("write-tree")
    old_base = repo.git("rev-parse", "HEAD")
    replacement = repo.git(
        "commit-tree",
        "HEAD^{tree}",
        "-p",
        old_base,
        "-m",
        "test: advance primary metadata",
    )
    repo.git("update-ref", "refs/heads/main", replacement, old_base)
    assert repo.git("write-tree") == candidate_tree
    _assert_blocked(repo.review("verify", "--index"))
    assert len(repo.calls()) == 1


def test_review_index_requires_a_pending_merge(
    review_repository: ReviewRepository,
) -> None:
    repo = review_repository
    repo.git("merge", "--abort")
    _assert_blocked(repo.review("run", "--index"))
    assert repo.calls() == []


def test_explicit_failed_rerun_invalidates_the_previous_pass(
    review_repository: ReviewRepository,
) -> None:
    repo = review_repository
    _assert_passed(repo.review("run", "--index"))
    _assert_blocked(repo.review("run", "--index", mode="p1"))
    _assert_blocked(repo.review("verify", "--index"))
    assert len(repo.calls()) == 2


@pytest.mark.parametrize("index", [False, True])
def test_review_verification_rejects_a_different_expected_tree(
    review_repository: ReviewRepository, index: bool
) -> None:
    repo = review_repository
    candidate_tree = repo.git("write-tree")
    other_tree = repo.git("rev-parse", "HEAD^{tree}")
    assert candidate_tree != other_tree
    _assert_passed(repo.review("run", "--index"))
    if not index:
        repo.git("commit", "-m", "Merge task/example")
    scope = ("--index",) if index else ("--revision", "HEAD")
    _assert_passed(repo.review("verify", *scope, "--expected-tree", candidate_tree))
    _assert_blocked(repo.review("verify", *scope, "--expected-tree", other_tree))
    _assert_passed(repo.review("verify", *scope, "--expected-tree", candidate_tree))
    assert len(repo.calls()) == 1


@pytest.mark.parametrize("failure", ["dirty-worktree", "invalid-timeout"])
def test_failed_review_preflight_invalidates_the_previous_pass(
    review_repository: ReviewRepository, failure: str
) -> None:
    repo = review_repository
    _assert_passed(repo.review("run", "--index"))
    _assert_passed(repo.review("verify", "--index"))
    path = repo.path / "primary.txt"
    original = path.read_bytes()
    arguments = ("--timeout-seconds", "0") if failure == "invalid-timeout" else ()
    if failure == "dirty-worktree":
        path.write_text("Unstaged change blocks review.\n", encoding="utf-8")
    _assert_blocked(repo.review("run", "--index", *arguments))
    path.write_bytes(original)
    assert repo.git("diff", "--name-only") == ""
    _assert_blocked(repo.review("verify", "--index"))
    assert len(repo.calls()) == 1, "Preflight failures must occur before invoking Codex"


@pytest.mark.parametrize("mode", ["hold-pass", "hold-failure"])
def test_concurrent_review_rejects_a_second_owner_without_invoking_codex(
    review_repository: ReviewRepository, mode: str
) -> None:
    repo = review_repository
    _assert_passed(repo.review("run", "--index"))
    with _held_review(repo, mode) as owner:
        _assert_blocked(repo.review("verify", "--index"))
        started = time.monotonic()
        competing = repo.review("run", "--index")
        assert time.monotonic() - started < 5, (
            "A competing run must not wait for its owner"
        )
        _assert_blocked(competing)
        assert "already running" in (competing.stdout + competing.stderr).lower()
        assert len(repo.calls()) == 2, "A competing run must not invoke Codex"
        _assert_blocked(repo.review("verify", "--index"))
        (repo.state / "release").write_text("release review", encoding="utf-8")
        stdout, stderr = owner.communicate(timeout=5)
        if mode == "hold-pass":
            assert owner.returncode == 0, stdout + stderr
            _assert_passed(repo.review("verify", "--index"))
        else:
            assert owner.returncode != 0, stdout + stderr
            _assert_blocked(repo.review("verify", "--index"))
    _assert_passed(repo.review("run", "--index"))
    _assert_passed(repo.review("verify", "--index"))
    assert len(repo.calls()) == 3


def test_terminated_review_releases_its_lock_without_a_passing_receipt(
    review_repository: ReviewRepository,
) -> None:
    repo = review_repository
    _assert_passed(repo.review("run", "--index"))
    with _held_review(repo, "hold-pass") as owner:
        _assert_blocked(repo.review("verify", "--index"))
        owner.terminate()
        stdout, stderr = owner.communicate(timeout=5)
        assert owner.returncode != 0, stdout + stderr
        _assert_blocked(repo.review("verify", "--index"))
    _assert_passed(repo.review("run", "--index"))
    _assert_passed(repo.review("verify", "--index"))
    assert len(repo.calls()) == 3


def test_candidate_changes_during_review_cannot_receive_a_passing_receipt(
    review_repository: ReviewRepository,
) -> None:
    repo = review_repository
    _assert_blocked(repo.review("run", "--index", mode="change-tree"))
    _assert_blocked(repo.review("verify", "--index"))
    assert len(repo.calls()) == 1


def test_review_can_explicitly_cover_an_existing_commit(
    review_repository: ReviewRepository,
) -> None:
    repo = review_repository
    repo.git("merge", "--abort")
    _assert_passed(repo.review("run"))
    _assert_passed(repo.review("verify"))
    assert len(repo.calls()) == 1


def _child_released_lock(state: Path) -> bool:
    import fcntl

    # The fake descendant keeps this descriptor locked for its entire lifetime.
    # Process exit releases the lock even before an orphaned zombie is reaped.
    with (state / "child-lock").open("a", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
    return True


def test_review_timeout_terminates_the_codex_descendant(
    review_repository: ReviewRepository,
) -> None:
    repo = review_repository
    child_pid: int | None = None
    try:
        started = time.monotonic()
        result = repo.review("run", "--index", "--timeout-seconds", "1", mode="timeout")
        elapsed = time.monotonic() - started
        pid_path = repo.state / "child-pid"
        assert pid_path.exists(), "The timeout oracle must exercise a real descendant"
        child_pid = int(pid_path.read_text(encoding="utf-8"))
        _assert_blocked(result)
        assert elapsed < 10, result.stdout + result.stderr
        deadline = time.monotonic() + 2
        while not _child_released_lock(repo.state) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert _child_released_lock(repo.state), "The Codex descendant survived timeout"
        _assert_blocked(repo.review("verify", "--index"))
    finally:
        if child_pid is not None and not _child_released_lock(repo.state):
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_review_receipt_binds_executed_shared_bytes_even_for_unchanged_revision(
    review_repository: ReviewRepository,
) -> None:
    repo = review_repository
    _assert_passed(repo.review("run", "--index"))
    repo.git("commit", "-m", "Merge task/example")
    revision = repo.git("rev-parse", "HEAD")
    implementation = repo.path / repo.implementation
    with implementation.open("a", encoding="utf-8") as stream:
        stream.write("\n# Executed implementation changed without staging.\n")
    _assert_blocked(repo.review("verify", "--revision", revision))
    assert len(repo.calls()) == 1


def test_reviewer_instances_keep_roots_and_evidence_separate(tmp_path: Path) -> None:
    roots = [tmp_path / "first", tmp_path / "second"]
    for root in roots:
        root.mkdir()
    first_repo = _make_review_repository(roots[0])
    second_repo = _make_review_repository(roots[1], "snapshot")
    first = code_review.Reviewer(first_repo.path)
    second = code_review.Reviewer(second_repo.path)
    first_target = first._target(index=True)
    second_target = second._target(index=True)
    assert first._target(index=True) == first_target
    assert first.repository != second.repository
    assert first._directory() == first_repo.path / ".git/h2h-review"
    assert second._directory() == second_repo.path / ".git/h2h-review"
    result = {"verdict": "pass", "summary": "Fixture review.", "findings": []}
    first._write_receipt(first_target, result, "fixture", None, first_repo.state)
    assert first._verify(first_target).exists()
    with pytest.raises(code_review.ReviewError, match="No valid code review"):
        second._verify(second_target)


def test_windows_offline_verification_does_not_require_codex_or_posix_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_review_repository(tmp_path)
    reviewer = code_review.Reviewer(repo.path)
    target = reviewer._target(index=True)
    result = {"verdict": "pass", "summary": "Fixture review.", "findings": []}
    reviewer._write_receipt(target, result, "fixture", None, repo.state)
    monkeypatch.setattr(code_review, "os", SimpleNamespace(name="nt"))
    with reviewer._review_lock(target, exclusive=False):
        assert reviewer._verify(target).exists()
    with pytest.raises(code_review.ReviewError, match="requires POSIX"):
        with reviewer._review_lock(target, exclusive=True):
            pytest.fail("Windows must reject online review before starting Codex")
    assert repo.calls() == []


@pytest.mark.parametrize(
    ("scope", "classifier_status", "review_status", "expected_success", "reviewed"),
    [
        ("documentation", 0, 0, True, False),
        ("full", 0, 0, True, True),
        ("full", 0, 7, False, True),
        ("documentation", 3, 0, False, False),
        ("unknown", 0, 0, False, False),
        ("", 0, 0, False, False),
    ],
)
def test_merge_review_routes_only_a_valid_exact_candidate_profile(
    tmp_path: Path,
    scope: str,
    classifier_status: int,
    review_status: int,
    expected_success: bool,
    reviewed: bool,
) -> None:
    if os.name != "posix":
        pytest.skip("Repository shell entry points require a POSIX shell")
    repository = tmp_path / "repository"
    scripts = repository / "scripts"
    scripts.mkdir(parents=True)
    interpreter = repository / ".venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(sys.executable)
    (scripts / "check_change_scope.py").write_text(
        "import sys\n"
        "assert sys.argv[1:] == ['--index', '--base', 'HEAD']\n"
        f"print({scope!r})\n"
        f"raise SystemExit({classifier_status})\n",
        encoding="utf-8",
    )
    (scripts / "review-code.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "assert sys.argv[1:] == ['run', '--index']\n"
        "Path('review-called').touch()\n"
        f"raise SystemExit({review_status})\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        (
            "bash",
            str(ROOT / "src/h2h_dev_tools/review-merge-candidate.sh"),
            str(repository),
        ),
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert (result.returncode == 0) == expected_success, result.stderr
    assert (repository / "review-called").exists() == reviewed
