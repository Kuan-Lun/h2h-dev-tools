from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

import pytest
from test_merge_workflow import _PASS_REVIEW, fixture_repository, git

from h2h_dev_tools import merge_admission as admission
from h2h_dev_tools.executable_fixture import write_python_executable


def configured(tmp_path: Path) -> tuple[Path, Path, str, str]:
    root = tmp_path / "repository"
    base = fixture_repository(root, succeeds=True)
    task = git(root, "rev-parse", "HEAD")
    state = tmp_path / "workflow.json"
    admission.acquire(root, state, os.getpid())
    admission.configure(
        state,
        worktree=root,
        primary_ref="refs/heads/master",
        primary_oid=base,
        task_ref="refs/heads/task",
        task_oid=task,
    )
    return root, state, base, task


@pytest.mark.parametrize("cleanup", [False, True])
@pytest.mark.parametrize("drift", ["before", "during", "after"])
def test_primary_switch_ref_drift_retains_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cleanup: bool, drift: str
) -> None:
    root, state, base, _ = configured(tmp_path)
    if cleanup:
        admission.begin(state, cleanup=True)

    def move_task() -> None:
        git(root, "update-ref", "refs/heads/task", base)

    original_protect = admission._protect_checkout_paths
    original_run = subprocess.run

    def protect(workflow: admission.Workflow, *, target_tree: str) -> None:
        original_protect(workflow, target_tree=target_tree)
        move_task()

    def run_with_drift(
        command: tuple[str, ...], **kwargs: Any
    ) -> subprocess.CompletedProcess[Any]:
        result = original_run(command, **kwargs)
        if command[3] == "switch":
            move_task()
        return result

    if drift == "before":
        move_task()
    elif drift == "during":
        monkeypatch.setattr(admission, "_protect_checkout_paths", protect)
    else:
        monkeypatch.setattr(subprocess, "run", run_with_drift)
    with pytest.raises(admission.AdmissionError, match=r"changed|unexpected state"):
        admission.switch_primary(state)
    assert git(root, "branch", "--show-current") == (
        "master" if drift == "after" else "task"
    )
    assert git(root, "rev-parse", "master") == base
    with pytest.raises(admission.AdmissionError, match="changed"):
        admission.recover_abort(state)
    assert (root / ".git/h2h-merge-workflow.lock/owner.json").is_file()
    assert not (root / ".git/MERGE_HEAD").exists()


def candidate(root: Path, state: Path, task: str) -> None:
    git(root, "switch", "master")
    admission.begin(state, cleanup=False)
    git(root, "merge", "--no-ff", "--no-commit", task)
    admission.capture(state)


@pytest.mark.parametrize("linked", [False, True])
def test_workflow_admission_rejects_same_and_linked_invocations(
    tmp_path: Path, linked: bool
) -> None:
    root, state, _, _ = configured(tmp_path)
    other = root
    if linked:
        other = tmp_path / "linked"
        git(root, "worktree", "add", "-b", "other", str(other), "HEAD")
    lock = root / ".git/h2h-merge-workflow.lock"
    before = (lock / "owner.json").read_bytes()
    with pytest.raises(admission.AdmissionError, match="already held"):
        admission.acquire(other, tmp_path / "second.json", os.getpid())
    assert (lock / "owner.json").read_bytes() == before
    assert not (tmp_path / "second.json").exists()
    admission.recover_abort(state)
    admission.recover_restore(state)
    admission.release(state)
    assert not lock.exists()


@pytest.mark.parametrize(
    "drift", ["index", "worktree", "merge-head", "operation", "task-ref", "primary-ref"]
)
def test_recovery_preserves_external_candidate_drift(
    tmp_path: Path, drift: str
) -> None:
    root, state, _, task = configured(tmp_path)
    candidate(root, state, task)
    if drift in {"index", "worktree"}:
        (root / "README.md").write_text("external work\n")
        if drift == "index":
            git(root, "add", "README.md")
    elif drift == "task-ref":
        git(
            root,
            "update-ref",
            "refs/heads/task",
            git(root, "rev-parse", "master"),
            task,
        )
    elif drift == "primary-ref":
        git(root, "update-ref", "refs/heads/master", task)
    elif drift == "merge-head":
        (root / ".git/MERGE_HEAD").write_text(git(root, "rev-parse", "HEAD") + "\n")
    else:
        (root / ".git/CHERRY_PICK_HEAD").write_text(task + "\n")
    before_index = git(root, "ls-files", "--stage")
    before_head = (root / ".git/MERGE_HEAD").read_bytes()
    with pytest.raises(admission.AdmissionError, match=r"outside|Another"):
        admission.recover_abort(state)
    assert git(root, "ls-files", "--stage") == before_index
    assert (root / ".git/MERGE_HEAD").read_bytes() == before_head
    assert (root / ".git/h2h-merge-workflow.lock/owner.json").is_file()


def test_failure_before_merge_never_aborts_a_foreign_operation(tmp_path: Path) -> None:
    root, state, _, task = configured(tmp_path)
    git(root, "switch", "master")
    git(root, "merge", "--no-ff", "--no-commit", task)
    with pytest.raises(admission.AdmissionError, match="Unowned"):
        admission.recover_abort(state)
    assert (root / ".git/MERGE_HEAD").read_text().strip() == task
    assert (root / ".git/h2h-merge-workflow.lock").is_dir()


def test_post_commit_recovery_releases_without_restoring_task(tmp_path: Path) -> None:
    root, state, base, task = configured(tmp_path)
    git(root, "switch", "master")
    admission.begin(state, cleanup=False)
    git(root, "merge", "--no-ff", "--no-commit", task)
    admission.capture(state)
    # This test isolates lifecycle authority; full workflow tests run real gates.
    git(root, "-c", "core.hooksPath=/dev/null", "commit", "--no-edit")
    commit = git(root, "rev-parse", "HEAD")
    admission.recover_abort(state)
    admission.recover_restore(state)
    admission.release(state)
    assert git(root, "rev-parse", "HEAD") == commit
    assert git(root, "branch", "--show-current") == "master"
    assert git(root, "show", "-s", "--format=%P", "HEAD").split() == [base, task]
    assert not (root / ".git/h2h-merge-workflow.lock").exists()
    git(root, "switch", "task")
    retried = subprocess.run(
        ["scripts/git-flow-merge.sh", "--cleanup-only"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert retried.returncode == 0, retried.stdout + retried.stderr
    assert git(root, "rev-parse", "master") == commit
    assert not git(root, "branch", "--list", "task")
    assert not (root / ".git/h2h-merge-workflow.lock").exists()


def test_exact_candidate_abort_restores_original_primary(tmp_path: Path) -> None:
    root, state, base, task = configured(tmp_path)
    candidate(root, state, task)
    admission.recover_abort(state)
    admission.recover_restore(state)
    admission.release(state)
    assert git(root, "branch", "--show-current") == "task"
    assert git(root, "rev-parse", "master") == base
    assert not (root / ".git/MERGE_HEAD").exists()


def test_unknown_lock_identity_is_not_released(tmp_path: Path) -> None:
    root, state, _, _ = configured(tmp_path)
    owner = root / ".git/h2h-merge-workflow.lock/owner.json"
    document = json.loads(owner.read_text())
    document["token"] = "a" * 64
    owner.write_text(json.dumps(document))
    with pytest.raises(admission.AdmissionError, match="token"):
        admission.recover_abort(state)
    assert owner.exists()


def test_admission_initialization_failure_reclaims_only_its_own_new_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository"
    fixture_repository(root, succeeds=True)
    original = OSError("private state write failure")

    def fail_save(self: admission.Workflow, path: Path) -> None:
        raise original

    monkeypatch.setattr(admission.Workflow, "save", fail_save)
    with pytest.raises(OSError) as raised:
        admission.acquire(root, tmp_path / "workflow.json", os.getpid())
    assert raised.value is original
    assert not (root / ".git/h2h-merge-workflow.lock").exists()


@pytest.mark.skipif(
    os.name != "posix", reason="Exercises simultaneous POSIX workflow processes"
)
@pytest.mark.parametrize("same_task", [False, True])
def test_concurrent_workflow_cannot_abort_other_candidate(
    tmp_path: Path, same_task: bool
) -> None:
    task_a = tmp_path / "task-a"
    fixture_repository(task_a, succeeds=True)
    task_b = tmp_path / "task-b"
    git(task_a, "worktree", "add", "-b", "task-other", str(task_b), "HEAD")
    local_bin = task_b / ".venv/bin"
    local_bin.mkdir(parents=True)
    (local_bin / "python").symlink_to(sys.executable)
    (task_b / "other.py").write_text("value = 2\n")
    git(task_b, "add", "other.py")
    git(task_b, "commit", "-m", "test: second candidate")
    primary = tmp_path / "primary"
    git(task_a, "worktree", "add", str(primary), "master")
    local_bin = primary / ".venv/bin"
    local_bin.mkdir(parents=True)
    (local_bin / "python").symlink_to(sys.executable)
    state = tmp_path / "state"
    state.mkdir()
    binary = tmp_path / "bin"
    binary.mkdir()
    actual_git = shutil.which("git")
    assert actual_git
    write_python_executable(
        binary / "git",
        """import os, sys, time
from pathlib import Path
state = Path(os.environ['FIXTURE_STATE'])
if os.environ.get('FIXTURE_WORKFLOW') == 'B' and 'merge-base' in sys.argv and '--is-ancestor' in sys.argv and not (state / 'b-ready').exists():
    (state / 'b-ready').touch()
    deadline = time.monotonic() + 20
    while not (state / 'b-preflight-release').exists():
        if time.monotonic() >= deadline:
            raise SystemExit('barrier deadline exceeded')
        time.sleep(0.01)
"""
        + f"os.execv({actual_git!r}, [{actual_git!r}, *sys.argv[1:]])\n",
    )
    write_python_executable(
        binary / "codex",
        """import json, os, sys, time
from pathlib import Path
if sys.argv[1:] == ['--version']:
    print('codex-cli concurrent-workflow-fixture')
    raise SystemExit(0)
prompt = sys.stdin.read()
output = Path(sys.argv[sys.argv.index('--output-last-message') + 1])
state = Path(os.environ['FIXTURE_STATE'])
actor = os.environ['FIXTURE_WORKFLOW'].lower()
(state / (actor + '-review-held')).touch()
deadline = time.monotonic() + 25
while not (state / (actor + '-release')).exists():
    if time.monotonic() >= deadline:
        raise SystemExit('review barrier deadline exceeded')
    time.sleep(0.01)
"""
        + _PASS_REVIEW,
    )
    environment = os.environ | {
        "PATH": str(binary) + os.pathsep + os.environ["PATH"],
        "FIXTURE_STATE": str(state),
    }
    actors: list[subprocess.Popen[bytes]] = []
    observation: dict[str, object] = {"same_task": same_task}

    def await_marker(name: str, actor: subprocess.Popen[bytes]) -> None:
        deadline = time.monotonic() + 20
        while not (state / name).exists():
            assert actor.poll() is None, f"{name}: actor stopped; logs at {state}"
            assert time.monotonic() < deadline, f"{name}: barrier not reached"
            time.sleep(0.01)

    common = Path(
        git(task_a, "rev-parse", "--path-format=absolute", "--git-common-dir")
    )
    merge_head = Path(
        git(primary, "rev-parse", "--path-format=absolute", "--git-path", "MERGE_HEAD")
    )
    with (
        (state / "a-output.log").open("wb") as output_a,
        (state / "b-output.log").open("wb") as output_b,
    ):
        try:
            b = subprocess.Popen(
                ["scripts/git-flow-merge.sh"],
                cwd=task_b,
                env=environment | {"FIXTURE_WORKFLOW": "B"},
                stdout=output_b,
                stderr=subprocess.STDOUT,
            )
            actors.append(b)
            await_marker("b-ready", b)
            a = subprocess.Popen(
                ["scripts/git-flow-merge.sh"],
                cwd=task_b if same_task else task_a,
                env=environment | {"FIXTURE_WORKFLOW": "A"},
                stdout=output_a,
                stderr=subprocess.STDOUT,
            )
            actors.append(a)
            deadline = time.monotonic() + 20
            while a.poll() is None and not (state / "a-review-held").exists():
                assert time.monotonic() < deadline
                time.sleep(0.01)
            if (state / "a-review-held").exists():
                # OLD control: both workflows passed preflight. B's failed merge
                # must never revoke A's still-live candidate, even for same task.
                before = merge_head.read_bytes()
                (state / "b-preflight-release").touch()
                b.wait(timeout=20)
                observation.update(
                    old_window=True,
                    a_alive=a.poll() is None,
                    b_exit=b.returncode,
                    candidate_before=before.decode(),
                    candidate_after=merge_head.read_text()
                    if merge_head.exists()
                    else None,
                )
                assert a.poll() is None
                assert merge_head.exists(), (
                    f"losing workflow aborted the active candidate: {state}"
                )
                assert merge_head.read_bytes() == before
                pytest.fail(
                    "Overlapping workflow was admitted before the first completed"
                )
            assert a.returncode != 0
            assert "admission is already held" in (state / "a-output.log").read_text()
            assert not merge_head.exists()
            (state / "b-preflight-release").touch()
            await_marker("b-review-held", b)
            before = merge_head.read_bytes()
            index = git(primary, "write-tree")
            cleanup = subprocess.run(
                ["scripts/git-flow-merge.sh", "--cleanup-only"],
                cwd=task_b,
                env=environment | {"FIXTURE_WORKFLOW": "C"},
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            assert cleanup.returncode != 0
            assert "admission is already held" in cleanup.stderr
            assert merge_head.read_bytes() == before
            assert git(primary, "write-tree") == index
            assert b.poll() is None
            observation.update(
                old_window=False,
                a_exit=a.returncode,
                cleanup_exit=cleanup.returncode,
                active_candidate=before.decode(),
                candidate_unchanged=True,
            )
            (state / "b-release").touch()
            b.wait(timeout=20)
            assert b.returncode != 0
            assert (
                "untracked or ignored material" in (state / "b-output.log").read_text()
            )
            assert task_b.is_dir()
            assert (task_b / ".venv/bin/python").is_symlink()
            completed = git(primary, "rev-parse", "HEAD")
            assert git(primary, "show", "-s", "--format=%P", "HEAD").split()[-1] == git(
                task_b, "rev-parse", "HEAD"
            )
            assert completed != before.decode().strip()
            assert not merge_head.exists()
            assert not (common / "h2h-merge-workflow.lock").exists()
        finally:
            (state / "b-preflight-release").touch()
            (state / "b-release").touch()
            (state / "a-release").touch()
            for actor in actors:
                try:
                    actor.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    actor.terminate()
                    actor.wait(timeout=15)
            observation["actor_exit_codes"] = [actor.returncode for actor in actors]
            (state / "observation.json").write_text(
                json.dumps(observation, indent=2) + "\n"
            )


def divergent_candidate(
    tmp_path: Path, *, stage: str = "candidate"
) -> tuple[Path, Path, str]:
    root = tmp_path / "repository"
    fixture_repository(root, succeeds=True)
    git(root, "switch", "master")
    (root / "restore.txt").write_text("original tracked content\n")
    (root / "parent").mkdir()
    (root / "parent/child.txt").write_text("original child\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "test: shared tracked originals")
    git(root, "switch", "task")
    git(
        root,
        "-c",
        "core.hooksPath=/dev/null",
        "merge",
        "--no-ff",
        "--no-edit",
        "master",
    )
    git(root, "rm", "restore.txt", "parent/child.txt")
    (root / ".gitignore").write_text(".venv/\nrestore.txt\nparent\n")
    (root / "task-only.txt").write_text("task only\n")
    if stage == "conflict":
        (root / "README.md").write_text("task conflict\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "test: task removes tracked originals")
    task = git(root, "rev-parse", "HEAD")
    git(root, "switch", "master")
    (root / "primary-only.txt").write_text("primary only\n")
    if stage == "conflict":
        (root / "README.md").write_text("primary conflict\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "test: divergent primary")
    primary = git(root, "rev-parse", "HEAD")
    git(root, "switch", "task")
    state = tmp_path / "workflow.json"
    admission.acquire(root, state, os.getpid())
    admission.configure(
        state,
        worktree=root,
        primary_ref="refs/heads/master",
        primary_oid=primary,
        task_ref="refs/heads/task",
        task_oid=task,
    )
    git(root, "switch", "master")
    if stage == "conflict":
        # Recovery must also handle an actual conflict from configuration or
        # noncooperating changes after a clean preview. Seed only that phase;
        # Git still creates the real conflicting index and merge metadata.
        document = json.loads(state.read_text())
        document["phase"] = "merging"
        state.write_text(json.dumps(document))
    else:
        admission.begin(state, cleanup=False)
    merged = subprocess.run(
        ["git", "merge", "--no-ff", "--no-commit", task],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert merged.returncode == (1 if stage == "conflict" else 0), (
        merged.stdout + merged.stderr
    )
    admission.capture(state)
    return root, state, primary


def test_exact_candidate_restores_divergent_primary(tmp_path: Path) -> None:
    root, state, primary = divergent_candidate(tmp_path)
    admission.recover_abort(state)
    assert git(root, "rev-parse", "HEAD") == primary
    assert git(root, "status", "--porcelain") == ""
    assert (root / "restore.txt").read_text() == "original tracked content\n"
    assert (root / "parent/child.txt").read_text() == "original child\n"
    assert (root / "primary-only.txt").read_text() == "primary only\n"
    assert not (root / "task-only.txt").exists()
    assert not (root / ".git/AUTO_MERGE").exists()
    admission.recover_restore(state)
    admission.release(state)


@pytest.mark.parametrize("stage", ["candidate", "conflict"])
@pytest.mark.parametrize("obstruction", ["file", "ancestor", "directory", "case-alias"])
def test_recovery_preserves_ignored_obstructions(
    tmp_path: Path, obstruction: str, stage: str
) -> None:
    root, state, _ = divergent_candidate(tmp_path, stage=stage)
    if obstruction == "file":
        target = root / "restore.txt"
    elif obstruction == "ancestor":
        target = root / "parent"
    elif obstruction == "directory":
        (root / "restore.txt").mkdir()
        target = root / "restore.txt/keep.txt"
    else:
        target = root / "RESTORE.txt"
    target.write_text("user bytes must survive\n")
    if obstruction == "case-alias" and not (root / "restore.txt").exists():
        pytest.skip("Native filesystem does not alias this differently cased spelling")
    before = git(root, "ls-files", "--stage")
    with pytest.raises(admission.AdmissionError, match="obstruction"):
        admission.recover_abort(state)
    assert target.read_text() == "user bytes must survive\n"
    assert git(root, "ls-files", "--stage") == before
    assert (root / ".git/AUTO_MERGE").exists()
    assert (root / ".git/h2h-merge-workflow.lock").exists()


def test_actual_content_conflict_is_rejected_before_merge(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    fixture_repository(root, succeeds=True)
    (root / "README.md").write_text("task conflict\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "test: conflicting task")
    task = git(root, "rev-parse", "HEAD")
    git(root, "switch", "master")
    (root / "README.md").write_text("primary conflict\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "test: conflicting primary")
    primary = git(root, "rev-parse", "HEAD")
    git(root, "switch", "task")
    state = tmp_path / "workflow.json"
    admission.acquire(root, state, os.getpid())
    admission.configure(
        state,
        worktree=root,
        primary_ref="refs/heads/master",
        primary_oid=primary,
        task_ref="refs/heads/task",
        task_oid=task,
    )
    git(root, "switch", "master")
    before = git(root, "ls-files", "--stage")
    with pytest.raises(admission.AdmissionError, match="preview reports conflicts"):
        admission.begin(state, cleanup=False)
    assert not (root / ".git/MERGE_HEAD").exists()
    assert not (root / ".git/AUTO_MERGE").exists()
    assert git(root, "ls-files", "--stage") == before
    assert git(root, "rev-parse", "HEAD") == primary
    assert json.loads(state.read_text())["phase"] == "prepared"
    admission.recover_abort(state)
    admission.recover_restore(state)
    admission.release(state)
    assert git(root, "branch", "--show-current") == "task"
    assert (root / "README.md").read_text() == "task conflict\n"


@pytest.mark.parametrize("operation", ["MERGE_HEAD", "MERGE_AUTOSTASH"])
def test_committed_merge_preserves_foreign_operation(
    tmp_path: Path, operation: str
) -> None:
    root, state, _, task = configured(tmp_path)
    candidate(root, state, task)
    git(root, "-c", "core.hooksPath=/dev/null", "commit", "--no-edit")
    commit = git(root, "rev-parse", "HEAD")
    target = root / ".git" / operation
    target.write_text(commit + "\n")
    with pytest.raises(admission.AdmissionError, match=r"outside|Another"):
        admission.recover_abort(state)
    assert target.read_text() == commit + "\n"
    assert git(root, "rev-parse", "HEAD") == commit
    assert (root / ".git/h2h-merge-workflow.lock").exists()


def test_admission_initialization_cleanup_failure_retains_barrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository"
    fixture_repository(root, succeeds=True)
    failure = OSError("state write failed")
    lock = root / ".git/h2h-merge-workflow.lock"
    original_unlink = Path.unlink

    def fail_save(self: admission.Workflow, path: Path) -> None:
        raise failure

    def fail_owner_unlink(path: Path, missing_ok: bool = False) -> None:
        if path == lock / "owner.json":
            raise OSError("owner cleanup failed")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(admission.Workflow, "save", fail_save)
    monkeypatch.setattr(Path, "unlink", fail_owner_unlink)
    with pytest.raises(
        admission.AdmissionError, match="initialization and cleanup"
    ) as raised:
        admission.acquire(root, tmp_path / "state.json", os.getpid())
    assert raised.value.__cause__ is failure
    assert (lock / "owner.json").is_file()
    with pytest.raises(admission.AdmissionError, match="already held"):
        admission.acquire(root, tmp_path / "other.json", os.getpid())


def test_unproven_candidate_cannot_release_admission(tmp_path: Path) -> None:
    root, state, _, task = configured(tmp_path)
    candidate(root, state, task)
    with pytest.raises(admission.AdmissionError, match="outcome is unproven"):
        admission.release(state)
    assert (root / ".git/MERGE_HEAD").read_text().strip() == task
    assert (root / ".git/h2h-merge-workflow.lock").is_dir()


def test_finish_returns_the_verified_commit_not_a_later_primary(tmp_path: Path) -> None:
    root, state, _, task = configured(tmp_path)
    candidate(root, state, task)
    git(root, "-c", "core.hooksPath=/dev/null", "commit", "--no-edit")
    committed = git(root, "rev-parse", "HEAD")
    expected_cleanup_commit = admission.finish(state)
    git(
        root,
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "--allow-empty",
        "-m",
        "test: external primary advance",
    )
    assert expected_cleanup_commit == committed
    assert git(root, "rev-parse", "HEAD") != committed
    with pytest.raises(admission.AdmissionError, match="Completed merge state changed"):
        admission.recover_abort(state)
    assert (root / ".git/h2h-merge-workflow.lock").exists()


@pytest.mark.parametrize("task_ref_state", ["removed", "advanced"])
def test_committed_recovery_distinguishes_removed_task_from_ref_drift(
    tmp_path: Path, task_ref_state: str
) -> None:
    root, state, _, task = configured(tmp_path)
    candidate(root, state, task)
    git(root, "-c", "core.hooksPath=/dev/null", "commit", "--no-edit")
    committed = admission.finish(state)
    if task_ref_state == "removed":
        # The normal owned branch-deletion command may finish before a signal.
        git(root, "branch", "-d", "task")
        admission.recover_abort(state)
        admission.recover_restore(state)
        admission.release(state)
        assert not (root / ".git/h2h-merge-workflow.lock").exists()
    else:
        git(root, "update-ref", "refs/heads/task", committed, task)
        with pytest.raises(
            admission.AdmissionError, match="Completed merge state changed"
        ):
            admission.recover_abort(state)
        assert (root / ".git/h2h-merge-workflow.lock").exists()
    assert git(root, "rev-parse", "HEAD") == committed
    assert not (root / ".git/MERGE_HEAD").exists()


@pytest.mark.parametrize("task_ref_state", ["removed", "advanced"])
def test_noop_cleanup_recovery_distinguishes_removed_task_from_ref_drift(
    tmp_path: Path, task_ref_state: str
) -> None:
    root, state, base, task = configured(tmp_path)
    candidate(root, state, task)
    git(root, "-c", "core.hooksPath=/dev/null", "commit", "--no-edit")
    primary = admission.finish(state)
    admission.release(state)
    # A later no-op workflow cleans the already-integrated task branch.
    admission.acquire(root, state, os.getpid())
    admission.configure(
        state,
        worktree=root,
        primary_ref="refs/heads/master",
        primary_oid=primary,
        task_ref="refs/heads/task",
        task_oid=task,
    )
    admission.begin(state, cleanup=True)
    if task_ref_state == "removed":
        git(root, "branch", "-d", "task")
        admission.recover_abort(state)
        admission.recover_restore(state)
        admission.release(state)
        assert not (root / ".git/h2h-merge-workflow.lock").exists()
    else:
        git(root, "update-ref", "refs/heads/task", base, task)
        with pytest.raises(admission.AdmissionError, match="Cleanup checkout changed"):
            admission.recover_abort(state)
        assert (root / ".git/h2h-merge-workflow.lock").exists()
    assert git(root, "rev-parse", "HEAD") == primary
    assert not (root / ".git/MERGE_HEAD").exists()


@pytest.mark.parametrize("phase", ["prepared", "aborted"])
@pytest.mark.parametrize("obstruction", ["file", "directory"])
def test_recovery_switch_preserves_ignored_private_task_targets(
    tmp_path: Path, phase: str, obstruction: str
) -> None:
    root = tmp_path / "repository"
    primary = fixture_repository(root, succeeds=True)
    tracked = root / "private.txt"
    tracked.write_bytes(b"tracked task content\n")
    git(root, "add", "private.txt")
    git(root, "commit", "-m", "test: task restoration destination")
    task = git(root, "rev-parse", "HEAD")
    state = tmp_path / "workflow.json"
    admission.acquire(root, state, os.getpid())
    admission.configure(
        state,
        worktree=root,
        primary_ref="refs/heads/master",
        primary_oid=primary,
        task_ref="refs/heads/task",
        task_oid=task,
    )
    git(root, "switch", "master")
    if phase == "aborted":
        admission.begin(state, cleanup=False)
        git(root, "merge", "--no-ff", "--no-commit", task)
        admission.capture(state)
        admission.recover_abort(state)
    # Ignored files do not appear in the clean-primary snapshot. Recovery must
    # still ask Git to protect them when switching back to the task's tree.
    (root / ".git/info/exclude").write_text("private.txt\n")
    private = tracked
    if obstruction == "directory":
        tracked.mkdir()
        private = tracked / "private-child.bin"
    content = b"private user bytes\x00must survive\n"
    private.write_bytes(content)
    private_path = private.relative_to(root).as_posix()
    assert git(root, "check-ignore", private_path) == private_path
    assert git(root, "status", "--porcelain") == ""
    before_index = git(root, "ls-files", "--stage")
    before_state = state.read_bytes()
    owner = root / ".git/h2h-merge-workflow.lock/owner.json"
    before_owner = owner.read_bytes()
    identity = owner.parent.stat()

    with pytest.raises(admission.AdmissionError, match="obstruction"):
        admission.recover_restore(state)

    assert private.read_bytes() == content
    assert git(root, "branch", "--show-current") == "master"
    assert git(root, "rev-parse", "HEAD") == primary
    assert git(root, "rev-parse", "refs/heads/master") == primary
    assert git(root, "rev-parse", "refs/heads/task") == task
    assert git(root, "ls-files", "--stage") == before_index
    assert git(root, "status", "--porcelain") == ""
    assert not (root / ".git/MERGE_HEAD").exists()
    assert state.read_bytes() == before_state
    assert json.loads(before_state)["phase"] == phase
    assert owner.read_bytes() == before_owner
    after_identity = owner.parent.stat()
    assert (after_identity.st_dev, after_identity.st_ino) == (
        identity.st_dev,
        identity.st_ino,
    )


@pytest.mark.parametrize("phase", ["prepared", "aborted"])
def test_named_primary_recovery_preserves_captured_entry_checkout(
    tmp_path: Path, phase: str
) -> None:
    root = tmp_path / "repository"
    primary = fixture_repository(root, succeeds=True)
    task = git(root, "rev-parse", "HEAD")
    git(root, "switch", "master")
    state = tmp_path / "workflow.json"
    admission.acquire(root, state, os.getpid())
    admission.configure(
        state,
        worktree=root,
        primary_ref="refs/heads/master",
        primary_oid=primary,
        task_ref="refs/heads/task",
        task_oid=task,
    )
    assert json.loads(state.read_text())["entry_ref"] == "refs/heads/master"
    if phase == "aborted":
        admission.begin(state, cleanup=False)
        git(root, "merge", "--no-ff", "--no-commit", task)
        admission.capture(state)
    admission.recover_abort(state)
    admission.recover_restore(state)
    admission.release(state)
    assert git(root, "branch", "--show-current") == "master"
    assert git(root, "rev-parse", "HEAD") == primary
    assert git(root, "rev-parse", "task") == task
    assert not (root / ".git/MERGE_HEAD").exists()


@pytest.mark.parametrize(
    "obstruction",
    ["file", "directory", "empty-directory", "symlink", "ancestor", "ancestor-symlink"],
)
def test_merge_begin_preserves_incoming_ignored_destinations(
    tmp_path: Path, obstruction: str
) -> None:
    root = tmp_path / "repository"
    primary = fixture_repository(root, succeeds=True)
    ancestor = obstruction.startswith("ancestor")
    target = root / "incoming"
    if ancestor:
        target.mkdir()
        (target / "child").write_text("task destination\n")
    else:
        target.write_text("task destination\n")
    git(root, "add", "incoming")
    git(root, "commit", "-m", "test: incoming task destination")
    task = git(root, "rev-parse", "HEAD")
    state = tmp_path / "workflow.json"
    admission.acquire(root, state, os.getpid())
    admission.configure(
        state,
        worktree=root,
        primary_ref="refs/heads/master",
        primary_oid=primary,
        task_ref="refs/heads/task",
        task_oid=task,
    )
    git(root, "switch", "master")
    (root / ".git/info/exclude").write_text("incoming\n")
    outside = tmp_path / "external-private"
    outside.mkdir()
    content = b"private ignored user bytes\x00\n"
    (outside / "child").write_bytes(content)
    private = target
    if obstruction in {"directory", "empty-directory"}:
        target.mkdir()
        if obstruction == "directory":
            private = target / "child"
            private.write_bytes(content)
    elif "symlink" in obstruction:
        target.symlink_to(outside, target_is_directory=True)
    else:
        private.write_bytes(content)
    before = state.read_bytes()
    index = git(root, "ls-files", "--stage")
    owner = root / ".git/h2h-merge-workflow.lock/owner.json"
    owner_bytes = owner.read_bytes()
    identity = target.lstat()
    assert git(root, "status", "--porcelain") == ""
    with pytest.raises(admission.AdmissionError, match="obstruction"):
        admission.begin(state, cleanup=False)
    assert state.read_bytes() == before
    assert owner.read_bytes() == owner_bytes
    assert (target.lstat().st_dev, target.lstat().st_ino) == (
        identity.st_dev,
        identity.st_ino,
    )
    if obstruction == "empty-directory":
        assert list(target.iterdir()) == []
    elif "symlink" in obstruction:
        assert target.readlink() == outside
    else:
        assert private.read_bytes() == content
    assert (outside / "child").read_bytes() == content
    assert git(root, "ls-files", "--stage") == index
    assert git(root, "rev-parse", "HEAD") == primary
    assert git(root, "rev-parse", "task") == task
    assert not (root / ".git/MERGE_HEAD").exists()


def test_merge_begin_allows_native_tracked_case_only_rename(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    fixture_repository(root, succeeds=True)
    git(root, "switch", "master")
    (root / "Foo").write_text("tracked original\n")
    if not (root / "foo").exists():
        pytest.skip("Native filesystem is case-sensitive; no case alias exists")
    git(root, "add", "Foo")
    git(root, "commit", "-m", "test: tracked original spelling")
    primary = git(root, "rev-parse", "HEAD")
    git(root, "switch", "task")
    git(
        root,
        "-c",
        "core.hooksPath=/dev/null",
        "merge",
        "--no-ff",
        "--no-edit",
        "master",
    )
    git(root, "mv", "Foo", "temporary-name")
    git(root, "mv", "temporary-name", "foo")
    git(root, "commit", "-m", "test: tracked case-only rename")
    task = git(root, "rev-parse", "HEAD")
    state = tmp_path / "workflow.json"
    admission.acquire(root, state, os.getpid())
    admission.configure(
        state,
        worktree=root,
        primary_ref="refs/heads/master",
        primary_oid=primary,
        task_ref="refs/heads/task",
        task_oid=task,
    )
    git(root, "switch", "master")
    assert "Foo" in git(root, "ls-files").splitlines()
    admission.begin(state, cleanup=False)
    git(root, "merge", "--no-ff", "--no-commit", task)
    assert (root / "foo").read_text() == "tracked original\n"
    assert "foo" in git(root, "ls-files").splitlines()
    admission.capture(state)
    admission.recover_abort(state)
    admission.recover_restore(state)
    admission.release(state)


def test_checkout_guard_does_not_treat_distinct_hardlink_as_tracked(
    tmp_path: Path,
) -> None:
    root, state, _, _ = configured(tmp_path)
    source = root / "README.md"
    target = root / "private-hardlink"
    os.link(source, target)
    git(root, "add", "private-hardlink")
    git(root, "commit", "-m", "test: incoming hardlink destination")
    incoming = git(root, "rev-parse", "HEAD")
    git(root, "rm", "--cached", "private-hardlink")
    assert target.stat().st_ino == source.stat().st_ino
    with pytest.raises(admission.AdmissionError, match="obstruction"):
        admission._protect_checkout_paths(admission._load(state), target_tree=incoming)
    assert target.read_bytes() == source.read_bytes()


def test_checkout_guard_never_follows_tracked_symlink_ancestor(tmp_path: Path) -> None:
    root, state, _, _ = configured(tmp_path)
    outside = tmp_path / "private-directory"
    outside.mkdir()
    (outside / "child").write_text("private external data\n")
    path = root / "transition"
    path.mkdir()
    (path / "child").write_text("tracked incoming child\n")
    git(root, "add", "transition")
    git(root, "commit", "-m", "test: incoming directory")
    incoming = git(root, "rev-parse", "HEAD")
    git(root, "rm", "-r", "transition")
    path.symlink_to(outside, target_is_directory=True)
    git(root, "add", "transition")
    admission._protect_checkout_paths(admission._load(state), target_tree=incoming)
    assert path.readlink() == outside
    assert (outside / "child").read_text() == "private external data\n"


def removable_worktree(
    tmp_path: Path, *, phase: str = "committed"
) -> tuple[Path, Path, Path, str, str]:
    root, state, primary, task = configured(tmp_path)
    candidate(root, state, task)
    git(root, "-c", "core.hooksPath=/dev/null", "commit", "--no-edit")
    committed = admission.finish(state)
    linked = tmp_path / "linked-task"
    git(root, "worktree", "add", str(linked), "task")
    if phase == "cleanup":
        admission.release(state)
        admission.acquire(root, state, os.getpid())
        admission.configure(
            state,
            worktree=root,
            primary_ref="refs/heads/master",
            primary_oid=committed,
            task_ref="refs/heads/task",
            task_oid=task,
        )
        admission.begin(state, cleanup=True)
    return root, state, linked, primary, task


@pytest.mark.parametrize("phase", ["cleanup", "committed"])
def test_owned_clean_integrated_worktree_can_be_removed(
    tmp_path: Path, phase: str
) -> None:
    root, state, linked, _, task = removable_worktree(tmp_path, phase=phase)
    committed = git(root, "rev-parse", "HEAD")
    admission.remove_task_worktree(state, linked)
    assert not linked.exists()
    assert git(root, "rev-parse", "HEAD") == committed
    assert git(root, "rev-parse", "task") == task
    admission.recover_abort(state)
    admission.recover_restore(state)
    admission.release(state)


@pytest.mark.parametrize(
    "material",
    [
        "file",
        "empty-directory",
        "nested-empty-directory",
        "symlink",
        "venv",
        "unignored",
    ],
)
def test_worktree_removal_preserves_all_private_material(
    tmp_path: Path, material: str
) -> None:
    root, state, linked, _, task = removable_worktree(tmp_path)
    (root / ".git/info/exclude").write_text("private\n")
    target = linked / (".venv" if material == "venv" else "private")
    outside = tmp_path / "outside"
    outside.write_bytes(b"private outside bytes\x00")
    if material in {"empty-directory", "venv"}:
        target.mkdir()
    elif material == "nested-empty-directory":
        (target / "nested").mkdir(parents=True)
    elif material == "symlink":
        target.symlink_to(outside)
    else:
        target.write_bytes(b"private bytes\x00")
        if material == "unignored":
            (root / ".git/info/exclude").write_text("")
    before = state.read_bytes()
    identity = target.lstat()
    with pytest.raises(
        admission.AdmissionError, match=r"material|changed before removal"
    ):
        admission.remove_task_worktree(state, linked)
    assert state.read_bytes() == before
    assert linked.is_dir()
    assert (target.lstat().st_dev, target.lstat().st_ino) == (
        identity.st_dev,
        identity.st_ino,
    )
    if material in {"file", "unignored"}:
        assert target.read_bytes() == b"private bytes\x00"
    elif material == "symlink":
        assert target.readlink() == outside
    elif material == "nested-empty-directory":
        assert list((target / "nested").iterdir()) == []
    else:
        assert list(target.iterdir()) == []
    assert outside.read_bytes() == b"private outside bytes\x00"
    assert git(root, "rev-parse", "task") == task
    assert (root / ".git/h2h-merge-workflow.lock/owner.json").is_file()


@pytest.mark.parametrize(
    "drift",
    ["primary", "task-ref", "foreign", "wrong-registration", "dirty", "operation"],
)
def test_worktree_removal_requires_exact_repository_refs_and_registration(
    tmp_path: Path, drift: str
) -> None:
    root, state, linked, primary, task = removable_worktree(tmp_path)
    target = linked
    if drift == "primary":
        target = root
    elif drift == "task-ref":
        git(root, "update-ref", "refs/heads/task", primary, task)
    elif drift == "foreign":
        preserved = tmp_path / "preserved-original-linked"
        linked.rename(preserved)
        fixture_repository(linked, succeeds=True)
    elif drift == "wrong-registration":
        target = tmp_path / "different-linked"
        git(root, "worktree", "add", "-b", "different", str(target), "HEAD")
    elif drift == "dirty":
        (linked / "README.md").write_text("user tracked changes\n")
    else:
        git_path = Path(
            git(
                linked,
                "rev-parse",
                "--path-format=absolute",
                "--git-path",
                "CHERRY_PICK_HEAD",
            )
        )
        git_path.write_text(task + "\n")
    before = state.read_bytes()
    with pytest.raises(admission.AdmissionError):
        admission.remove_task_worktree(state, target)
    assert state.read_bytes() == before
    assert target.is_dir()
    assert root.is_dir()
    assert linked.is_dir()
    assert (root / ".git/h2h-merge-workflow.lock/owner.json").is_file()
    if drift == "dirty":
        assert (linked / "README.md").read_text() == "user tracked changes\n"


def test_worktree_removal_rejects_unmerged_task(tmp_path: Path) -> None:
    root, state, primary, task = configured(tmp_path)
    git(root, "switch", "master")
    linked = tmp_path / "linked-task"
    git(root, "worktree", "add", str(linked), "task")
    admission.begin(state, cleanup=True)
    with pytest.raises(admission.AdmissionError, match="not integrated"):
        admission.remove_task_worktree(state, linked)
    assert linked.is_dir()
    assert git(root, "rev-parse", "master") == primary
    assert git(root, "rev-parse", "task") == task


@pytest.mark.parametrize("private", [False, True])
def test_merge_preview_protects_directory_rename_output(
    tmp_path: Path, private: bool
) -> None:
    root = tmp_path / "repository"
    fixture_repository(root, succeeds=True)
    git(root, "config", "merge.directoryRenames", "true")
    git(root, "switch", "master")
    (root / "dir").mkdir()
    (root / "dir/a").write_text("original shared file\n")
    git(root, "add", "dir/a")
    git(root, "commit", "-m", "test: shared directory")
    git(root, "switch", "task")
    git(
        root,
        "-c",
        "core.hooksPath=/dev/null",
        "merge",
        "--no-ff",
        "--no-edit",
        "master",
    )
    (root / "dir/new").write_text("task new file\n")
    git(root, "add", "dir/new")
    git(root, "commit", "-m", "test: task file in old directory")
    task = git(root, "rev-parse", "HEAD")
    git(root, "switch", "master")
    git(root, "mv", "dir", "renamed")
    git(root, "commit", "-m", "test: primary directory rename")
    primary = git(root, "rev-parse", "HEAD")
    assert (
        "renamed/new"
        not in git(root, "ls-tree", "-r", "--name-only", task).splitlines()
    )
    assert (
        "renamed/new"
        not in git(root, "ls-tree", "-r", "--name-only", primary).splitlines()
    )
    state = tmp_path / "workflow.json"
    admission.acquire(root, state, os.getpid())
    admission.configure(
        state,
        worktree=root,
        primary_ref="refs/heads/master",
        primary_oid=primary,
        task_ref="refs/heads/task",
        task_oid=task,
    )
    (root / ".git/info/exclude").write_text("renamed/new\n")
    destination = root / "renamed/new"
    if private:
        destination.write_bytes(b"private output destination\x00")
        before = state.read_bytes()
        index = git(root, "ls-files", "--stage")
        with pytest.raises(admission.AdmissionError, match="obstruction"):
            admission.begin(state, cleanup=False)
        assert destination.read_bytes() == b"private output destination\x00"
        assert state.read_bytes() == before
        assert git(root, "ls-files", "--stage") == index
        assert not (root / ".git/MERGE_HEAD").exists()
    else:
        admission.begin(state, cleanup=False)
        git(root, "merge", "-s", "ort", "--no-ff", "--no-commit", task)
        assert destination.read_text() == "task new file\n"
        admission.capture(state)
    admission.recover_abort(state)
    admission.recover_restore(state)
    admission.release(state)
    assert git(root, "rev-parse", "HEAD") == primary


@pytest.mark.parametrize("option", ["", "--no-ff"])
def test_merge_preview_accepts_single_canonical_option(
    tmp_path: Path, option: str
) -> None:
    root, state, primary, task = configured(tmp_path)
    git(root, "switch", "master")
    # Empty is neutral; install-git-hooks.sh installs the sole --no-ff option.
    git(root, "config", "branch.master.mergeOptions", option)

    admission.begin(state, cleanup=False)

    assert json.loads(state.read_text())["phase"] == "merging"
    assert git(root, "config", "branch.master.mergeOptions") == option
    git(
        root,
        "-c",
        "branch.master.mergeOptions=",
        "merge",
        "-s",
        "ort",
        "--no-ff",
        "--no-commit",
        task,
    )
    assert (root / ".git/MERGE_HEAD").read_text().strip() == task
    assert git(root, "write-tree") == git(root, "rev-parse", task + "^{tree}")
    admission.capture(state)
    admission.recover_abort(state)
    admission.recover_restore(state)
    admission.release(state)
    assert git(root, "rev-parse", "HEAD") == task
    assert git(root, "rev-parse", "master") == primary
    assert git(root, "config", "branch.master.mergeOptions") == option


@pytest.mark.parametrize(
    "options",
    [
        ["-Xfind-renames=1%"],
        ["--no-ff -Xfind-renames=1%"],
        ["--no-ff", "--no-ff"],
        ["", ""],
        ["", "--no-ff"],
        ["--no-ff", ""],
    ],
)
def test_merge_preview_rejects_custom_branch_options_without_changing_config(
    tmp_path: Path,
    options: list[str],
) -> None:
    root, state, primary, task = configured(tmp_path)
    git(root, "switch", "master")
    for option in options:
        git(root, "config", "--add", "branch.master.mergeOptions", option)
    before = state.read_bytes()
    with pytest.raises(admission.AdmissionError, match="custom branch mergeOptions"):
        admission.begin(state, cleanup=False)
    assert subprocess.check_output(
        ("git", "config", "--null", "--get-all", "branch.master.mergeOptions"),
        cwd=root,
    ) == b"".join(option.encode() + b"\0" for option in options)
    assert state.read_bytes() == before
    assert git(root, "rev-parse", "HEAD") == primary
    assert git(root, "rev-parse", "task") == task
    assert not (root / ".git/MERGE_HEAD").exists()


@pytest.mark.parametrize("failure", ["unsupported", "incomplete", "not-tree"])
def test_merge_preview_rejects_failed_or_malformed_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    root, state, primary, task = configured(tmp_path)
    git(root, "switch", "master")
    original = subprocess.run

    def run(
        command: tuple[str, ...], *, stdout: BinaryIO, stderr: BinaryIO, check: bool
    ) -> subprocess.CompletedProcess[bytes]:
        if "merge-tree" in command:
            if failure == "unsupported":
                os.write(stderr.fileno(), b"unknown option write-tree")
                return subprocess.CompletedProcess(command, 129)
            output = primary.encode() + (
                b"\0" if failure == "not-tree" else b"unterminated"
            )
            os.write(stdout.fileno(), output)
            return subprocess.CompletedProcess(command, 0)
        return original(command, stdout=stdout, stderr=stderr, check=check)

    before = state.read_bytes()
    expected_error = {
        "unsupported": "unknown option write-tree",
        "incomplete": "did not return one complete tree object",
        "not-tree": "result is not a tree object",
    }[failure]
    with monkeypatch.context() as context:
        context.setattr(subprocess, "run", run)
        with pytest.raises(admission.AdmissionError, match=expected_error):
            admission.begin(state, cleanup=False)
    assert state.read_bytes() == before
    assert git(root, "rev-parse", "HEAD") == primary
    assert git(root, "rev-parse", "task") == task
    assert not (root / ".git/MERGE_HEAD").exists()


def test_native_conflicted_candidate_without_obstruction_aborts_and_restores_task(
    tmp_path: Path,
) -> None:
    root, state, primary = divergent_candidate(tmp_path, stage="conflict")
    task = git(root, "rev-parse", "task")
    assert git(root, "ls-files", "--unmerged")
    assert (root / ".git/MERGE_HEAD").read_text().strip() == task
    document = json.loads(state.read_text())
    assert document["phase"] == "candidate"
    assert document["candidate"]["tree"] is None
    assert document["entry_ref"] == "refs/heads/task"

    admission.recover_abort(state)

    assert git(root, "branch", "--show-current") == "master"
    assert git(root, "rev-parse", "HEAD") == primary
    assert git(root, "rev-parse", "master") == primary
    assert git(root, "rev-parse", "task") == task
    assert not git(root, "ls-files", "--unmerged")
    assert git(root, "write-tree") == git(root, "rev-parse", primary + "^{tree}")
    assert git(root, "status", "--porcelain") == ""
    assert (root / "README.md").read_text() == "primary conflict\n"
    assert (root / "restore.txt").read_text() == "original tracked content\n"
    assert (root / "parent/child.txt").read_text() == "original child\n"
    assert not (root / ".git/MERGE_HEAD").exists()
    assert not (root / ".git/AUTO_MERGE").exists()
    assert json.loads(state.read_text())["phase"] == "aborted"

    admission.recover_restore(state)
    admission.release(state)

    assert git(root, "branch", "--show-current") == "task"
    assert git(root, "rev-parse", "HEAD") == task
    assert git(root, "rev-parse", "master") == primary
    assert git(root, "write-tree") == git(root, "rev-parse", task + "^{tree}")
    assert git(root, "status", "--porcelain") == ""
    assert (root / "README.md").read_text() == "task conflict\n"
    assert not (root / ".git/h2h-merge-workflow.lock").exists()
    assert not state.exists()


def _initialized_gitlink(tmp_path: Path) -> tuple[Path, str]:
    origin = tmp_path / "module-source"
    origin.mkdir()
    git(origin, "init", "-b", "master")
    git(origin, "config", "user.name", "Submodule Test")
    git(origin, "config", "user.email", "submodule@example.invalid")
    (origin / "source.txt").write_text("committed module source\n")
    (origin / ".gitignore").write_text("private.bin\n")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "test: module source")
    root = tmp_path / "repository"
    root.mkdir()
    git(root, "init", "-b", "master")
    git(root, "config", "user.name", "Submodule Test")
    git(root, "config", "user.email", "submodule@example.invalid")
    git(
        root,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(origin),
        "module",
    )
    (root / "README.md").write_text("baseline\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "test: initialized module")
    primary = git(root, "rev-parse", "HEAD")
    git(root, "switch", "-c", "task")
    (root / "README.md").write_text("task documentation\n")
    git(root, "add", "README.md")
    git(root, "commit", "-m", "docs: task with unchanged module")
    return root, primary


def _configure_gitlink(
    root: Path, state: Path, primary: str, *, repository: Path | None = None
) -> None:
    admission.acquire(repository or root, state, os.getpid())
    admission.configure(
        state,
        worktree=root,
        primary_ref="refs/heads/master",
        primary_oid=primary,
        task_ref="refs/heads/task",
        task_oid=git(root, "rev-parse", "task"),
    )


@pytest.mark.parametrize("linked", [False, True])
def test_unchanged_initialized_gitlink_allows_primary_switch_and_merge_preview(
    tmp_path: Path, linked: bool
) -> None:
    root, primary = _initialized_gitlink(tmp_path)
    state = tmp_path / "workflow.json"
    module = root / "module"
    before = {path.name: path.read_bytes() for path in module.iterdir()}
    module_head = git(module, "rev-parse", "HEAD")
    caller = root
    if linked:
        git(root, "switch", "master")
        caller = tmp_path / "linked-task"
        git(root, "worktree", "add", str(caller), "task")
    _configure_gitlink(root, state, primary, repository=caller)
    assert git(root, "status", "--porcelain") == ""
    if not linked:
        admission.switch_primary(state)
    admission.begin(state, cleanup=False)
    assert git(root, "branch", "--show-current") == "master"
    assert json.loads(state.read_text())["phase"] == "merging"
    assert git(module, "rev-parse", "HEAD") == module_head
    assert {path.name: path.read_bytes() for path in module.iterdir()} == before
    assert not (root / ".git/MERGE_HEAD").exists()
    admission.recover_abort(state)
    admission.recover_restore(state)
    admission.release(state)


@pytest.mark.parametrize("replacement", ["commit", "file", "directory", "removed"])
@pytest.mark.parametrize(
    "material", ["tracked-dirty", "untracked", "ignored", "empty-directory", "symlink"]
)
def test_changed_gitlink_preserves_nested_private_material(
    tmp_path: Path, replacement: str, material: str
) -> None:
    root, primary = _initialized_gitlink(tmp_path)
    module = root / "module"
    if replacement == "commit":
        origin = tmp_path / "module-source"
        (origin / "source.txt").write_text("next module revision\n")
        git(origin, "add", "source.txt")
        git(origin, "commit", "-m", "test: changed gitlink revision")
        incoming = git(origin, "rev-parse", "HEAD")
        mode, destination = "160000", "module"
    else:
        payload = tmp_path / "replacement.txt"
        payload.write_text("incoming superproject content\n")
        incoming = git(root, "hash-object", "-w", str(payload))
        mode = "100644"
        destination = "module/new.txt" if replacement == "directory" else "module"
    git(root, "update-index", "--force-remove", "module")
    if replacement != "removed":
        git(root, "update-index", "--add", "--cacheinfo", mode, incoming, destination)
    target_tree = git(root, "write-tree")
    git(root, "read-tree", "HEAD")
    private = module / ("source.txt" if material == "tracked-dirty" else "private.bin")
    if material == "empty-directory":
        private.mkdir()
    elif material == "symlink":
        private.symlink_to("../outside-private")
    else:
        if material == "untracked":
            private = module / "untracked.bin"
        private.write_bytes(b"private nested bytes\x00\n")
    state = tmp_path / "workflow.json"
    _configure_gitlink(root, state, primary)
    index = git(root, "ls-files", "--stage")
    before = state.read_bytes()
    module_git = (module / ".git").read_bytes()
    with pytest.raises(admission.AdmissionError, match="obstruction"):
        admission._protect_checkout_paths(
            admission._load(state), target_tree=target_tree
        )
    assert git(root, "ls-files", "--stage") == index
    assert state.read_bytes() == before
    assert (module / ".git").read_bytes() == module_git
    if material == "empty-directory":
        assert private.is_dir() and not list(private.iterdir())
    elif material == "symlink":
        assert private.is_symlink() and os.readlink(private) == "../outside-private"
    else:
        assert private.read_bytes() == b"private nested bytes\x00\n"
    assert (root / ".git/h2h-merge-workflow.lock/owner.json").is_file()
    assert not (root / ".git/MERGE_HEAD").exists()


def test_unmerged_gitlink_stage_is_not_an_unchanged_checkout(
    tmp_path: Path,
) -> None:
    root, primary = _initialized_gitlink(tmp_path)
    state = tmp_path / "workflow.json"
    _configure_gitlink(root, state, primary)
    target_tree = git(root, "rev-parse", "HEAD^{tree}")
    oid = git(root / "module", "rev-parse", "HEAD")
    git(root, "update-index", "--force-remove", "module")
    subprocess.run(
        ("git", "-C", str(root), "update-index", "--index-info"),
        input=f"160000 {oid} 1\tmodule\n",
        text=True,
        check=True,
    )
    index = git(root, "ls-files", "--stage")
    with pytest.raises(admission.AdmissionError, match="obstruction"):
        admission._protect_checkout_paths(
            admission._load(state), target_tree=target_tree
        )
    assert git(root, "ls-files", "--stage") == index
    assert (root / "module/source.txt").read_text() == "committed module source\n"


@pytest.mark.parametrize("stage", ["candidate", "conflict"])
def test_capture_exports_only_a_mergeable_owned_tree(
    tmp_path: Path, stage: str
) -> None:
    root, state, primary = divergent_candidate(tmp_path, stage=stage)
    document = json.loads(state.read_text())
    document["phase"] = "merging"
    state.write_text(json.dumps(document))
    assert admission.main(["capture", "--state", str(state)]) == 0
    tree_file = state.with_suffix(".tree")
    if stage == "candidate":
        assert tree_file.read_text().strip() == git(root, "write-tree")
        tree_file.unlink()
    else:
        assert not tree_file.exists()
        assert json.loads(state.read_text())["candidate"]["tree"] is None
    admission.recover_abort(state)
    assert not tree_file.exists()
    assert git(root, "rev-parse", "HEAD") == primary
    assert not (root / ".git/MERGE_HEAD").exists()
    admission.recover_restore(state)
    admission.release(state)


def test_git_capture_reads_fixed_extents_before_callback_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_temporary_file = tempfile.TemporaryFile
    streams: list[BinaryIO] = []
    requested_sizes: list[int] = []
    expected_stdout = b"query result\n"
    expected_stderr = b"query diagnostic\n"

    @contextmanager
    def growing_spool() -> Iterator[BinaryIO]:
        with original_temporary_file() as stream:
            streams.append(stream)
            original_read = stream.read

            def append_then_read(size: int = -1) -> bytes:
                requested_sizes.append(size)
                position = stream.tell()
                stream.seek(0, os.SEEK_END)
                stream.write(b"late callback output\n")
                stream.seek(position)
                return original_read(size)

            monkeypatch.setattr(stream, "read", append_then_read)
            yield stream

    def completed_git(
        command: tuple[str, ...], *, stdout: BinaryIO, stderr: BinaryIO, check: bool
    ) -> subprocess.CompletedProcess[bytes]:
        assert not check
        stdout.write(expected_stdout)
        stderr.write(expected_stderr)
        stdout.flush()
        stderr.flush()
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(tempfile, "TemporaryFile", growing_spool)
    monkeypatch.setattr(subprocess, "run", completed_git)
    result = admission._capture_git(tmp_path, "status", "--porcelain=v1")
    assert result.stdout == expected_stdout
    assert result.stderr == expected_stderr
    assert requested_sizes == [len(expected_stdout), len(expected_stderr)]
    assert len(streams) == 2 and all(stream.closed for stream in streams)
