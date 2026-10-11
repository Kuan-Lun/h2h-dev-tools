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
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from typing import Any

import pytest

from h2h_dev_tools import bundle_verify, code_review, merge_command

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
if mode in {"timeout", "passing-child"}:
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
    if mode == "timeout":
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
    for name in code_review.REVIEW_FILES:
        shutil.copy2(ROOT / "src/h2h_dev_tools" / name, package / name)
    (local / "scripts").mkdir(exist_ok=True)
    shutil.copy2(
        ROOT / "scripts/review-merge-candidate.sh",
        local / "scripts/review-merge-candidate.sh",
    )
    (local / "scripts/review-code.py").write_text(
        "import runpy, sys\n"
        "from pathlib import Path\n"
        "sys.dont_write_bytecode = True\n"
        "root = Path(__file__).resolve().parents[1]\n"
        f"package = root / {package_parent!r} / 'h2h_dev_tools'\n"
        "runpy.run_path(str(package / 'source_loader.py'))['load_package'](package, 'h2h_dev_tools')\n"
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
    [
        "feature.py",
        "AGENTS.md",
        "scripts/review-code.py",
        "shared-implementation",
        "process-implementation",
        "classifier-implementation",
        "shared-shell",
        "scripts/review-merge-candidate.sh",
    ],
)
def test_review_receipt_does_not_cover_changed_code_policy_or_runner(
    review_repository: ReviewRepository, name: str
) -> None:
    repo = review_repository
    _assert_passed(repo.review("run", "--index"))
    if name == "shared-implementation":
        name = repo.implementation
    elif name == "process-implementation":
        name = str(Path(repo.implementation).with_name("process.py"))
    elif name == "classifier-implementation":
        name = str(Path(repo.implementation).with_name("change_scope.py"))
    elif name == "shared-shell":
        name = str(Path(repo.implementation).with_name("review-merge-candidate.sh"))
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


def test_standalone_success_cleans_a_descendant_before_publishing_evidence(
    review_repository: ReviewRepository,
) -> None:
    repo = review_repository
    try:
        _assert_passed(repo.review("run", "--index", mode="passing-child"))
        assert (repo.state / "child-pid").exists()
        assert _child_released_lock(repo.state)
        _assert_passed(repo.review("verify", "--index"))
    finally:
        pid_path = repo.state / "child-pid"
        if pid_path.exists() and not _child_released_lock(repo.state):
            try:
                os.kill(int(pid_path.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_standalone_review_cannot_publish_when_group_cleanup_is_unproven(
    review_repository: ReviewRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = review_repository
    reviewer = code_review.Reviewer(repo.path)
    target = reviewer._target(index=True)
    original_popen = subprocess.Popen
    original_killpg = os.killpg
    review_processes: list[subprocess.Popen[bytes]] = []

    def launch(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        child = original_popen(*args, **kwargs)
        if list(args[0][:2]) == ["codex", "exec"]:
            review_processes.append(child)
        return child

    def group_signal(group: int, signum: int) -> None:
        if review_processes and group == review_processes[0].pid:
            # Model an exited leader whose descendant stays in uninterruptible
            # I/O: signals succeed, but the OS still reports a nonempty group.
            return
        original_killpg(group, signum)

    monkeypatch.setenv("PATH", repo.environment["PATH"])
    monkeypatch.setenv("FAKE_CODEX_STATE", str(repo.state))
    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr(os, "killpg", group_signal)
    started = time.monotonic()
    with pytest.raises(code_review.ReviewError, match=r"cleanup.*proven"):
        reviewer._run(target, index=True, revision="HEAD", timeout=10, model=None)
    assert time.monotonic() - started < 8
    assert len(review_processes) == 1
    assert review_processes[0].poll() == 0
    assert not reviewer._receipt_path(target).exists()
    with pytest.raises(code_review.ReviewError, match="No valid code review"):
        reviewer._verify(target)


@pytest.mark.parametrize("implementation_name", code_review.REVIEW_FILES)
def test_review_receipt_binds_executed_shared_bytes_even_for_unchanged_revision(
    review_repository: ReviewRepository, implementation_name: str
) -> None:
    repo = review_repository
    _assert_passed(repo.review("run", "--index"))
    repo.git("commit", "-m", "Merge task/example")
    revision = repo.git("rev-parse", "HEAD")
    implementation = (repo.path / repo.implementation).with_name(implementation_name)
    with implementation.open("a", encoding="utf-8") as stream:
        stream.write("\n# Executed implementation changed without staging.\n")
    _assert_blocked(repo.review("verify", "--revision", revision))
    assert len(repo.calls()) == 1


def test_review_closure_changes_follow_the_exported_component_contract(
    review_repository: ReviewRepository,
) -> None:
    repo = review_repository
    package = (repo.path / repo.implementation).parent
    verifier = package / "bundle_verify.py"
    original = verifier.read_text(encoding="utf-8")
    extended = original.replace(
        '"code_review.py",', '"review_extra.py", "code_review.py",', 1
    )
    assert extended != original
    verifier.write_text(extended, encoding="utf-8")
    dependency = package / "review_extra.py"
    dependency.write_text("value = 1\n", encoding="utf-8")
    repo.git("add", str(package.relative_to(repo.path)))
    _assert_passed(repo.review("run", "--index"))
    _assert_passed(repo.review("verify", "--index"))
    repo.git("commit", "-m", "Merge component closure fixture")
    _assert_passed(repo.review("verify"))
    # Verify the reviewed candidate without the index workspace guard: the
    # dependency fingerprint itself must reject changed executed source bytes.
    dependency.write_text("value = 2\n", encoding="utf-8")
    _assert_blocked(repo.review("verify"))
    assert len(repo.calls()) == 1


def test_review_fingerprints_the_loaded_bundle_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_review_repository(tmp_path)
    reviewer = code_review.Reviewer(repo.path)
    tree = repo.git("write-tree")
    original = reviewer._policy_digest(tree)
    loaded = tmp_path / "loaded_bundle_verify.py"
    loaded.write_bytes(Path(bundle_verify.__file__).read_bytes())
    monkeypatch.setattr(bundle_verify, "__file__", str(loaded))
    assert reviewer._policy_digest(tree) == original
    with loaded.open("a", encoding="utf-8") as stream:
        stream.write("\n# Loaded dependency differs from the adjacent source file.\n")
    assert reviewer._policy_digest(tree) != original


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
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        "expected = os.environ.get('H2H_CODE_REVIEW_TIMEOUT_SECONDS', '900')\n"
        "assert sys.argv[1:] == ['run', '--index', '--timeout-seconds', expected]\n"
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


REVIEW_PROCESS_TREE = r"""
import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path

state = Path(sys.argv[1])
leader_lock = (state / 'leader-lock').open('a')
fcntl.flock(leader_lock, fcntl.LOCK_EX)
child_code = '''
import fcntl
import os
import sys
import time
from pathlib import Path
state = Path(sys.argv[1])
lock = (state / 'child-lock').open('a')
fcntl.flock(lock, fcntl.LOCK_EX)
(state / 'child-pid').write_text(str(os.getpid()))
while True:
    time.sleep(0.1)
'''
subprocess.Popen([sys.executable, '-c', child_code, str(state)])
while not (state / 'child-pid').exists():
    time.sleep(0.01)
(state / 'tree-ready').touch()
while True:
    time.sleep(0.1)
"""


@pytest.mark.parametrize("signal_name", ["SIGINT", "SIGTERM", "SIGHUP"])
@pytest.mark.parametrize("interrupt_handoff", [True, False])
def test_review_signals_cannot_interrupt_spawn_ownership_or_tree_cleanup(
    review_repository: ReviewRepository,
    monkeypatch: pytest.MonkeyPatch,
    signal_name: str,
    interrupt_handoff: bool,
) -> None:
    repo = review_repository
    reviewer = code_review.Reviewer(repo.path)
    target = reviewer._target(index=True)
    signal_number = int(getattr(signal, signal_name))
    previous_handler = signal.getsignal(signal_number)
    original_popen = subprocess.Popen
    original_stop = code_review._stop
    owned: list[subprocess.Popen[bytes]] = []
    cleanup_started: list[int] = []

    def unprotected_signal(_signal: int, _frame: object) -> None:
        raise AssertionError("Reviewer did not install its signal controller")

    def launch_then_signal(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        command = args[0]
        if list(command[:2]) != ["codex", "exec"]:
            return original_popen(*args, **kwargs)
        child = original_popen(
            (sys.executable, "-c", REVIEW_PROCESS_TREE, str(repo.state)), **kwargs
        )
        owned.append(child)
        deadline = time.monotonic() + 5
        while not (repo.state / "tree-ready").exists():
            assert child.poll() is None, "The process-tree fixture exited too early"
            assert time.monotonic() < deadline, "The descendant never became ready"
            time.sleep(0.01)
        if interrupt_handoff:
            # The child exists but Reviewer has not received its Popen handle yet.
            os.kill(os.getpid(), signal_number)
        return child

    def stop_with_repeated_signals(child: subprocess.Popen[bytes]) -> None:
        if child not in owned:
            original_stop(child)
            return
        cleanup_started.append(child.pid)
        os.kill(os.getpid(), signal_number)
        os.kill(os.getpid(), signal_number)
        original_stop(child)

    monkeypatch.setenv("PATH", repo.environment["PATH"])
    monkeypatch.setenv("FAKE_CODEX_STATE", str(repo.state))
    monkeypatch.setattr(subprocess, "Popen", launch_then_signal)
    monkeypatch.setattr(code_review, "_stop", stop_with_repeated_signals)
    signal.signal(signal_number, unprotected_signal)
    try:
        with pytest.raises(code_review.ReviewError, match="interrupted by signal"):
            reviewer._run(target, index=True, revision="HEAD", timeout=1, model=None)
        assert len(owned) == 1
        child = owned[0]
        assert cleanup_started == [child.pid]
        assert child.poll() is not None, "The reviewer process survived interruption"
        deadline = time.monotonic() + 2
        while not _child_released_lock(repo.state) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert _child_released_lock(repo.state), "The descendant survived interruption"
        assert not reviewer._receipt_path(target).exists()
        assert signal.getsignal(signal_number) is unprotected_signal
    finally:
        signal.signal(signal_number, previous_handler)
        for child in owned:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=5)
            if child.stdin is not None:
                child.stdin.close()


def _candidate_receipt(repo: ReviewRepository) -> tuple[Path, dict[str, Any]]:
    reviewer = code_review.Reviewer(repo.path)
    path = reviewer._receipt_path(reviewer._target(index=True))
    return path, json.loads(path.read_text(encoding="utf-8"))


def _merge_full_baseline(repo: ReviewRepository) -> tuple[str, Path]:
    _assert_passed(repo.review("run", "--index"))
    path, document = _candidate_receipt(repo)
    assert document["scope"] == "repository"
    repo.git("commit", "-m", "Merge initial full repository review")
    return repo.git("rev-parse", "HEAD"), path


def _next_candidate(repo: ReviewRepository, name: str = "next") -> None:
    repo.git("switch", "-c", f"task/{name}")
    repo.commit_file(f"{name}.py", "value = 2\n")
    repo.git("switch", "main")
    repo.git("merge", "--no-ff", "--no-commit", f"task/{name}")


def test_first_review_covers_existing_repository_and_verifies_before_merge(
    review_repository: ReviewRepository,
) -> None:
    repo = review_repository
    _assert_passed(repo.review("run", "--index"))
    prompt = str(repo.calls()[-1]["prompt"])
    assert "Review scope: full repository." in prompt
    assert "including ALL tracked source" in prompt
    assert "even when they predate this task" in prompt
    assert "defects introduced by this complete change" not in prompt
    _, receipt = _candidate_receipt(repo)
    assert receipt["schema_version"] == 2
    assert receipt["target"]["profile"] == "h2h-code-review-v2"
    assert receipt["scope"] == "repository"
    assert receipt["baseline"] is None
    _assert_passed(repo.review("verify", "--index", mode="nonzero"))
    assert len(repo.calls()) == 1


def test_merged_repository_review_supports_a_continuous_incremental_chain(
    review_repository: ReviewRepository,
) -> None:
    repo = review_repository
    baseline_commit, baseline_path = _merge_full_baseline(repo)
    _next_candidate(repo)
    _assert_passed(repo.review("run", "--index"))
    _, receipt = _candidate_receipt(repo)
    assert receipt["scope"] == "incremental"
    reference = receipt["baseline"]
    assert reference["commit"] == baseline_commit
    assert reference["receipt_key"] == baseline_path.stem
    assert "Review scope: incremental." in str(repo.calls()[-1]["prompt"])
    _assert_passed(repo.review("verify", "--index"))
    repo.git("commit", "-m", "Merge incremental review")
    _next_candidate(repo, "third")
    _assert_passed(repo.review("run", "--index"))
    _, next_receipt = _candidate_receipt(repo)
    assert next_receipt["scope"] == "incremental"
    assert next_receipt["baseline"] == reference
    _assert_passed(repo.review("verify", "--index"))


def test_incremental_revalidation_does_not_duplicate_ancestor_file_descriptors(
    review_repository: ReviewRepository,
) -> None:
    repo = review_repository
    _merge_full_baseline(repo)
    reviewer = code_review.Reviewer(repo.path)
    for index in range(40):
        repo.commit_file("README.md", f"# Documented change {index}\n")
        # Lock files legitimately persist after an admitted review is cancelled,
        # even when documentation can bridge that commit without a receipt.
        with reviewer._review_lock(
            reviewer._target(index=False, revision="HEAD"), exclusive=True
        ):
            pass
    _next_candidate(repo)
    # One retained descriptor per ancestor fits. Opening a second complete lock
    # chain at publication exhausts this real OS budget after Codex succeeds.
    command = (
        "import resource, runpy, sys; "
        "_, hard = resource.getrlimit(resource.RLIMIT_NOFILE); "
        "resource.setrlimit(resource.RLIMIT_NOFILE, (int(sys.argv[1]), hard)); "
        "sys.argv = ['scripts/review-code.py', 'run', '--index']; "
        "runpy.run_path(sys.argv[0], run_name='__main__')"
    )
    for limit, expected_scope in ((64, "incremental"), (32, "repository")):
        result = subprocess.run(
            (sys.executable, "-c", command, str(limit)),
            cwd=repo.path,
            env=repo.environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        _assert_passed(result)
        # If even one lock per ancestor exceeds the budget, release all guards
        # and do a new repository review; never issue unlocked incremental proof.
        assert _candidate_receipt(repo)[1]["scope"] == expected_scope
        _assert_passed(repo.review("verify", "--index"))


def test_aborted_full_candidate_cannot_establish_a_baseline(
    review_repository: ReviewRepository,
) -> None:
    repo = review_repository
    _assert_passed(repo.review("run", "--index"))
    old_path, _ = _candidate_receipt(repo)
    repo.git("merge", "--abort")
    _next_candidate(repo)
    _assert_passed(repo.review("run", "--index"))
    assert old_path.exists(), "Unused evidence may remain but cannot cover primary"
    assert _candidate_receipt(repo)[1]["scope"] == "repository"


def test_full_receipt_on_second_parent_cannot_cover_primary(
    review_repository: ReviewRepository,
) -> None:
    repo = review_repository
    original_primary = repo.git("rev-parse", "HEAD")
    baseline_commit, _ = _merge_full_baseline(repo)
    repo.git("switch", "-c", "unreviewed-primary", original_primary)
    repo.commit_file("other-primary.py", "value = 3\n")
    repo.git("merge", "--no-ff", "--no-commit", baseline_commit)
    _assert_passed(repo.review("run", "--index"))
    assert _candidate_receipt(repo)[1]["scope"] == "repository"


@pytest.mark.parametrize("damage", ["missing", "v1", "malformed"])
def test_missing_obsolete_or_invalid_baseline_requires_repository_review(
    review_repository: ReviewRepository, damage: str
) -> None:
    repo = review_repository
    _, baseline_path = _merge_full_baseline(repo)
    if damage == "missing":
        baseline_path.unlink()
    elif damage == "v1":
        document = json.loads(baseline_path.read_text(encoding="utf-8"))
        document["schema_version"] = 1
        document["target"]["profile"] = "h2h-code-review-v1"
        document.pop("scope")
        document.pop("baseline")
        baseline_path.write_text(json.dumps(document), encoding="utf-8")
    else:
        baseline_path.write_text("not JSON", encoding="utf-8")
    _next_candidate(repo)
    _assert_passed(repo.review("run", "--index"))
    assert _candidate_receipt(repo)[1]["scope"] == "repository"


@pytest.mark.parametrize("document_only", [False, True])
def test_only_proven_documentation_can_bridge_unreviewed_primary_commits(
    review_repository: ReviewRepository, document_only: bool
) -> None:
    repo = review_repository
    baseline_commit, _ = _merge_full_baseline(repo)
    if document_only:
        repo.commit_file("README.md", "# Documented development behavior\n")
    else:
        repo.commit_file("unreviewed.py", "unreviewed_value = 9\n")
    _next_candidate(repo)
    _assert_passed(repo.review("run", "--index"))
    _, receipt = _candidate_receipt(repo)
    assert receipt["scope"] == ("incremental" if document_only else "repository")
    if document_only:
        assert receipt["baseline"]["commit"] == baseline_commit
    _assert_passed(repo.review("verify", "--index"))


@pytest.mark.parametrize(
    "changed",
    [
        "AGENTS.md",
        "scripts/review-code.py",
        "scripts/review-merge-candidate.sh",
        *code_review.REVIEW_FILES,
    ],
)
def test_changed_policy_or_shared_implementation_requires_repository_review(
    review_repository: ReviewRepository, changed: str
) -> None:
    repo = review_repository
    _merge_full_baseline(repo)
    _next_candidate(repo)
    path = (
        repo.path / changed
        if changed in code_review.POLICY_PATHS
        else (repo.path / repo.implementation).with_name(changed)
    )
    with path.open("a", encoding="utf-8") as stream:
        stream.write("\n# Review contract changed.\n")
    repo.git("add", str(path.relative_to(repo.path)))
    _assert_passed(repo.review("run", "--index"))
    # Use the receipt reported by this subprocess: its changed implementation
    # intentionally has a different executed-bytes digest from this test process.
    receipts = [
        json.loads(item.read_text())
        for item in (repo.path / ".git/h2h-review/receipts").glob("*.json")
    ]
    matching = [
        item for item in receipts if item["target"]["tree"] == repo.git("write-tree")
    ]
    assert len(matching) == 1
    assert matching[0]["scope"] == "repository"
    _assert_passed(repo.review("verify", "--index"))


@pytest.mark.parametrize("damage", ["delete", "rewrite", "scope"])
def test_incremental_verification_rechecks_full_baseline_authority(
    review_repository: ReviewRepository, damage: str
) -> None:
    repo = review_repository
    _, baseline_path = _merge_full_baseline(repo)
    _next_candidate(repo)
    _assert_passed(repo.review("run", "--index"))
    _assert_passed(repo.review("verify", "--index"))
    if damage == "delete":
        baseline_path.unlink()
    else:
        document = json.loads(baseline_path.read_text(encoding="utf-8"))
        if damage == "rewrite":
            document["reviewed_at"] = "A later review run replaced the authority"
        else:
            document["scope"] = "incremental"
        baseline_path.write_text(json.dumps(document), encoding="utf-8")
    calls = repo.calls()
    _assert_blocked(repo.review("verify", "--index"))
    assert repo.calls() == calls
    _assert_passed(repo.review("run", "--index"))
    expected = "incremental" if damage == "rewrite" else "repository"
    assert _candidate_receipt(repo)[1]["scope"] == expected


def test_scope_cannot_be_changed_without_invalidating_result_integrity(
    review_repository: ReviewRepository,
) -> None:
    repo = review_repository
    _merge_full_baseline(repo)
    _next_candidate(repo)
    _assert_passed(repo.review("run", "--index"))
    path, receipt = _candidate_receipt(repo)
    assert receipt["scope"] == "incremental"
    receipt["scope"] = "repository"
    receipt["baseline"] = None
    path.write_text(json.dumps(receipt), encoding="utf-8")
    _assert_blocked(repo.review("verify", "--index"))


def test_baseline_deleted_during_review_cannot_receive_incremental_receipt(
    review_repository: ReviewRepository,
) -> None:
    repo = review_repository
    _, baseline_path = _merge_full_baseline(repo)
    _next_candidate(repo)
    with _held_review(repo, "hold-pass") as owner:
        baseline_path.unlink()
        (repo.state / "release").touch()
        stdout, stderr = owner.communicate(timeout=10)
        assert owner.returncode != 0, stdout + stderr
    _assert_blocked(repo.review("verify", "--index"))


def test_incremental_review_holds_baseline_shared_lock_until_receipt_is_written(
    review_repository: ReviewRepository,
) -> None:
    repo = review_repository
    baseline_commit, baseline_path = _merge_full_baseline(repo)
    original_receipt = baseline_path.read_bytes()
    _next_candidate(repo)
    with _held_review(repo, "hold-pass") as owner:
        competing = repo.review("run", "--revision", baseline_commit)
        _assert_blocked(competing)
        assert "already running" in competing.stderr
        assert baseline_path.read_bytes() == original_receipt
        (repo.state / "release").touch()
        stdout, stderr = owner.communicate(timeout=10)
        assert owner.returncode == 0, stdout + stderr
    _assert_passed(repo.review("verify", "--index"))


def test_explicit_repository_scope_reestablishes_a_full_baseline(
    review_repository: ReviewRepository,
) -> None:
    repo = review_repository
    _merge_full_baseline(repo)
    _next_candidate(repo)
    _assert_passed(repo.review("run", "--index", "--scope", "repository"))
    _, receipt = _candidate_receipt(repo)
    assert receipt["scope"] == "repository"
    assert receipt["baseline"] is None
    _assert_passed(repo.review("verify", "--index"))


@pytest.mark.parametrize("damage", ["delete", "malformed"])
def test_incremental_verification_rechecks_every_intervening_review(
    review_repository: ReviewRepository, damage: str
) -> None:
    repo = review_repository
    _merge_full_baseline(repo)
    _next_candidate(repo)
    _assert_passed(repo.review("run", "--index"))
    intermediate_path, _ = _candidate_receipt(repo)
    repo.git("commit", "-m", "Merge reviewed intermediate change")
    _next_candidate(repo, "third")
    _assert_passed(repo.review("run", "--index"))
    assert _candidate_receipt(repo)[1]["scope"] == "incremental"
    if damage == "delete":
        intermediate_path.unlink()
    else:
        intermediate_path.write_text("malformed receipt", encoding="utf-8")
    calls = repo.calls()
    _assert_blocked(repo.review("verify", "--index"))
    assert repo.calls() == calls
    _assert_passed(repo.review("run", "--index"))
    assert _candidate_receipt(repo)[1]["scope"] == "repository"


def test_initial_primary_root_without_a_baseline_can_receive_full_review(
    review_repository: ReviewRepository,
) -> None:
    repo = review_repository
    repo.git("merge", "--abort")
    root = repo.git("rev-list", "--max-parents=0", "HEAD")
    repo.git("switch", "-c", "root-primary", root)
    repo.git("merge", "--no-ff", "--no-commit", "task/example")
    _assert_passed(repo.review("run", "--index"))
    assert _candidate_receipt(repo)[1]["scope"] == "repository"
    _assert_passed(repo.review("verify", "--index"))


def test_review_policy_uses_git_paths_even_with_windows_filesystem_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_review_repository(tmp_path)
    reviewer = code_review.Reviewer(repo.path)
    tree = repo.git("write-tree")
    expected = reviewer._policy_digest(tree)

    def windows_relative_path(value: str | Path) -> Path | PureWindowsPath:
        if str(value) in code_review.IMPLEMENTATION_PATHS:
            return PureWindowsPath(value)
        return Path(value)

    # Before the fix, using filesystem Path for Git object paths produced native
    # backslashes and rev-parse rejected the candidate implementation blobs.
    monkeypatch.setattr(code_review, "Path", windows_relative_path)
    assert reviewer._policy_digest(tree) == expected


@pytest.mark.parametrize("value", ["invalid", "0", "-1", "999999999"])
def test_external_review_owner_rejects_unrelated_or_invalid_parent(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("H2H_REVIEW_PROCESS_OWNER", value)
    with pytest.raises(code_review.ReviewError, match=r"[Ii]nvalid|does not match"):
        code_review._external_process_owner()


def test_external_review_owner_requires_the_direct_process_group_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("H2H_REVIEW_PROCESS_OWNER", "100")
    namespace = SimpleNamespace(
        environ=os.environ,
        name="posix",
        getpid=lambda: 201,
        getppid=lambda: 100,
        getpgrp=lambda: 200,
    )
    monkeypatch.setattr(code_review, "os", namespace)
    with pytest.raises(code_review.ReviewError, match="does not match"):
        code_review._external_process_owner()
    namespace.getpgrp = lambda: 201
    with pytest.raises(code_review.ReviewError, match="implementation differs"):
        code_review._external_process_owner()
    monkeypatch.setenv("H2H_REVIEW_OWNER_SOURCE", merge_command.owner_source_digest())
    assert code_review._external_process_owner()


def test_review_fingerprints_the_loaded_external_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_review_repository(tmp_path)
    reviewer = code_review.Reviewer(repo.path)
    tree = repo.git("write-tree")
    original = reviewer._policy_digest(tree)
    loaded = tmp_path / "loaded_merge_command.py"
    loaded.write_bytes(Path(merge_command.__file__).read_bytes())
    monkeypatch.setattr(merge_command, "__file__", str(loaded))
    assert reviewer._policy_digest(tree) == original
    with loaded.open("a", encoding="utf-8") as stream:
        stream.write("\n# The actual external owner import differs.\n")
    assert reviewer._policy_digest(tree) != original


@pytest.mark.parametrize("change", ["candidate", "baseline"])
def test_managed_receipt_revalidates_candidate_and_coverage_before_publication(
    review_repository: ReviewRepository, change: str
) -> None:
    repo = review_repository
    _baseline, baseline_path = _merge_full_baseline(repo)
    _next_candidate(repo)
    _assert_passed(repo.review("run", "--index"))
    reviewer = code_review.Reviewer(repo.path)
    target = reviewer._target(index=True)
    receipt = reviewer._receipt_path(target)
    pending = repo.state / "pending-review.json"
    receipt.replace(pending)
    if change == "candidate":
        (repo.path / "feature.py").write_text("value = 100\n")
        repo.git("add", "feature.py")
    else:
        baseline_path.unlink()
    with (
        reviewer._review_lock(target, exclusive=True),
        pytest.raises(code_review.ReviewError, match=r"candidate|coverage"),
    ):
        reviewer._publish_managed_receipt(target, pending)
    assert not receipt.exists()
    assert pending.exists()
