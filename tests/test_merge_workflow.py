from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from h2h_dev_tools import merge_command, process

_ROOT = Path(__file__).resolve().parents[1]
_REVIEWER = """import json
import subprocess
import sys
from pathlib import Path

def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()

directory = Path(git("rev-parse", "--git-common-dir"))
mode = sys.argv[1]
with (directory / "fixture-review-events").open("a") as events:
    events.write(mode + "\\n")
candidate = [git("rev-parse", "HEAD"), git("rev-parse", "MERGE_HEAD"), git("write-tree")]
receipt = directory / "fixture-review"
if mode == "run":
    if (directory / "reject-review").exists():
        raise SystemExit(1)
    receipt.write_text(json.dumps(candidate))
elif mode == "verify":
    assert sys.argv[2:] == ["--index", "--expected-tree", candidate[2]]
    assert json.loads(receipt.read_text()) == candidate
else:
    raise AssertionError(mode)
"""


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


def fixture_repository(root: Path, *, succeeds: bool, profile: str = "full") -> str:
    root.mkdir()
    git(root, "init", "-b", "master")
    git(root, "config", "user.name", "Merge Test")
    git(root, "config", "user.email", "merge@example.invalid")
    (root / ".gitignore").write_text(".venv/\n")
    (root / "README.md").write_text("baseline\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "chore: initialize repository")
    base = git(root, "rev-parse", "HEAD")
    git(root, "switch", "-c", "task")
    scripts = root / "scripts"
    scripts.mkdir()
    hooks = root / ".githooks"
    hooks.mkdir()
    for name in (
        "git-flow-merge.sh",
        "detect-primary-branch.sh",
        "release-gate.py",
        "review-merge-candidate.sh",
    ):
        shutil.copy2(_ROOT / "scripts" / name, scripts / name)
    shutil.copytree(
        _ROOT / "src/h2h_dev_tools",
        root / "src/h2h_dev_tools",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    for name in ("pre-commit", "pre-merge-commit", "commit-msg", "pre-rebase"):
        shutil.copy2(_ROOT / ".githooks" / name, hooks / name)
    (scripts / "check-version.py").write_text("print('version fixture passed')\n")
    (scripts / "check_change_scope.py").write_text(f"print({profile!r})\n")
    (scripts / "review-code.py").write_text(_REVIEWER)
    (scripts / "check-docs.py").write_text("print('documentation fixture passed')\n")
    executable(scripts / "check-fast.sh", "#!/bin/sh\nexit 0\n")
    executable(
        scripts / "check-full.sh",
        "#!/bin/sh\n"
        "printf 'checked\\n' >> \"$(git rev-parse --git-path fixture-gate)\"\n"
        f"exit {0 if succeeds else 1}\n",
    )
    git(root, "add", ".")
    git(root, "commit", "-m", "feat: add candidate")
    local_bin = root / ".venv/bin"
    local_bin.mkdir(parents=True)
    (local_bin / "python").symlink_to(sys.executable)
    git(root, "config", "core.hooksPath", ".githooks")
    return base


@pytest.mark.parametrize("succeeds", [True, False])
def test_merge_checks_real_candidate_once_and_preserves_task_on_failure(
    tmp_path: Path, succeeds: bool
) -> None:
    root = tmp_path / "repository"
    base = fixture_repository(root, succeeds=succeeds)
    result = subprocess.run(
        ("scripts/git-flow-merge.sh",),
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) is succeeds, result.stdout + result.stderr
    assert (root / ".git/fixture-gate").read_text() == "checked\n"
    expected_reviews = "run\nverify\nverify\n" if succeeds else "run\nverify\n"
    assert (root / ".git/fixture-review-events").read_text() == expected_reviews
    assert not (root / ".git/MERGE_HEAD").exists()
    if succeeds:
        assert git(root, "branch", "--show-current") == "master"
        assert len(git(root, "show", "-s", "--format=%P", "HEAD").split()) == 2
        assert git(root, "branch", "--list", "task") == ""
    else:
        assert git(root, "branch", "--show-current") == "task"
        assert git(root, "rev-parse", "master") == base
        assert git(root, "branch", "--list", "task")
    assert git(root, "status", "--porcelain") == ""


def test_release_gate_rejects_candidate_mutation(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    fixture_repository(root, succeeds=True)
    executable(
        root / "scripts/check-full.sh",
        "#!/bin/sh\nprintf 'changed\\n' > README.md\n",
    )
    git(root, "add", ".")
    git(
        root, "-c", "core.hooksPath=/dev/null", "commit", "-m", "test: mutate candidate"
    )
    git(root, "switch", "master")
    git(root, "merge", "--no-ff", "--no-commit", "task")
    subprocess.run(
        (sys.executable, "scripts/review-code.py", "run", "--index"),
        cwd=root,
        check=True,
    )
    result = subprocess.run(
        (sys.executable, "scripts/release-gate.py"),
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "candidate changed during the gate" in result.stderr


def test_full_gate_rejects_unreviewed_candidate_before_running_checks(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    fixture_repository(root, succeeds=True)
    git(root, "switch", "master")
    git(root, "merge", "--no-ff", "--no-commit", "task")
    result = subprocess.run(
        (sys.executable, "scripts/release-gate.py"),
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert not (root / ".git/fixture-gate").exists()
    assert (root / ".git/fixture-review-events").read_text() == "verify\n"


@pytest.mark.parametrize("failure", ["review", "evidence-after-gate"])
def test_review_failure_aborts_merge_and_preserves_task(
    tmp_path: Path, failure: str
) -> None:
    root = tmp_path / "repository"
    base = fixture_repository(root, succeeds=True)
    if failure == "review":
        (root / ".git/reject-review").touch()
    else:
        executable(
            root / "scripts/check-full.sh",
            '#!/bin/sh\nrm "$(git rev-parse --git-path fixture-review)"\n',
        )
        git(root, "add", ".")
        git(root, "commit", "-m", "test: invalidate review during checks")
    result = subprocess.run(
        ("scripts/git-flow-merge.sh",),
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert git(root, "rev-parse", "master") == base
    assert git(root, "branch", "--show-current") == "task"
    assert git(root, "branch", "--list", "task")
    assert not (root / ".git/MERGE_HEAD").exists()
    expected = "run\n" if failure == "review" else "run\nverify\nverify\n"
    assert (root / ".git/fixture-review-events").read_text() == expected


def test_documentation_merge_does_not_run_or_verify_review(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    fixture_repository(root, succeeds=True, profile="documentation")
    (root / ".git/reject-review").touch()
    result = subprocess.run(
        ("scripts/git-flow-merge.sh",),
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (root / ".git/fixture-review-events").exists()
    assert not (root / ".git/fixture-gate").exists()
    assert git(root, "branch", "--show-current") == "master"


def test_missing_environment_leaves_task_checkout_and_primary_untouched(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    base = fixture_repository(root, succeeds=True)
    task = git(root, "rev-parse", "HEAD")
    shutil.rmtree(root / ".venv")
    result = subprocess.run(
        ("scripts/git-flow-merge.sh",),
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "missing repository-local Python" in result.stderr
    assert "Switched to branch" not in result.stderr
    assert git(root, "branch", "--show-current") == "task"
    assert git(root, "rev-parse", "HEAD") == task
    assert git(root, "rev-parse", "master") == base
    assert not (root / ".git/MERGE_HEAD").exists()
    assert git(root, "status", "--porcelain") == ""


@pytest.mark.parametrize("cleanup_only", [False, True])
def test_noop_cleanup_needs_no_python_environment(
    tmp_path: Path, cleanup_only: bool
) -> None:
    root = tmp_path / "repository"
    fixture_repository(root, succeeds=True)
    git(root, "switch", "master")
    git(root, "-c", "core.hooksPath=/dev/null", "merge", "--no-ff", "--no-edit", "task")
    primary = git(root, "rev-parse", "HEAD")
    git(root, "switch", "task")
    shutil.rmtree(root / ".venv")
    result = subprocess.run(
        ("scripts/git-flow-merge.sh", *(["--cleanup-only"] if cleanup_only else [])),
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert git(root, "branch", "--show-current") == "master"
    assert git(root, "rev-parse", "HEAD") == primary
    assert not git(root, "branch", "--list", "task")


def test_merge_accepts_windows_style_environment_layout(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    fixture_repository(root, succeeds=True)
    # Exercise interpreter discovery on the host, not Windows process semantics.
    executable(root / ".githooks/pre-merge-commit", "#!/bin/sh\nexit 0\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "test: use shell-only merge hook")
    shutil.rmtree(root / ".venv")
    windows_bin = root / ".venv/Scripts"
    windows_bin.mkdir(parents=True)
    (windows_bin / "python.exe").symlink_to(sys.executable)
    result = subprocess.run(
        (
            "bash",
            "src/h2h_dev_tools/git-flow-merge.sh",
            str(root),
            "direct",
            "",
        ),
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert git(root, "branch", "--show-current") == "master"
    assert not git(root, "branch", "--list", "task")


@pytest.mark.parametrize("cleanup_proven", [False, True])
def test_launcher_reserves_125_for_unproven_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cleanup_proven: bool
) -> None:
    def run_command(
        phase: process.ProcessCommand, *, hard_deadline: float | None
    ) -> int:
        assert phase.argv == ("git", "commit", "--no-edit")
        assert phase.cwd == tmp_path
        assert hard_deadline is None
        if not cleanup_proven:
            raise process.ProcessOwnershipError("fixture cleanup failure")
        return 125

    monkeypatch.setattr(process, "run_command", run_command)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "merge-command",
            "--cleanup-receipt",
            str(tmp_path / "cleanup"),
            str(tmp_path),
            "--",
            "git",
            "commit",
            "--no-edit",
        ],
    )
    assert merge_command.main() == (1 if cleanup_proven else 125)
    assert (tmp_path / "cleanup").exists() is cleanup_proven


@pytest.mark.parametrize("cleanup_proven", [False, True])
def test_merge_only_aborts_after_owned_cleanup_is_proven(
    tmp_path: Path, cleanup_proven: bool
) -> None:
    root = tmp_path / "repository"
    base = fixture_repository(root, succeeds=True)
    if cleanup_proven:
        executable(root / "scripts/check-full.sh", "#!/bin/sh\nexit 125\n")
    else:
        # Inject the ownership failure at the process boundary. Candidate creation
        # remains real; no real descendant is intentionally left behind here.
        source = root / "src/h2h_dev_tools/process.py"
        source.write_text(
            source.read_text()
            + "\n_original_run_command = run_command\n"
            + "def run_command(phase, **kwargs):\n"
            + '    if phase.argv[-2:] == ("commit", "--no-edit"):\n'
            + '        raise ProcessOwnershipError("fixture cleanup unproven")\n'
            + "    return _original_run_command(phase, **kwargs)\n"
        )
    git(root, "add", ".")
    git(root, "commit", "-m", "test: exercise merge cleanup failure")
    task = git(root, "rev-parse", "HEAD")
    result = subprocess.run(
        ("scripts/git-flow-merge.sh",),
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert git(root, "rev-parse", "master") == base
    assert git(root, "rev-parse", "task") == task
    assert (root / ".git/MERGE_HEAD").exists() is not cleanup_proven
    if cleanup_proven:
        assert git(root, "branch", "--show-current") == "task"
        assert git(root, "status", "--porcelain") == ""
        assert "process cleanup unproven" not in result.stderr
    else:
        assert git(root, "branch", "--show-current") == "master"
        assert "process cleanup unproven; candidate and task retained" in result.stderr


@pytest.mark.parametrize("exit_status", [0, 1, 125])
def test_launcher_unexpected_exit_without_receipt_preserves_candidate(
    tmp_path: Path, exit_status: int
) -> None:
    root = tmp_path / "repository"
    base = fixture_repository(root, succeeds=True)
    source = root / "src/h2h_dev_tools/process.py"
    source.write_text(
        source.read_text()
        + "\n_original_run_command = run_command\n"
        + "def run_command(phase, **kwargs):\n"
        + '    if phase.argv[-2:] == ("commit", "--no-edit"):\n'
        + f"        raise SystemExit({exit_status})\n"
        + "    return _original_run_command(phase, **kwargs)\n"
    )
    git(root, "add", ".")
    git(root, "commit", "-m", "test: exit owner without cleanup evidence")
    task = git(root, "rev-parse", "HEAD")
    result = subprocess.run(
        ("scripts/git-flow-merge.sh",),
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert git(root, "rev-parse", "master") == base
    assert git(root, "rev-parse", "task") == task
    assert git(root, "branch", "--show-current") == "master"
    assert (root / ".git/MERGE_HEAD").exists()
    assert "process cleanup unproven; candidate and task retained" in result.stderr


_BLOCKING_GATE = r"""import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

state = Path(os.environ["MERGE_INTERRUPTION_STATE"])
lock = (state / "leader-lock").open("a")
fcntl.flock(lock, fcntl.LOCK_EX)
(state / "group").write_text(str(os.getpgrp()))
child_code = '''
import fcntl
import os
import signal
import sys
import time
from pathlib import Path
state = Path(sys.argv[1])
signal.signal(signal.SIGTERM, signal.SIG_IGN)
lock = (state / "child-lock").open("a")
fcntl.flock(lock, fcntl.LOCK_EX)
(state / "ready").touch()
while True:
    time.sleep(0.1)
'''
subprocess.Popen([sys.executable, "-c", child_code, str(state)])
while True:
    time.sleep(0.1)
"""


def _assert_gate_lock_released(path: Path) -> None:
    import fcntl

    with path.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.skipif(os.name == "nt", reason="Exercises real POSIX process signals")
@pytest.mark.parametrize("strategy", ["direct", "candidate"])
@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("handoff_signal", [False, True])
def test_merge_interruption_cleans_gate_descendants_before_restoring_git_state(
    tmp_path: Path, strategy: str, committed: bool, handoff_signal: bool
) -> None:
    root = tmp_path / "repository"
    base = fixture_repository(root, succeeds=True)
    state = tmp_path / "state"
    state.mkdir()
    (root / "scripts/blocking-gate.py").write_text(_BLOCKING_GATE)
    executable(
        root / ".githooks/pre-merge-commit",
        "#!/bin/sh\nexec scripts/check-full.sh\n",
    )
    executable(
        root / ".githooks/pre-commit",
        "#!/bin/sh\n"
        "if git rev-parse --verify MERGE_HEAD >/dev/null 2>&1; then\n"
        "    exec .githooks/pre-merge-commit\n"
        "fi\n",
    )
    blocking_entry = (
        "#!/bin/sh\n"
        'if [ -n "${MERGE_INTERRUPTION_STATE:-}" ]; then\n'
        "    exec .venv/bin/python scripts/blocking-gate.py\n"
        "fi\n"
    )
    hook = "post-merge" if strategy == "direct" else "post-commit"
    executable(
        root / (f".githooks/{hook}" if committed else "scripts/check-full.sh"),
        blocking_entry,
    )
    if handoff_signal:
        # Deterministically interrupt after the supervisor has started a real
        # gate tree but before the shell registers $!. Foreground sleep does not
        # overwrite the PID of the last asynchronous command.
        shell = root / "src/h2h_dev_tools/git-flow-merge.sh"
        shell.write_text(
            shell.read_text().replace(
                "    active_pid=$!\n",
                '    if [[ "$active_owner" == process && " $* " != *" --no-commit "* ]]; then\n'
                '        while [[ ! -e "$MERGE_INTERRUPTION_STATE/ready" ]]; do sleep 0.01; done\n'
                '        kill -TERM "$$"\n'
                "    fi\n"
                "    active_pid=$!\n",
            )
        )
    git(root, "add", ".")
    git(root, "commit", "-m", "test: install blocking gate")
    task = git(root, "rev-parse", "HEAD")
    wrapper = subprocess.Popen(
        (
            "bash",
            str(root / "src/h2h_dev_tools/git-flow-merge.sh"),
            str(root),
            strategy,
            "scripts/review-merge-candidate.sh" if strategy == "candidate" else "",
        ),
        cwd=root,
        env=os.environ | {"MERGE_INTERRUPTION_STATE": str(state)},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 20
        while not (state / "ready").exists():
            assert wrapper.poll() is None, wrapper.communicate()[0]
            assert time.monotonic() < deadline, "The gate descendant never started"
            time.sleep(0.01)
        if committed:
            assert git(root, "rev-parse", "master") != base
        if not handoff_signal:
            wrapper.send_signal(signal.SIGTERM)
        time.sleep(0.05)
        # A repeated cancellation must not interrupt the first teardown.
        wrapper.send_signal(signal.SIGTERM)
        try:
            output, _ = wrapper.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            pytest.fail("TERM did not stop the merge gate and its owned descendants")
        assert wrapper.returncode == 143, output
        _assert_gate_lock_released(state / "leader-lock")
        _assert_gate_lock_released(state / "child-lock")
        assert not (root / ".git/MERGE_HEAD").exists()
        assert git(root, "rev-parse", "task") == task
        if committed:
            assert git(root, "branch", "--show-current") == "master"
            parents = git(root, "show", "-s", "--format=%P", "master").split()
            assert parents == [base, task]
        else:
            assert git(root, "branch", "--show-current") == "task"
            assert git(root, "rev-parse", "master") == base
        assert git(root, "status", "--porcelain") == ""
    finally:
        groups = {wrapper.pid}
        if (state / "group").exists():
            groups.add(int((state / "group").read_text()))
        for group in groups:
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        wrapper.communicate(timeout=5)
