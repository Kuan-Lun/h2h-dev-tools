from __future__ import annotations

import hashlib
import json
import os
import runpy
import shlex
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

from h2h_dev_tools import (
    bundle,
    bundle_verify,
    code_review,
    merge_command,
    process,
    review_execution,
    review_plan,
    review_test_support,
)
from h2h_dev_tools.executable_fixture import write_python_executable

ROOT = Path(__file__).resolve().parents[1]

FAKE_CODEX = (
    review_test_support.synthetic_review_response_source()
    + r'''
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
prompt = sys.stdin.read()
with (state / "calls.jsonl").open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({"args": sys.argv[1:], "prompt": prompt}) + "\n")

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

if mode == "change-worktree":
    Path(os.environ["FAKE_CODEX_DRIFT_PATH"]).write_text(
        "Unreviewed local changes.\n", encoding="utf-8"
    )
if mode == "hide-index-change":
    subprocess.run(
        ["git", "update-index", os.environ["FAKE_CODEX_INDEX_FLAG"], "feature.py"],
        check=True,
    )
    Path("feature.py").write_text("value = 'unreviewed'\n", encoding="utf-8")

output = Path(sys.argv[sys.argv.index("--output-last-message") + 1])
if mode == "missing":
    raise SystemExit(0)
if mode == "malformed":
    output.write_text("not JSON", encoding="utf-8")
    raise SystemExit(0)

result = synthetic_review_response(prompt)
if mode == "integration-finding" and result['shard_id'] == 'integration':
    mode = 'p2'
if mode == 'missing-unit' and result['unit_reviews']:
    result['unit_reviews'].pop()
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
output.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
print(json.dumps({"type": "turn.completed"}))
'''
)


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


def _refresh_snapshot_manifest(repo: ReviewRepository) -> None:
    """Represent deliberately changed test sources as a valid candidate bundle."""
    package = (repo.path / repo.implementation).parent
    manifest = package / "manifest.json"
    if not manifest.exists():
        return
    document = json.loads(manifest.read_text(encoding="utf-8"))
    hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in package.iterdir()
        if path.suffix in {".py", ".sh"}
    }
    if document["files"] == hashes:
        return
    document["files"] = hashes
    document["payload_sha256"] = bundle_verify.payload_digest(hashes)
    manifest.write_text(json.dumps(document), encoding="utf-8")
    # A closure-change fixture also updates its verifier's component contract.
    # Check that exact contract, rather than bypassing production validation.
    runpy.run_path(str(package / "bundle_verify.py"))["verify_bundle"](repo.path)


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
    write_python_executable(executable, FAKE_CODEX)
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
    if layout == "snapshot":
        (local / "pyproject.toml").write_text(
            "[tool.h2h-dev-tools]\ncomponents = ['code-review']\n",
            encoding="utf-8",
        )
        bundle.sync_bundle(local)
    else:
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


@pytest.mark.parametrize("model", [None, "fixture-model"], ids=["default", "explicit"])
def test_review_binds_the_real_merge_candidate_and_verifies_after_commit(
    review_repository: ReviewRepository, model: str | None
) -> None:
    repo = review_repository
    base = repo.git("rev-parse", "HEAD")
    tree = repo.git("write-tree")
    model_arguments = () if model is None else ("--model", model)
    _assert_passed(repo.review("run", "--index", *model_arguments))
    calls = repo.calls()
    assert len(calls) == 1
    arguments = calls[0]["args"]
    assert isinstance(arguments, list)
    assert arguments[0] == "exec"
    assert arguments[arguments.index("--sandbox") + 1] == "read-only"
    if model is None:
        assert "--model" not in arguments
    else:
        assert arguments[arguments.index("--model") + 1] == model
    configuration = [
        arguments[index + 1] for index, value in enumerate(arguments) if value == "-c"
    ]
    assert configuration == ['approval_policy="never"', "notify=[]"]
    assert all(
        isinstance(value, str)
        and '"' not in value.partition("=")[0]
        and "'" not in value.partition("=")[0]
        for value in configuration
    )
    # Preserve normal CLI session persistence so subagent spawning can resolve
    # its parent thread. Only completion notifications are disabled.
    assert "--ephemeral" not in arguments
    # Do not suppress user model,
    # authentication, tools/plugins, feature flags, or safety-hook settings.
    expected_options = {
        "--sandbox",
        "-c",
        "--json",
        "--output-schema",
        "--output-last-message",
        "-",
    }
    if model is not None:
        expected_options.add("--model")
    assert {
        value for value in arguments if isinstance(value, str) and value.startswith("-")
    } == expected_options
    assert {
        "--json",
        "--output-schema",
        "--output-last-message",
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


@pytest.mark.parametrize(
    "name",
    ["ordinary.txt", " ", "\t", "\n"],
    ids=["ordinary", "space", "tab", "newline"],
)
@pytest.mark.parametrize("untracked", [False, True])
def test_review_rejects_dirty_worktrees_before_invoking_codex(
    review_repository: ReviewRepository, untracked: bool, name: str
) -> None:
    repo = review_repository
    if not untracked:
        (repo.path / name).write_text("Reviewed contents.\n", encoding="utf-8")
        repo.git("add", "--", name)
    reviewer = code_review.Reviewer(repo.path)
    target = reviewer._target(index=True)
    (repo.path / name).write_text("Unreviewed local changes.\n", encoding="utf-8")
    result = repo.review("run", "--index")
    _assert_blocked(result)
    assert "no unstaged or untracked files" in result.stderr
    assert repo.calls() == []
    assert reviewer._target(index=True) == target
    assert not reviewer._receipt_path(target).exists()


def test_review_accepts_a_clean_candidate_with_whitespace_paths(
    review_repository: ReviewRepository,
) -> None:
    repo = review_repository
    for name in (" ", "\t", "\n"):
        (repo.path / name).write_text("Reviewed contents.\n", encoding="utf-8")
        repo.git("add", "--", name)
    assert (
        subprocess.check_output(
            ("git", "diff", "--name-only", "-z"), cwd=repo.path, env=repo.environment
        )
        == b""
    )
    _assert_passed(repo.review("run", "--index"))
    _assert_passed(repo.review("verify", "--index"))
    assert len(repo.calls()) == 1


@pytest.mark.parametrize(
    "name",
    ["ordinary.txt", " ", "\t", "\n"],
    ids=["ordinary", "space", "tab", "newline"],
)
@pytest.mark.parametrize("untracked", [False, True])
def test_review_rejects_worktree_drift_before_publishing_a_receipt(
    review_repository: ReviewRepository, untracked: bool, name: str
) -> None:
    repo = review_repository
    if not untracked:
        (repo.path / name).write_text("Reviewed contents.\n", encoding="utf-8")
        repo.git("add", "--", name)
    reviewer = code_review.Reviewer(repo.path)
    target = reviewer._target(index=True)
    repo.environment["FAKE_CODEX_DRIFT_PATH"] = name
    result = repo.review("run", "--index", mode="change-worktree")
    _assert_blocked(result)
    assert "no unstaged or untracked files" in result.stderr
    assert len(repo.calls()) == 1
    assert (repo.path / name).read_text() == "Unreviewed local changes.\n"
    assert reviewer._target(index=True) == target
    assert not reviewer._receipt_path(target).exists()
    command = (
        ("ls-files", "--others", "--exclude-standard", "-z")
        if untracked
        else ("diff", "--name-only", "-z")
    )
    assert (
        subprocess.check_output(("git", *command), cwd=repo.path, env=repo.environment)
        == name.encode() + b"\0"
    )


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
    _refresh_snapshot_manifest(repo)
    repo.git("add", name)
    if (path.parent / "manifest.json").exists():
        repo.git("add", str(path.parent.relative_to(repo.path) / "manifest.json"))
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


@pytest.mark.parametrize(
    "mode,stream_name,expected",
    [
        ("hold-pass", "stdout", 0),
        ("hold-failure", "stdout", 1),
        ("hold-failure", "stderr", 1),
    ],
)
def test_standalone_review_preserves_result_after_diagnostic_reader_closes(
    review_repository: ReviewRepository, mode: str, stream_name: str, expected: int
) -> None:
    repo = review_repository
    reviewer = code_review.Reviewer(repo.path)
    receipt = reviewer._receipt_path(reviewer._target(index=True))
    with _held_review(repo, mode) as owner:
        assert owner.stdout is not None
        assert owner.stdout.readline().startswith("Reviewing repository scope:")
        assert "aggregate deadline" in owner.stdout.readline()
        assert owner.stdout.readline().startswith("Review logs:")
        assert not receipt.exists(), "Codex must still be held before publication"
        stream = getattr(owner, stream_name)
        assert stream is not None
        stream.close()
        # communicate must not read our deliberately closed pipe endpoint.
        setattr(owner, stream_name, None)
        (repo.state / "release").write_text("release review", encoding="utf-8")
        stdout, stderr = owner.communicate(timeout=5)

    if expected == 0:
        assert receipt.exists(), "A successful real review must publish its evidence"
        _assert_passed(repo.review("verify", "--index"))
    else:
        assert not receipt.exists()
        _assert_blocked(repo.review("verify", "--index"))
    assert len(repo.calls()) == 1
    assert owner.returncode == expected, (stdout or "") + (stderr or "")


def test_offline_verify_succeeds_when_stdout_has_no_reader(
    review_repository: ReviewRepository,
) -> None:
    repo = review_repository
    _assert_passed(repo.review("run", "--index"))
    reviewer = code_review.Reviewer(repo.path)
    receipt = reviewer._receipt_path(reviewer._target(index=True))
    original_receipt = receipt.read_bytes()
    reader, writer = os.pipe()
    os.close(reader)
    try:
        result = subprocess.run(
            (sys.executable, "scripts/review-code.py", "verify", "--index"),
            cwd=repo.path,
            env=repo.environment,
            stdout=writer,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=5,
        )
    finally:
        os.close(writer)

    assert receipt.read_bytes() == original_receipt
    assert len(repo.calls()) == 1, "Offline verify must not invoke Codex"
    assert result.returncode == 0, result.stderr


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
        result = repo.review("run", "--index", "--timeout-seconds", "8", mode="timeout")
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


def test_standalone_surviving_descendant_blocks_publication_even_after_cleanup(
    review_repository: ReviewRepository,
) -> None:
    repo = review_repository
    try:
        _assert_blocked(repo.review("run", "--index", mode="passing-child"))
        assert (repo.state / "child-pid").exists()
        assert _child_released_lock(repo.state)
        _assert_blocked(repo.review("verify", "--index"))
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
    owners: list[process.OwnedProcess] = []
    original_stop = process.terminate_owned_tree

    def remember_owner(
        owner: process.OwnedProcess, *, deadline: float, **kwargs: Any
    ) -> bool:
        if owner.process in review_processes:
            owners.append(owner)
        return original_stop(owner, deadline=deadline, **kwargs)

    def launch(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        child = original_popen(*args, **kwargs)
        if "review_worker.py" in str(args[0]) and "exec" in args[0]:
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
    monkeypatch.setattr(process, "terminate_owned_tree", remember_owner)
    try:
        started = time.monotonic()
        with pytest.raises(process.ProcessOwnershipError, match=r"cleanup.*proven"):
            reviewer._run(target, index=True, revision="HEAD", timeout=10, model=None)
        assert time.monotonic() - started < 10
        assert len(review_processes) == 1
        assert review_processes[0].poll() == 0
        assert not reviewer._receipt_path(target).exists()
        with pytest.raises(code_review.ReviewError, match="No valid code review"):
            reviewer._verify(target)
    finally:
        # Remove the simulated obstruction, then obtain actual group proof.
        # Never release a parent obligation using the mocked cleanup result.
        monkeypatch.setattr(os, "killpg", original_killpg)
        for owner in owners:
            cleanup_deadline = time.monotonic() + 5
            assert original_stop(owner, deadline=cleanup_deadline)
            assert process.close_owner(owner, deadline=cleanup_deadline)


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
    _refresh_snapshot_manifest(repo)
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
    _refresh_snapshot_manifest(repo)
    repo.git("add", str(package.relative_to(repo.path)))
    _assert_passed(repo.review("run", "--index"))
    _assert_passed(repo.review("verify", "--index"))
    repo.git("commit", "-m", "Merge component closure fixture")
    _assert_passed(repo.review("verify"))
    # Verify the reviewed candidate without the index workspace guard: the
    # dependency fingerprint itself must reject changed executed source bytes.
    dependency.write_text("value = 2\n", encoding="utf-8")
    _refresh_snapshot_manifest(repo)
    _assert_blocked(repo.review("verify"))
    assert len(repo.calls()) == 1


def test_review_rejects_a_relocated_loaded_bundle_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_review_repository(tmp_path)
    reviewer = code_review.Reviewer(repo.path)
    tree = repo.git("write-tree")
    reviewer._policy_digest(tree)
    loaded = tmp_path / "loaded_bundle_verify.py"
    loaded.write_bytes(Path(bundle_verify.__file__).read_bytes())
    monkeypatch.setattr(bundle_verify, "__file__", str(loaded))
    with pytest.raises(
        code_review.ReviewError, match="differs from its source registration"
    ):
        reviewer._policy_digest(tree)


def _write_fixture_receipt(
    reviewer: code_review.Reviewer, target: code_review.Target, state: Path
) -> None:
    plan = review_plan.build_plan(
        reviewer.repository,
        target.document(),
        code_review.Coverage().document(),
        deadline=time.monotonic() + 30,
    )
    stages = []
    identifiers = [f"shard-{i:04d}" for i in range(len(plan.shards))]
    if len(plan.shards) > 1:
        identifiers.append("integration")
    for index, identifier in enumerate(identifiers):
        units = [] if identifier == "integration" else plan.shards[index]
        result = {
            "verdict": "pass",
            "summary": "Synthetic offline verification fixture.",
            "findings": [],
            "plan_digest": plan.fingerprint,
            "shard_id": identifier,
            "unit_reviews": [
                {"unit_id": key, "method": "Synthetic method.", "related_paths": []}
                for key in units
            ],
            "integration_reviews": [
                {
                    "obligation": obligation,
                    "analysis": "Synthetic integration method.",
                    "source_paths": ["AGENTS.md"],
                }
                for obligation in review_plan.INTEGRATION_OBLIGATIONS
            ]
            if identifier == "integration" or len(plan.shards) == 1
            else [],
            "contracts": [],
        }
        stages.append(
            {
                "id": identifier,
                "review": result,
                "review_sha256": review_plan.digest(review_plan.canonical(result)),
                "owned_tree_empty": True,
            }
        )
    execution = {
        "plan": plan.document(),
        "plan_digest": plan.fingerprint,
        "stages": stages,
    }
    reviewer._write_receipt(target, result, "fixture", None, state, execution=execution)


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
    _write_fixture_receipt(first, first_target, first_repo.state)
    assert first._verify(first_target).exists()
    with pytest.raises(code_review.ReviewError, match="No valid code review"):
        second._verify(second_target)


def test_windows_offline_verification_does_not_require_codex_or_posix_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_review_repository(tmp_path)
    reviewer = code_review.Reviewer(repo.path)
    target = reviewer._target(index=True)
    _write_fixture_receipt(reviewer, target, repo.state)
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
    original_stop = process.terminate_owned_tree
    owned: list[subprocess.Popen[bytes]] = []
    cleanup_started: list[int] = []

    def unprotected_signal(_signal: int, _frame: object) -> None:
        raise AssertionError("Reviewer did not install its signal controller")

    def launch_then_signal(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        command = args[0]
        if "review_worker.py" not in str(command) or "exec" not in command:
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

    def stop_with_repeated_signals(
        owner: process.OwnedProcess, *, deadline: float, **kwargs: Any
    ) -> bool:
        child = owner.process
        if child not in owned:
            return original_stop(owner, deadline=deadline, **kwargs)
        cleanup_started.append(child.pid)
        os.kill(os.getpid(), signal_number)
        os.kill(os.getpid(), signal_number)
        return original_stop(owner, deadline=deadline, **kwargs)

    monkeypatch.setenv("PATH", repo.environment["PATH"])
    monkeypatch.setenv("FAKE_CODEX_STATE", str(repo.state))
    monkeypatch.setattr(subprocess, "Popen", launch_then_signal)
    monkeypatch.setattr(process, "terminate_owned_tree", stop_with_repeated_signals)
    signal.signal(signal_number, unprotected_signal)
    try:
        with pytest.raises(code_review.ReviewError, match="interrupted by signal"):
            reviewer._run(target, index=True, revision="HEAD", timeout=8, model=None)
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
    assert "includes ALL tracked source" in prompt
    assert "even when they predate this task" in prompt
    assert "For EACH unit_reviews entry, related_paths MUST include EVERY" in prompt
    assert "Repeat shared paths in every applicable unit entry" in prompt
    assert "Read those sources before attesting" in prompt
    assert "defects introduced by this complete change" not in prompt
    _, receipt = _candidate_receipt(repo)
    assert receipt["schema_version"] == 3
    assert receipt["target"]["profile"] == "h2h-code-review-v3"
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


@pytest.mark.parametrize("failure_kind", ["git", "deadline", "ownership"])
def test_baseline_classifier_only_falls_back_for_clean_git_query_failure(
    review_repository: ReviewRepository,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    repo = review_repository
    _merge_full_baseline(repo)
    repo.commit_file("README.md", "# Intervening historical document\n")
    historical_blob = repo.git("rev-parse", "HEAD:README.md")
    _next_candidate(repo)
    reviewer = code_review.Reviewer(repo.path)
    target = reviewer._target(index=True)
    original = review_execution.git_output
    injected = []
    unknown = process.ProcessOwnershipError("historical Git cleanup is unknown")

    def read(root: Path, *arguments: str, deadline: float | None = None) -> bytes:
        if arguments == ("cat-file", "blob", historical_blob):
            injected.append(arguments)
            if failure_kind == "ownership":
                raise unknown
            if failure_kind == "deadline":
                raise review_execution.ExecutionError("historical query deadline")
            # A real, fully owned Git nonzero result can invalidate an old
            # documentation bridge without making the current tree incomplete.
            return original(root, "cat-file", "blob", "0" * 40, deadline=deadline)
        return original(root, *arguments, deadline=deadline)

    monkeypatch.setattr(review_execution, "git_output", read)
    if failure_kind == "git":
        with reviewer._select_coverage(target, force_repository=False) as coverage:
            assert coverage == code_review.Coverage("repository")
    elif failure_kind == "deadline":
        with pytest.raises(review_execution.ExecutionError, match="historical query"):
            with reviewer._select_coverage(target, force_repository=False):
                pytest.fail("An expired query cannot become a new full review")
    else:
        with pytest.raises(process.ProcessOwnershipError) as caught:
            with reviewer._select_coverage(target, force_repository=False):
                pytest.fail("Unknown cleanup cannot become a new full review")
        assert caught.value is unknown
    assert injected == [("cat-file", "blob", historical_blob)]


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
    _refresh_snapshot_manifest(repo)
    repo.git("add", str(path.relative_to(repo.path)))
    if (path.parent / "manifest.json").exists():
        repo.git("add", str(path.parent.relative_to(repo.path) / "manifest.json"))
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


def test_review_rejects_a_relocated_loaded_external_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_review_repository(tmp_path)
    reviewer = code_review.Reviewer(repo.path)
    tree = repo.git("write-tree")
    reviewer._policy_digest(tree)
    loaded = tmp_path / "loaded_merge_command.py"
    loaded.write_bytes(Path(merge_command.__file__).read_bytes())
    monkeypatch.setattr(merge_command, "__file__", str(loaded))
    with pytest.raises(
        code_review.ReviewError, match="differs from its source registration"
    ):
        reviewer._policy_digest(tree)


@pytest.mark.parametrize(
    "change", ["candidate", "baseline", "unstaged-space", "untracked-space"]
)
def test_managed_receipt_revalidates_candidate_and_coverage_before_publication(
    review_repository: ReviewRepository, change: str
) -> None:
    repo = review_repository
    _baseline, baseline_path = _merge_full_baseline(repo)
    _next_candidate(repo)
    if change == "unstaged-space":
        (repo.path / " ").write_text("Reviewed contents.\n")
        repo.git("add", "--", " ")
    _assert_passed(repo.review("run", "--index"))
    reviewer = code_review.Reviewer(repo.path)
    target = reviewer._target(index=True)
    receipt = reviewer._receipt_path(target)
    pending = repo.state / "pending-review.json"
    receipt.replace(pending)
    if change == "candidate":
        (repo.path / "feature.py").write_text("value = 100\n")
        repo.git("add", "feature.py")
    elif change == "baseline":
        baseline_path.unlink()
    else:
        (repo.path / " ").write_text("Unreviewed local changes.\n")
    with (
        reviewer._review_lock(target, exclusive=True),
        pytest.raises(code_review.ReviewError, match=r"candidate|coverage|no unstaged"),
    ):
        reviewer._publish_managed_receipt(target, pending)
    assert not receipt.exists()
    assert pending.exists()


def test_result_diagnostic_failure_cannot_publish_standalone_evidence(
    review_repository: ReviewRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = review_repository
    reviewer = code_review.Reviewer(repo.path)
    target = reviewer._target(index=True)
    original = process.report_diagnostic
    failure = process.ProcessOwnershipError("diagnostic cleanup unproven")

    def report(message: str, **options: Any) -> None:
        if "Review result ready; writing" in message:
            raise failure
        original(message, **options)

    monkeypatch.setenv("PATH", repo.environment["PATH"])
    monkeypatch.setenv("FAKE_CODEX_STATE", str(repo.state))
    monkeypatch.setattr(process, "report_diagnostic", report)
    with pytest.raises(process.ProcessOwnershipError) as raised:
        reviewer._run(target, index=True, revision="HEAD", timeout=10, model=None)
    assert raised.value is failure
    assert not reviewer._receipt_path(target).exists()
    assert len(repo.calls()) == 1


def test_owner_diagnostic_failure_precedes_managed_evidence_publication(
    review_repository: ReviewRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = review_repository
    classifier = repo.path / "scripts/check_change_scope.py"
    classifier.write_text(
        "import runpy\nfrom pathlib import Path\n"
        "root = Path(__file__).resolve().parents[1]\n"
        f"package = root / {str(Path(repo.implementation).parent)!r}\n"
        "runpy.run_path(str(package / 'source_loader.py'))['load_package'](package, 'h2h_dev_tools')\n"
        "runpy.run_module('h2h_dev_tools.change_scope', run_name='__main__')\n"
    )
    repo.git("add", "scripts/check_change_scope.py")
    reviewer = code_review.Reviewer(repo.path)
    target = reviewer._target(index=True)
    original = process.report_diagnostic
    failure = process.ProcessOwnershipError("diagnostic cleanup unproven")

    def report(message: str, **options: Any) -> None:
        if message.startswith("Code review cleanup verified; publishing"):
            raise failure
        original(message, **options)

    monkeypatch.setattr(process, "report_diagnostic", report)
    with (
        pytest.raises(process.ProcessOwnershipError) as raised,
        merge_command._review(
            repo.path,
            (sys.executable, "scripts/review-code.py", "run", "--index"),
            dict(repo.environment),
        ),
    ):
        pytest.fail("Diagnostic failure must precede publication")
    assert raised.value is failure
    assert not reviewer._receipt_path(target).exists()
    assert len(repo.calls()) == 1


def test_cli_does_not_launch_diagnostic_after_unknown_review_cleanup(
    review_repository: ReviewRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = review_repository
    failure = process.ProcessOwnershipError("review cleanup unproven")
    reports: list[str] = []

    def fail_review(*args: Any, **kwargs: Any) -> None:
        # Pre-admission Git queries own their normal in-memory diagnostics.
        # Start the trap exactly when cleanup becomes unknown.
        monkeypatch.setattr(
            process,
            "report_diagnostic",
            lambda message, **options: reports.append(message),
        )
        raise failure

    monkeypatch.setattr(code_review.Reviewer, "_run", fail_review)
    monkeypatch.setattr(sys, "argv", ["review-code.py", "run", "--index"])
    with pytest.raises(SystemExit) as raised:
        code_review.main(repo.path)
    assert raised.value.code == process.TERMINATION_FAILED_EXIT_CODE
    assert raised.value.__cause__ is failure
    assert reports == []


@pytest.mark.skipif(
    os.name != "posix",
    reason="The fake Codex executable and online review ownership require POSIX",
)
@pytest.mark.parametrize("change", ["edit", "add", "remove", "mode"])
def test_parent_initializer_change_requires_a_new_repository_review(
    tmp_path: Path, change: str
) -> None:
    repo = _make_review_repository(tmp_path, "snapshot")
    entry = repo.path / "scripts/review-code.py"
    entry.write_text(
        entry.read_text()
        .replace("package, 'h2h_dev_tools')", "package, '_shared.h2h_dev_tools')")
        .replace(
            "from h2h_dev_tools.code_review", "from _shared.h2h_dev_tools.code_review"
        )
    )
    parent = repo.path / "scripts/_shared/__init__.py"
    if change != "add":
        parent.write_text("VALUE = 1\n")
    repo.git("add", "scripts")
    _assert_passed(repo.review("run", "--index"))
    baseline_tree = repo.git("write-tree")
    baseline_receipts = [
        json.loads(path.read_text())
        for path in (repo.path / ".git/h2h-review/receipts").glob("*.json")
    ]
    (baseline,) = [
        item for item in baseline_receipts if item["target"]["tree"] == baseline_tree
    ]
    assert baseline["scope"] == "repository"
    repo.git("commit", "-m", "Merge initial full repository review")
    _next_candidate(repo)
    if change == "remove":
        parent.unlink()
    elif change == "mode":
        parent.chmod(0o755)
    else:
        parent.write_text("VALUE = 2\n")
    repo.git("add", "scripts/_shared")
    _assert_passed(repo.review("run", "--index"))
    receipts = [
        json.loads(path.read_text())
        for path in (repo.path / ".git/h2h-review/receipts").glob("*.json")
    ]
    (current,) = [
        item for item in receipts if item["target"]["tree"] == repo.git("write-tree")
    ]
    assert current["scope"] == "repository"
    assert current["target"]["policy_digest"] != baseline["target"]["policy_digest"]
    _assert_passed(repo.review("verify", "--index"))


@pytest.mark.skipif(
    os.name != "posix",
    reason="The fake Codex executable and online review ownership require POSIX",
)
@pytest.mark.parametrize("change", ["edit", "add-ignored", "remove", "mode"])
def test_offline_revision_verification_rejects_changed_current_parent_dependency(
    tmp_path: Path, change: str
) -> None:
    repo = _make_review_repository(tmp_path, "snapshot")
    entry = repo.path / "scripts/review-code.py"
    entry.write_text(
        entry.read_text()
        .replace("package, 'h2h_dev_tools')", "package, '_shared.h2h_dev_tools')")
        .replace(
            "from h2h_dev_tools.code_review", "from _shared.h2h_dev_tools.code_review"
        )
    )
    parent = repo.path / "scripts/_shared/__init__.py"
    if change == "add-ignored":
        (repo.path / ".gitignore").write_text("/scripts/_shared/__init__.py\n")
    else:
        parent.write_text("VALUE = 1\n")
    repo.git("add", ".")
    revision, _ = _merge_full_baseline(repo)
    calls = len(repo.calls())
    if change == "remove":
        parent.unlink()
    elif change == "mode":
        parent.chmod(0o755)
    else:
        parent.write_text("VALUE = 2\n")
    if change == "add-ignored":
        assert repo.git("status", "--porcelain") == ""
    _assert_blocked(repo.review("verify", "--revision", revision))
    result = repo.review("run", "--revision", revision)
    _assert_blocked(result)
    if change == "add-ignored":
        assert "Current review parent differs from the exact candidate" in result.stderr
    assert len(repo.calls()) == calls


def test_parent_policy_fingerprint_is_portable_between_exact_checkouts(
    tmp_path: Path,
) -> None:
    repo = _make_review_repository(tmp_path, "snapshot")
    (repo.path / "scripts/_shared/__init__.py").write_text("VALUE = 1\n")
    repo.git("add", "scripts")
    tree = repo.git("write-tree")
    original = code_review.Reviewer(repo.path)._policy_digest(tree)
    copied = tmp_path / "another checkout"
    shutil.copytree(repo.path, copied)
    assert code_review.Reviewer(copied)._policy_digest(tree) == original


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
@pytest.mark.parametrize("command", ["run", "verify"])
@pytest.mark.parametrize("changed", [False, True])
def test_review_rejects_hidden_index_entries_before_using_evidence(
    review_repository: ReviewRepository, flag: str, command: str, changed: bool
) -> None:
    repo = review_repository
    reviewer = code_review.Reviewer(repo.path)
    target = reviewer._target(index=True)
    if command == "verify":
        _assert_passed(repo.review("run", "--index"))
    calls = repo.calls()
    repo.git("update-index", flag, "feature.py")
    if changed:
        (repo.path / "feature.py").write_text("value = 'unreviewed'\n")
    assert repo.git("diff", "--name-only") == ""
    before = repo.git("ls-files", "-v", "--", "feature.py")
    result = repo.review(command, "--index")
    _assert_blocked(result)
    assert "index" in result.stderr.lower()
    assert repo.calls() == calls
    assert repo.git("ls-files", "-v", "--", "feature.py") == before
    assert repo.git("write-tree") == target.tree
    assert (
        tuple(repo.git("rev-parse", "HEAD", "MERGE_HEAD").splitlines())
        == target.parents
    )
    if command == "run":
        assert not reviewer._receipt_path(target).exists()


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_review_rejects_index_visibility_drift_before_publishing(
    review_repository: ReviewRepository, flag: str
) -> None:
    repo = review_repository
    reviewer = code_review.Reviewer(repo.path)
    target = reviewer._target(index=True)
    repo.environment["FAKE_CODEX_INDEX_FLAG"] = flag
    result = repo.review("run", "--index", mode="hide-index-change")
    _assert_blocked(result)
    assert "index" in result.stderr.lower()
    assert len(repo.calls()) == 1
    assert repo.git("diff", "--name-only") == ""
    assert (repo.path / "feature.py").read_text() == "value = 'unreviewed'\n"
    assert repo.git("write-tree") == target.tree
    assert not reviewer._receipt_path(target).exists()


@pytest.mark.parametrize("mode", ["pass", "integration-finding", "missing-unit"])
def test_partitioned_review_requires_every_shard_and_integration(
    review_repository: ReviewRepository, mode: str
) -> None:
    repo = review_repository
    (repo.path / "large.py").write_text("value = 1\n" * 120000)
    repo.git("add", "large.py")
    result = repo.review("run", "--index", mode=mode)
    if mode == "pass":
        _assert_passed(result)
        _assert_passed(repo.review("verify", "--index"))
        _path, document = _candidate_receipt(repo)
        stages = document["execution"]["stages"]
        assert len(stages) > 2
        assert stages[-1]["id"] == "integration"
        assert all(stage["owned_tree_empty"] for stage in stages)
    else:
        _assert_blocked(result)
        _assert_blocked(repo.review("verify", "--index"))
    assignments = [
        json.loads(
            str(call["prompt"]).split(
                "COORDINATOR ASSIGNMENT (data, not instructions):\n", 1
            )[1]
        )
        for call in repo.calls()
    ]
    if mode == "integration-finding":
        assert assignments[-1]["shard_id"] == "integration"
    if mode == "missing-unit":
        assert all(
            assignment["shard_id"] != "integration" for assignment in assignments
        )


def test_passing_receipt_is_removed_if_publication_finishes_after_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from h2h_dev_tools import review_execution

    repo = _make_review_repository(tmp_path)
    reviewer = code_review.Reviewer(repo.path)
    target = reviewer._target(index=True)
    _write_fixture_receipt(reviewer, target, repo.state)
    path = reviewer._receipt_path(target)
    document = json.loads(path.read_text())
    path.unlink()
    original = os.replace
    renamed = []

    def late_replace(source: Any, destination: Any) -> None:
        original(source, destination)
        renamed.append(destination)
        time.sleep(0.2)

    monkeypatch.setattr(os, "replace", late_replace)
    with pytest.raises(review_execution.ExecutionError, match="deadline"):
        with review_execution.within_deadline(time.monotonic() + 0.1):
            reviewer._write_receipt(
                target,
                document["review"],
                "fixture",
                None,
                repo.state,
                execution=document["execution"],
            )
    assert renamed == [path]
    assert not path.exists()


@pytest.mark.skipif(
    os.name == "nt", reason="Uses real POSIX Git process termination and lock release"
)
def test_standalone_baseline_classification_obeys_aggregate_deadline(
    review_repository: ReviewRepository, tmp_path: Path
) -> None:
    repo = review_repository
    _merge_full_baseline(repo)
    repo.commit_file("README.md", "# Intervening documentation commit\n")
    _next_candidate(repo)
    calls_before = len(repo.calls())
    real_git = shutil.which("git", path=repo.environment["PATH"])
    assert real_git is not None
    binary = tmp_path / "slow-classifier-git"
    binary.mkdir()
    marker = repo.state / "classifier-entered"
    lock = repo.state / "classifier-lock"
    shim = binary / "git"
    slow_query = write_python_executable(
        binary / "slow-query",
        f"import fcntl, os, sys, time\nfrom pathlib import Path\n"
        "if sys.argv[1:3] == ['diff', '--raw']:\n"
        f"    stream = Path({str(lock)!r}).open('w')\n"
        "    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)\n"
        f"    Path({str(marker)!r}).touch()\n"
        "    time.sleep(5)\n"
        f"os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])\n",
    )
    # Only the classifier query has injected latency. A Python shim around
    # every unrelated Git read would consume the short aggregate work budget
    # before this test reaches its intended deadline boundary.
    shim.write_text(
        '#!/bin/sh\nif [ "$1" = diff ] && [ "$2" = --raw ]; then\n'
        f'    exec {shlex.quote(str(slow_query))} "$@"\n'
        "fi\n"
        f'exec {shlex.quote(real_git)} "$@"\n'
    )
    shim.chmod(0o755)
    started = time.monotonic()
    result = subprocess.run(
        (
            sys.executable,
            "scripts/review-code.py",
            "run",
            "--index",
            "--timeout-seconds",
            "8",
        ),
        cwd=repo.path,
        env=repo.environment
        | {"PATH": str(binary) + os.pathsep + repo.environment["PATH"]},
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    elapsed = time.monotonic() - started
    assert marker.exists(), result.stdout + result.stderr
    assert result.returncode != 0
    assert elapsed < 4, f"Classifier exceeded the review work deadline: {elapsed}s"
    assert len(repo.calls()) == calls_before
    import fcntl

    with lock.open() as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    reviewer = code_review.Reviewer(repo.path)
    assert not reviewer._receipt_path(reviewer._target(index=True)).exists()


@pytest.mark.parametrize(
    "drift",
    [
        "staged",
        "unstaged",
        "untracked",
        "unstaged-space",
        "untracked-space",
        "parent",
        "revision",
    ],
)
def test_offline_verify_rechecks_authority_after_evidence_validation(
    review_repository: ReviewRepository, drift: str
) -> None:
    repo = review_repository
    if drift == "unstaged-space":
        (repo.path / " ").write_text("Reviewed contents.\n")
        repo.git("add", "--", " ")
    drift_path = (
        " "
        if drift.endswith("-space")
        else "unknown.py"
        if drift == "untracked"
        else "feature.py"
    )
    alternate = repo.git("rev-parse", "task/example")
    entry = repo.path / "scripts/review-code.py"
    entry.write_text(
        entry.read_text().replace(
            "main(root)\n",
            "from h2h_dev_tools import code_review\n"
            "import subprocess\n"
            "original_verify = code_review.Reviewer._verify\n"
            "def verify_then_drift(self, target):\n"
            "    result = original_verify(self, target)\n"
            "    if (root / '.git/drift-after-validation').exists():\n"
            + (
                f"        subprocess.run(['git', 'update-ref', 'HEAD', {alternate!r}], cwd=root, check=True)\n"
                if drift in {"parent", "revision"}
                else (
                    f"        (root / {drift_path!r}).write_text('value = 2\\n')\n"
                    + (
                        "        subprocess.run(['git', 'add', 'feature.py'], cwd=root, check=True)\n"
                        if drift == "staged"
                        else ""
                    )
                )
            )
            + "    return result\n"
            "code_review.Reviewer._verify = verify_then_drift\n"
            "main(root)\n",
        )
    )
    repo.git("add", "scripts/review-code.py")
    _assert_passed(repo.review("run", "--index"))
    expected_tree = repo.git("write-tree")
    if drift == "revision":
        repo.git("commit", "-m", "Merge exact reviewed fixture")
    (repo.path / ".git/drift-after-validation").touch()
    calls = len(repo.calls())
    result = repo.review(
        "verify",
        *(["--revision", "HEAD"] if drift == "revision" else ["--index"]),
        "--expected-tree",
        expected_tree,
    )
    _assert_blocked(result)
    assert len(repo.calls()) == calls, "Offline verification must not call Codex"


@pytest.mark.parametrize("boundary", ["write-return", "ancestor-exit", "run-return"])
def test_standalone_completion_failure_revokes_published_receipt(
    review_repository: ReviewRepository, boundary: str
) -> None:
    repo = review_repository
    marker = repo.state / "publication-reached"
    injection = (
        "import time\nfrom contextlib import contextmanager\n"
        "from h2h_dev_tools import code_review, process\n"
        "clock = time.monotonic\noffset = 0\n"
        "time.monotonic = lambda: clock() + offset\n"
        f"marker = Path({str(marker)!r})\n"
        "def expire():\n    global offset\n    marker.touch()\n    offset = 1000\n"
    )
    if boundary == "write-return":
        injection += (
            "original = code_review.Reviewer._write_receipt\n"
            "def write(self, *args, **kwargs):\n"
            "    original(self, *args, **kwargs)\n"
            "    expire()\n"
            "code_review.Reviewer._write_receipt = write\n"
        )
    elif boundary == "ancestor-exit":
        injection += (
            "original = code_review.Reviewer._select_coverage\n"
            "@contextmanager\ndef coverage(self, target, **kwargs):\n"
            "    with original(self, target, **kwargs) as value: yield value\n"
            "    if self._receipt_path(target).exists(): expire()\n"
            "code_review.Reviewer._select_coverage = coverage\n"
        )
    else:
        injection += (
            "original = code_review.Reviewer._run\n"
            "def run(self, *args, **kwargs):\n"
            "    original(self, *args, **kwargs)\n    expire()\n"
            "code_review.Reviewer._run = run\n"
        )
    entry = repo.path / "scripts/review-code.py"
    entry.write_text(
        entry.read_text().replace("main(root)\n", injection + "main(root)\n")
    )
    repo.git("add", "scripts/review-code.py")
    result = repo.review("run", "--index", "--timeout-seconds", "60")
    assert marker.exists(), result.stdout + result.stderr
    _assert_blocked(result)
    reviewer = code_review.Reviewer(repo.path)
    assert not reviewer._receipt_path(reviewer._target(index=True)).exists()
    _assert_blocked(repo.review("verify", "--index"))


@pytest.mark.parametrize(
    "boundary", ["publish-return", "pending-cleanup", "cleanup-proof"]
)
def test_managed_completion_failure_revokes_published_receipt(
    review_repository: ReviewRepository, boundary: str, tmp_path: Path
) -> None:
    repo = review_repository
    classifier = repo.path / "scripts/check_change_scope.py"
    classifier.write_text("print('full')\n")
    repo.git("add", "scripts/check_change_scope.py")
    marker = repo.state / "publication-reached"
    cleanup = tmp_path / "owner-cleanup"
    package = (repo.path / repo.implementation).parent
    script = (
        "import runpy, time\nfrom pathlib import Path\n"
        f"package = Path({str(package)!r})\n"
        "runpy.run_path(str(package / 'source_loader.py'))['load_package'](package, 'h2h_dev_tools')\n"
        "from h2h_dev_tools import code_review, merge_command\n"
        "clock = time.monotonic\noffset = 0\n"
        "time.monotonic = lambda: clock() + offset\n"
        f"marker = Path({str(marker)!r})\n"
        "def expire():\n    global offset\n    marker.touch()\n    offset = 1000\n"
    )
    if boundary == "publish-return":
        script += (
            "original = code_review.Reviewer._publish_managed_receipt\n"
            "def publish(self, *args):\n"
            "    result = original(self, *args)\n    expire()\n    return result\n"
            "code_review.Reviewer._publish_managed_receipt = publish\n"
        )
    elif boundary == "pending-cleanup":
        script += (
            "original = merge_command.tempfile.TemporaryDirectory\n"
            "class PendingDirectory(original):\n"
            "    def cleanup(self):\n"
            "        super().cleanup()\n"
            "        if Path(self.name).name.startswith('pending-'): expire()\n"
            "merge_command.tempfile.TemporaryDirectory = PendingDirectory\n"
        )
    else:
        script += (
            "original = Path.replace\n"
            "def replace(self, destination):\n"
            f"    if Path(destination) == Path({str(cleanup)!r}):\n"
            "        marker.touch()\n        raise OSError('injected cleanup proof failure')\n"
            "    return original(self, destination)\n"
            "Path.replace = replace\n"
        )
    script += "raise SystemExit(merge_command.main())\n"
    result = subprocess.run(
        (
            sys.executable,
            "-I",
            "-B",
            "-c",
            script,
            "--review",
            "--cleanup-receipt",
            str(cleanup),
            str(repo.path),
            "--",
            sys.executable,
            "scripts/review-code.py",
            "run",
            "--index",
            "--timeout-seconds",
            "60",
        ),
        cwd=repo.path,
        env=repo.environment | {"H2H_CODE_REVIEW_TIMEOUT_SECONDS": "60"},
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert marker.exists(), result.stdout + result.stderr
    assert repo.calls(), result.stdout + result.stderr
    _assert_blocked(result)
    reviewer = code_review.Reviewer(repo.path)
    assert not reviewer._receipt_path(reviewer._target(index=True)).exists()
    _assert_blocked(repo.review("verify", "--index"))


@pytest.mark.parametrize("boundary", ["write-return", "run-return"])
@pytest.mark.parametrize("signal_name", ["SIGINT", "SIGTERM", "SIGHUP"])
def test_standalone_publication_signal_revokes_receipt(
    review_repository: ReviewRepository, boundary: str, signal_name: str
) -> None:
    repo = review_repository
    marker = repo.state / "signal-after-publication"
    injection = (
        "import os, signal\n"
        "from h2h_dev_tools import code_review, process\n"
        f"marker = Path({str(marker)!r})\n"
        "def interrupt():\n"
        "    marker.touch()\n"
        f"    os.kill(os.getpid(), signal.{signal_name})\n"
    )
    if boundary == "write-return":
        injection += (
            "original = code_review.Reviewer._write_receipt\n"
            "def write(self, *args, **kwargs):\n"
            "    original(self, *args, **kwargs)\n"
            "    interrupt()\n"
            "code_review.Reviewer._write_receipt = write\n"
        )
    else:
        injection += (
            "original = code_review.Reviewer._run\n"
            "def run(self, *args, **kwargs):\n"
            "    original(self, *args, **kwargs)\n"
            "    interrupt()\n"
            "code_review.Reviewer._run = run\n"
        )
    entry = repo.path / "scripts/review-code.py"
    source = entry.read_text()
    assert source.count("main(root)\n") == 1
    entry.write_text(source.replace("main(root)\n", injection + "main(root)\n"))
    repo.git("add", "scripts/review-code.py")
    result = repo.review("run", "--index", "--timeout-seconds", "60")
    assert marker.exists(), result.stdout + result.stderr
    assert repo.calls(), "The real owned reviewer must finish before interruption"
    _assert_blocked(result)
    # This is a real signal after atomic publication, not an injected exception.
    # Offline verification must reject the failed invocation after its lock exits.
    _assert_blocked(repo.review("verify", "--index"))
    reviewer = code_review.Reviewer(repo.path)
    assert not reviewer._receipt_path(reviewer._target(index=True)).exists()


@pytest.mark.skipif(os.name == "nt", reason="Real POSIX signals during publication")
@pytest.mark.parametrize("signal_name", ["SIGINT", "SIGTERM", "SIGHUP"])
@pytest.mark.parametrize(
    "boundary", ["publish-return", "pending-cleanup", "cleanup-proof"]
)
def test_managed_publication_signal_revokes_receipt(
    review_repository: ReviewRepository, signal_name: str, boundary: str, tmp_path: Path
) -> None:
    repo = review_repository
    classifier = repo.path / "scripts/check_change_scope.py"
    classifier.write_text("print('full')\n")
    repo.git("add", "scripts/check_change_scope.py")
    marker = repo.state / "publication-signal"
    cleanup = tmp_path / "owner-cleanup"
    package = (repo.path / repo.implementation).parent
    script = (
        "import os, runpy, signal\nfrom pathlib import Path\n"
        f"package = Path({str(package)!r})\n"
        "runpy.run_path(str(package / 'source_loader.py'))['load_package'](package, 'h2h_dev_tools')\n"
        "from h2h_dev_tools import code_review, merge_command\n"
        f"marker = Path({str(marker)!r})\n"
        "def interrupt():\n"
        "    if not marker.exists():\n"
        "        marker.touch()\n"
        f"        os.kill(os.getpid(), signal.{signal_name})\n"
    )
    if boundary == "publish-return":
        script += (
            "original = code_review.Reviewer._publish_managed_receipt\n"
            "def publish(self, *args):\n"
            "    result = original(self, *args)\n    interrupt()\n    return result\n"
            "code_review.Reviewer._publish_managed_receipt = publish\n"
        )
    elif boundary == "pending-cleanup":
        script += (
            "original = merge_command.tempfile.TemporaryDirectory\n"
            "class PendingDirectory(original):\n"
            "    def cleanup(self):\n"
            "        super().cleanup()\n"
            "        if Path(self.name).name.startswith('pending-'): interrupt()\n"
            "merge_command.tempfile.TemporaryDirectory = PendingDirectory\n"
        )
    else:
        script += (
            "original = Path.replace\n"
            "def replace(self, destination):\n"
            "    result = original(self, destination)\n"
            f"    if Path(destination) == Path({str(cleanup)!r}): interrupt()\n"
            "    return result\n"
            "Path.replace = replace\n"
        )
    script += "raise SystemExit(merge_command.main())\n"
    result = subprocess.run(
        (
            sys.executable,
            "-I",
            "-B",
            "-c",
            script,
            "--review",
            "--cleanup-receipt",
            str(cleanup),
            str(repo.path),
            "--",
            sys.executable,
            "scripts/review-code.py",
            "run",
            "--index",
            "--timeout-seconds",
            "60",
        ),
        cwd=repo.path,
        env=repo.environment | {"H2H_CODE_REVIEW_TIMEOUT_SECONDS": "60"},
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert marker.exists(), result.stdout + result.stderr
    assert repo.calls(), result.stdout + result.stderr
    assert result.returncode != 0
    reviewer = code_review.Reviewer(repo.path)
    receipt = reviewer._receipt_path(reviewer._target(index=True))
    verification = repo.review("verify", "--index")
    # Record the real consequence before the negative assertion: OLD SIGTERM/HUP
    # kills the owner with a passing receipt that the unchanged verifier accepts.
    (repo.state / "signal-proof.json").write_text(
        json.dumps(
            {
                "signal": signal_name,
                "boundary": boundary,
                "returncode": result.returncode,
                "receipt_exists": receipt.exists(),
                "verify_returncode": verification.returncode,
                "cleanup_receipt_exists": cleanup.exists(),
            }
        )
    )
    assert not receipt.exists(), result.stdout + result.stderr
    _assert_blocked(verification)
    assert result.returncode == 128 + int(getattr(signal, signal_name))
    assert cleanup.read_text() == "owned_tree_empty\n"


def test_managed_admission_diagnostic_cleanup_failure_exits_125(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    failure = process.ProcessOwnershipError(
        "Diagnostic writer cleanup could not be proven"
    )
    calls: list[tuple[bool, float | None]] = []
    original_deadline = time.monotonic() + 100.0

    def fail_diagnostic(
        message: str, *, standard_output: bool = False, deadline: float | None = None
    ) -> None:
        assert message.startswith("Managed publication reserve:")
        calls.append((standard_output, deadline))
        raise failure

    monkeypatch.setattr(
        sys, "argv", ["review-code.py", "run", "--index", "--timeout-seconds", "100"]
    )
    monkeypatch.setenv("H2H_REVIEW_DEADLINE", str(original_deadline))
    monkeypatch.setattr(code_review, "_external_process_owner", lambda: True)
    monkeypatch.setattr(process, "report_diagnostic", fail_diagnostic)
    with pytest.raises(SystemExit) as raised:
        code_review.main(tmp_path)
    assert raised.value.code == process.TERMINATION_FAILED_EXIT_CODE == 125
    assert raised.value.__cause__ is failure
    assert calls == [(True, original_deadline - 5.0)]


@pytest.mark.parametrize(
    "change", [None, "code_review.py", "bundle_verify.py", "primary-branch.sh"]
)
@pytest.mark.parametrize("command", ["run", "verify"])
def test_review_rejects_source_changed_after_import_before_first_capture(
    review_repository: ReviewRepository, change: str | None, command: str
) -> None:
    repo = review_repository
    package = (repo.path / repo.implementation).parent
    namespace = (
        "_shared.h2h_dev_tools" if "_shared" in package.parts else "h2h_dev_tools"
    )
    entry = repo.path / "scripts/review-code.py"
    entry.write_text(
        "import hashlib, importlib, json, os, runpy, subprocess, sys\n"
        "from pathlib import Path\n"
        "sys.dont_write_bytecode = True\n"
        "root = Path(__file__).resolve().parents[1]\n"
        "sys.path.insert(0, str(root / 'scripts'))\n"
        f"package = root / {str(package.relative_to(repo.path))!r}\n"
        f"namespace = {namespace!r}\n"
        "runpy.run_path(str(package / 'source_loader.py'))['load_package'](package, namespace)\n"
        "review = importlib.import_module(namespace + '.code_review')\n"
        "verifier = importlib.import_module(namespace + '.bundle_verify')\n"
        "change = os.environ.get('FIXTURE_SOURCE_CHANGE')\n"
        "if change:\n"
        "    registered = review.__loader__._h2h_source_registration[2]\n"
        "    source = package / change\n"
        "    before = registered[change]\n"
        "    source.write_bytes(source.read_bytes() + b'\\n# New candidate source after import.\\n')\n"
        "    after = hashlib.sha256(source.read_bytes()).hexdigest()\n"
        "    assert before != after\n"
        "    assert review.__loader__._h2h_source_registration[2][change] == before\n"
        "    manifest_path = package / 'manifest.json'\n"
        "    if manifest_path.exists():\n"
        "        manifest = json.loads(manifest_path.read_text())\n"
        "        manifest['files'][change] = after\n"
        "        manifest['payload_sha256'] = verifier.payload_digest(manifest['files'])\n"
        "        manifest_path.write_text(json.dumps(manifest))\n"
        "        verifier.verify_bundle(root)\n"
        "    subprocess.run(['git', 'add', str(package)], cwd=root, check=True)\n"
        "    state = Path(os.environ['FAKE_CODEX_STATE'])\n"
        "    (state / 'loaded-before-capture.json').write_text(json.dumps({'loaded': before, 'staged': after}))\n"
        "if sys.argv[1] == 'verify' and os.environ.get('FIXTURE_PREPARE_RECEIPT'):\n"
        "    environment = {key: value for key, value in os.environ.items() if not key.startswith('FIXTURE_')}\n"
        "    fresh = subprocess.run([sys.executable, str(__file__), 'run', '--index'], cwd=root, env=environment, capture_output=True, text=True, timeout=15)\n"
        "    assert fresh.returncode == 0, fresh.stdout + fresh.stderr\n"
        "review.main(root)\n",
        encoding="utf-8",
    )
    repo.git("add", "scripts/review-code.py")
    environment = dict(repo.environment)
    if change is not None:
        environment["FIXTURE_SOURCE_CHANGE"] = change
    if command == "verify":
        environment["FIXTURE_PREPARE_RECEIPT"] = "1"
    result = subprocess.run(
        (sys.executable, str(entry), command, "--index"),
        cwd=repo.path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    fresh_verify = repo.review("verify", "--index")
    (repo.state / "source-authority-result.json").write_text(
        json.dumps(
            {
                "command": command,
                "change": change,
                "loaded_process": {
                    "exit": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                },
                "fresh_verify": {
                    "exit": fresh_verify.returncode,
                    "stdout": fresh_verify.stdout,
                    "stderr": fresh_verify.stderr,
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    if change is None:
        _assert_passed(result)
        _assert_passed(fresh_verify)
    else:
        observation = json.loads(
            (repo.state / "loaded-before-capture.json").read_text()
        )
        assert observation["loaded"] != observation["staged"]
        _assert_blocked(result)
        assert "Review source changed after bootstrap:" in result.stderr
        assert "differs from the controller registration" in result.stderr
        if command == "run":
            assert not repo.calls()
            _assert_blocked(fresh_verify)
            assert not list((repo.path / ".git/h2h-review/receipts").glob("*.json"))
            # A fresh process can review the updated candidate normally.
            _assert_passed(repo.review("run", "--index"))
        _assert_passed(repo.review("verify", "--index"))


def test_review_requires_the_actual_source_loader_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_review_repository(tmp_path)
    reviewer = code_review.Reviewer(repo.path)
    monkeypatch.setattr(code_review, "__loader__", None)
    with pytest.raises(code_review.ReviewError, match="source loader registration"):
        reviewer._policy_digest(repo.git("write-tree"))


@pytest.mark.parametrize("reserve", [6.0, 30.0], ids=["standalone", "managed"])
def test_planning_obeys_the_active_phase_deadline(
    review_repository: ReviewRepository,
    monkeypatch: pytest.MonkeyPatch,
    reserve: float,
) -> None:
    repo = review_repository
    reviewer = code_review.Reviewer(repo.path)
    target = reviewer._target(index=True)
    started = time.monotonic()
    cutoff = started + 900 - reserve
    observed: list[float] = []

    def stop_planning(*args: Any, deadline: float, **kwargs: Any) -> None:
        observed.append(deadline)
        raise review_plan.PlanError("Stop at the planning boundary")

    monkeypatch.setattr(review_plan, "build_plan", stop_planning)
    with (
        pytest.raises(review_plan.PlanError, match="planning boundary"),
        review_execution.within_deadline(cutoff),
    ):
        reviewer._run(
            target,
            index=True,
            revision="HEAD",
            timeout=900,
            model=None,
            started=started,
        )
    assert observed == [cutoff]
    assert not repo.calls(), "Planning must be bounded before Codex admission"
    assert not reviewer._receipt_path(target).exists()
