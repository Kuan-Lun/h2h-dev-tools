"""Candidate checks reject Git flags which hide tracked working-tree changes."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from h2h_dev_tools import git_index
from h2h_dev_tools import merge_admission as admission


def git(root: Path, *arguments: str) -> bytes:
    return subprocess.run(
        ("git", "-C", str(root), *arguments),
        capture_output=True,
        check=True,
        timeout=15,
    ).stdout


def oid(root: Path, revision: str) -> str:
    return git(root, "rev-parse", revision).decode("ascii").strip()


def repository(root: Path) -> Path:
    root.mkdir()
    git(root, "init", "--initial-branch=master")
    git(root, "config", "user.name", "Git index fixture")
    git(root, "config", "user.email", "git-index@example.invalid")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "config", "core.autocrlf", "false")
    (root / "tracked.txt").write_text("original source\n")
    git(root, "add", "tracked.txt")
    git(root, "commit", "-m", "test: create tracked source")
    return root


def mark_hidden(root: Path, flag: str, name: str = "tracked.txt") -> None:
    for option in flag.split("+"):
        git(root, "update-index", "--" + option, "--", name)


@pytest.mark.parametrize(
    "flag", ["assume-unchanged", "skip-worktree", "assume-unchanged+skip-worktree"]
)
@pytest.mark.parametrize("name", ["tracked.txt", " ", "\t", "\n"])
def test_native_index_flags_cannot_hide_working_tree_changes(
    tmp_path: Path, flag: str, name: str
) -> None:
    if os.name == "nt" and name in {" ", "\t", "\n"}:
        pytest.skip("Native Windows paths cannot represent these Git path names")
    root = repository(tmp_path / "repository")
    if name != "tracked.txt":
        (root / name).write_text("original source\n")
        git(root, "add", "--", name)
        git(root, "commit", "-m", "test: add unusual source path")
    mark_hidden(root, flag, name)
    (root / name).write_text("hidden local changes\n")
    assert git(root, "diff", "--name-only", "-z") == b""
    before = (root / ".git/index").read_bytes()
    inventory = git(root, "ls-files", "-v", "-z")

    with pytest.raises(
        git_index.GitIndexError, match="assume-unchanged or skip-worktree"
    ):
        git_index.assert_index_visible(lambda *args: git(root, *args))

    assert (root / ".git/index").read_bytes() == before
    assert git(root, "ls-files", "-v", "-z") == inventory
    assert (root / name).read_text() == "hidden local changes\n"


@pytest.mark.skipif(os.name != "posix", reason="Raw non-UTF-8 POSIX path bytes")
def test_native_index_rejection_preserves_non_utf8_path_bytes(tmp_path: Path) -> None:
    root = repository(tmp_path / "repository")
    name = os.fsdecode(b"source-\xff\n")
    # Git can contain names which the current filesystem cannot materialize.
    # Build the real index record directly, preserving its raw NUL-delimited
    # path; this still exercises Git's own -v inventory and hidden-entry flag.
    blob = git(root, "rev-parse", "HEAD:tracked.txt").removesuffix(b"\n")
    subprocess.run(
        ("git", "-C", str(root), "update-index", "-z", "--index-info"),
        input=b"100644 " + blob + b"\t" + os.fsencode(name) + b"\0",
        capture_output=True,
        check=True,
        timeout=15,
    )
    mark_hidden(root, "assume-unchanged", name)
    before = (root / ".git/index").read_bytes()
    with pytest.raises(git_index.GitIndexError) as error:
        git_index.assert_index_visible(lambda *args: git(root, *args))
    assert repr(os.fsencode(name)) in str(error.value)
    assert (root / ".git/index").read_bytes() == before


def test_native_visible_index_and_empty_repository_are_accepted(tmp_path: Path) -> None:
    root = repository(tmp_path / "repository")
    git_index.assert_index_visible(lambda *args: git(root, *args))
    (root / "tracked.txt").write_text("ordinary visible edit\n")
    git_index.assert_index_visible(lambda *args: git(root, *args))
    assert git(root, "diff", "--name-only", "-z") == b"tracked.txt\0"
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "--initial-branch=master")
    git_index.assert_index_visible(lambda *args: git(empty, *args))


@pytest.mark.parametrize(
    "inventory",
    [
        b"H source",
        b"\0",
        b"H\0",
        b"H \0",
        b"H-source\0",
        b"X source\0",
        b"H source\0\0",
    ],
)
def test_malformed_inventory_is_rejected(inventory: bytes) -> None:
    with pytest.raises(git_index.GitIndexError):
        git_index.assert_index_visible(lambda *_args: inventory)


def test_git_inventory_failure_is_not_treated_as_an_empty_index() -> None:
    failure = subprocess.CalledProcessError(128, ("git", "ls-files", "-v", "-z"))

    def fail(*_args: str) -> bytes:
        raise failure

    with pytest.raises(subprocess.CalledProcessError) as error:
        git_index.assert_index_visible(fail)
    assert error.value is failure


def branches(tmp_path: Path, *, linked: bool) -> tuple[Path, Path, str, str]:
    task_root = repository(tmp_path / "repository")
    primary_oid = oid(task_root, "HEAD")
    git(task_root, "switch", "-c", "task")
    (task_root / "task.txt").write_text("task source\n")
    git(task_root, "add", "task.txt")
    git(task_root, "commit", "-m", "test: create candidate change")
    task_oid = oid(task_root, "HEAD")
    primary_root = task_root
    if linked:
        primary_root = tmp_path / "primary"
        git(task_root, "worktree", "add", str(primary_root), "master")
    return task_root, primary_root, primary_oid, task_oid


def configure(tmp_path: Path, *, linked: bool) -> tuple[Path, Path, Path, str]:
    task_root, primary_root, primary_oid, task_oid = branches(tmp_path, linked=linked)
    state = tmp_path / "workflow.json"
    admission.acquire(task_root, state, os.getpid())
    admission.configure(
        state,
        worktree=primary_root,
        primary_ref="refs/heads/master",
        primary_oid=primary_oid,
        task_ref="refs/heads/task",
        task_oid=task_oid,
    )
    if not linked:
        admission.switch_primary(state)
    return task_root, primary_root, state, task_oid


@pytest.mark.parametrize("flag", ["assume-unchanged", "skip-worktree"])
@pytest.mark.parametrize("location", ["single", "linked-task", "linked-primary"])
def test_admission_checks_both_caller_and_primary_indexes_before_mutation(
    tmp_path: Path, flag: str, location: str
) -> None:
    task_root, primary_root, primary_oid, task_oid = branches(
        tmp_path, linked=location != "single"
    )
    hidden_root = primary_root if location == "linked-primary" else task_root
    mark_hidden(hidden_root, flag)
    (hidden_root / "tracked.txt").write_text("hidden local changes\n")
    inventory = git(hidden_root, "ls-files", "-v", "-z")
    state = tmp_path / "workflow.json"
    admission.acquire(task_root, state, os.getpid())
    with pytest.raises(
        admission.AdmissionError, match="assume-unchanged or skip-worktree"
    ):
        admission.configure(
            state,
            worktree=primary_root,
            primary_ref="refs/heads/master",
            primary_oid=primary_oid,
            task_ref="refs/heads/task",
            task_oid=task_oid,
        )
    assert oid(task_root, "master") == primary_oid
    assert oid(task_root, "task") == task_oid
    assert git(hidden_root, "ls-files", "-v", "-z") == inventory
    assert (hidden_root / "tracked.txt").read_text() == "hidden local changes\n"
    admission.release(state)


@pytest.mark.parametrize("flag", ["assume-unchanged", "skip-worktree"])
def test_switch_rechecks_index_flags_after_admission(tmp_path: Path, flag: str) -> None:
    task_root, primary_root, primary_oid, task_oid = branches(tmp_path, linked=False)
    state = tmp_path / "workflow.json"
    admission.acquire(task_root, state, os.getpid())
    admission.configure(
        state,
        worktree=primary_root,
        primary_ref="refs/heads/master",
        primary_oid=primary_oid,
        task_ref="refs/heads/task",
        task_oid=task_oid,
    )
    mark_hidden(task_root, flag)
    (task_root / "tracked.txt").write_text("hidden local changes\n")
    inventory = git(task_root, "ls-files", "-v", "-z")
    with pytest.raises(
        admission.AdmissionError, match="assume-unchanged or skip-worktree"
    ):
        admission.switch_primary(state)
    assert git(task_root, "symbolic-ref", "HEAD") == b"refs/heads/task\n"
    assert oid(task_root, "master") == primary_oid
    assert git(task_root, "ls-files", "-v", "-z") == inventory
    assert (task_root / "tracked.txt").read_text() == "hidden local changes\n"
    assert (task_root / ".git/h2h-merge-workflow.lock/owner.json").is_file()


@pytest.mark.parametrize("flag", [None, "assume-unchanged", "skip-worktree"])
def test_integrated_linked_task_cleanup_rechecks_its_own_index(
    tmp_path: Path, flag: str | None
) -> None:
    primary_root = repository(tmp_path / "primary")
    primary_oid = oid(primary_root, "HEAD")
    task_root = tmp_path / "task"
    git(primary_root, "worktree", "add", "-b", "task", str(task_root), "master")
    (task_root / "task.txt").write_text("task source\n")
    git(task_root, "add", "task.txt")
    git(task_root, "commit", "-m", "test: create linked candidate change")
    task_oid = oid(task_root, "HEAD")
    state = tmp_path / "workflow.json"
    admission.acquire(task_root, state, os.getpid())
    admission.configure(
        state,
        worktree=primary_root,
        primary_ref="refs/heads/master",
        primary_oid=primary_oid,
        task_ref="refs/heads/task",
        task_oid=task_oid,
    )
    admission.begin(state, cleanup=False)
    git(primary_root, "merge", "--no-ff", "--no-commit", task_oid)
    admission.capture(state)
    git(primary_root, "commit", "--no-edit")
    commit = admission.finish(state)
    if flag is None:
        admission.remove_task_worktree(state, task_root)
        assert not task_root.exists()
    else:
        mark_hidden(task_root, flag)
        (task_root / "tracked.txt").write_text("hidden local changes\n")
        inventory = git(task_root, "ls-files", "-v", "-z")
        with pytest.raises(
            admission.AdmissionError, match="assume-unchanged or skip-worktree"
        ):
            admission.remove_task_worktree(state, task_root)
        assert git(task_root, "ls-files", "-v", "-z") == inventory
        assert (task_root / "tracked.txt").read_text() == "hidden local changes\n"
        assert git(task_root, "symbolic-ref", "HEAD") == b"refs/heads/task\n"
    assert oid(primary_root, "master") == commit
    assert oid(primary_root, "task") == task_oid
    admission.release(state)


@pytest.mark.parametrize("flag", ["assume-unchanged", "skip-worktree"])
@pytest.mark.parametrize("linked", [False, True])
@pytest.mark.parametrize("stage", ["begin", "capture", "recover-abort", "finish"])
def test_index_flags_introduced_during_workflow_preserve_candidate_and_admission(
    tmp_path: Path, flag: str, linked: bool, stage: str
) -> None:
    task_root, primary_root, state, task_oid = configure(tmp_path, linked=linked)
    if stage != "begin":
        admission.begin(state, cleanup=False)
        git(primary_root, "merge", "--no-ff", "--no-commit", task_oid)
    if stage in {"recover-abort", "finish"}:
        admission.capture(state)
    if stage == "finish":
        git(primary_root, "commit", "--no-edit")
    mark_hidden(primary_root, flag)
    (primary_root / "tracked.txt").write_text("hidden local changes\n")
    inventory = git(primary_root, "ls-files", "-v", "-z")
    head = oid(primary_root, "HEAD")
    tree = git(primary_root, "write-tree")
    metadata_path = Path(
        os.fsdecode(
            git(
                primary_root,
                "rev-parse",
                "--path-format=absolute",
                "--git-path",
                "MERGE_HEAD",
            ).removesuffix(b"\n")
        )
    )
    metadata = metadata_path.read_bytes() if metadata_path.exists() else None

    with pytest.raises(
        admission.AdmissionError, match="assume-unchanged or skip-worktree"
    ):
        if stage == "begin":
            admission.begin(state, cleanup=False)
        elif stage == "capture":
            admission.capture(state)
        elif stage == "recover-abort":
            admission.recover_abort(state)
        else:
            admission.finish(state)

    assert oid(primary_root, "HEAD") == head
    assert oid(task_root, "task") == task_oid
    assert git(primary_root, "write-tree") == tree
    assert git(primary_root, "ls-files", "-v", "-z") == inventory
    assert (metadata_path.read_bytes() if metadata_path.exists() else None) == metadata
    assert (primary_root / "tracked.txt").read_text() == "hidden local changes\n"
    assert (task_root / ".git/h2h-merge-workflow.lock/owner.json").is_file()


def test_auto_merge_without_merge_head_does_not_authorize_recovery(
    tmp_path: Path,
) -> None:
    task_root, primary_root, state, task_oid = configure(tmp_path, linked=False)
    admission.begin(state, cleanup=False)
    git(primary_root, "merge", "--no-ff", "--no-commit", task_oid)
    for name in ("MERGE_HEAD", "MERGE_MODE", "MERGE_MSG"):
        (primary_root / ".git" / name).unlink(missing_ok=True)
    before = git(primary_root, "ls-files", "--stage", "-z")
    auto_merge = (primary_root / ".git/AUTO_MERGE").read_bytes()
    with pytest.raises(admission.AdmissionError, match="unknown ownership"):
        admission.capture(state)
    with pytest.raises(admission.AdmissionError, match="ownership is unproven"):
        admission.recover_abort(state)
    assert git(primary_root, "ls-files", "--stage", "-z") == before
    assert (primary_root / ".git/AUTO_MERGE").read_bytes() == auto_merge
    assert (task_root / ".git/h2h-merge-workflow.lock/owner.json").is_file()


def test_commit_without_captured_candidate_is_preserved_without_adoption(
    tmp_path: Path,
) -> None:
    task_root, primary_root, state, task_oid = configure(tmp_path, linked=False)
    admission.begin(state, cleanup=False)
    git(primary_root, "merge", "--no-ff", "--no-commit", task_oid)
    git(primary_root, "commit", "--no-edit")
    commit = oid(primary_root, "HEAD")
    with pytest.raises(admission.AdmissionError, match="unknown ownership"):
        admission.capture(state)
    with pytest.raises(admission.AdmissionError, match="ownership is unproven"):
        admission.recover_abort(state)
    assert oid(primary_root, "master") == commit
    assert oid(task_root, "task") == task_oid
    assert (task_root / ".git/h2h-merge-workflow.lock/owner.json").is_file()


@pytest.mark.parametrize(
    "operation", ["MERGE_HEAD", "MERGE_MODE", "MERGE_MSG", "AUTO_MERGE"]
)
def test_completed_candidate_with_foreign_metadata_is_never_quit_or_aborted(
    tmp_path: Path, operation: str
) -> None:
    task_root, primary_root, state, task_oid = configure(tmp_path, linked=False)
    admission.begin(state, cleanup=False)
    git(primary_root, "merge", "--no-ff", "--no-commit", task_oid)
    admission.capture(state)
    git(primary_root, "commit", "--no-edit")
    commit = oid(primary_root, "HEAD")
    value = (
        task_oid if operation == "MERGE_HEAD" else oid(primary_root, "HEAD^{tree}")
    ).encode("ascii") + b"\n"
    path = primary_root / ".git" / operation
    path.write_bytes(value)
    with pytest.raises(admission.AdmissionError, match="changed outside"):
        admission.recover_abort(state)
    assert oid(primary_root, "HEAD") == commit
    assert path.read_bytes() == value
    assert (task_root / ".git/h2h-merge-workflow.lock/owner.json").is_file()
