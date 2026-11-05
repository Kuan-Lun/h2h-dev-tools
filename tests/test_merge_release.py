"""Terminal admission release stays inside the proven cleanup boundary."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_merge_workflow import (
    fixture_repository,
    git,
    owned_fixture_command,
    record_workflow_receipts,
)
from test_merge_workflow import offline_codex as offline_codex


@pytest.mark.skipif(
    os.name != "posix", reason="Real site startup and POSIX owner groups"
)
@pytest.mark.parametrize("startup", ["sitecustomize", "pth"])
def test_terminal_release_retains_admission_for_surviving_site_child(
    tmp_path: Path, startup: str
) -> None:
    root = tmp_path / "repository"
    base = fixture_repository(root, succeeds=False)
    receipts = tmp_path / "receipts.tsv"
    record_workflow_receipts(root, receipts)
    git(root, "add", "src/h2h_dev_tools/git-flow-merge.sh")
    git(root, "commit", "-m", "test: record cleanup receipts")
    shutil.rmtree(root / ".venv")
    subprocess.run(
        (
            sys.executable,
            "-I",
            "-S",
            "-m",
            "venv",
            "--without-pip",
            str(root / ".venv"),
        ),
        check=True,
        capture_output=True,
        timeout=10,
    )
    python = root / ".venv/bin/python"
    site = Path(
        subprocess.check_output(
            (
                str(python),
                "-I",
                "-B",
                "-c",
                "import sysconfig; print(sysconfig.get_path('purelib'))",
            ),
            text=True,
            timeout=5,
        ).strip()
    )
    ready = tmp_path / "child-ready.json"
    observation = tmp_path / "child-cleanup.json"
    child = tmp_path / "child.py"
    lock = root / ".git/h2h-merge-workflow.lock"
    child.write_text(
        "import json, os, signal\nfrom pathlib import Path\n"
        "def terminate(signum, frame):\n"
        f"    Path({str(observation)!r}).write_text(json.dumps(dict(lock_exists=Path({str(lock)!r}).is_dir(), pid=os.getpid(), group=os.getpgrp())))\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, terminate)\n"
        f"Path({str(ready)!r}).write_text(json.dumps(dict(pid=os.getpid(), group=os.getpgrp())))\n"
        "while True: signal.pause()\n"
    )
    hook = (
        "import subprocess, sys, time\nfrom pathlib import Path\n"
        "if '--workflow-state' in sys.argv and sys.argv[sys.argv.index('--workflow-state') + 1] in ('release', 'validate-release'):\n"
        f"    ready = Path({str(ready)!r})\n"
        "    if not ready.exists():\n"
        f"        subprocess.Popen([sys.executable, '-I', '-S', '-B', {str(child)!r}], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "        deadline = time.monotonic() + 5\n"
        "        while not ready.exists():\n"
        "            if time.monotonic() >= deadline: raise RuntimeError('child readiness deadline')\n"
        "            time.sleep(0.01)\n"
    )
    if startup == "sitecustomize":
        (site / "sitecustomize.py").write_text(hook)
    else:
        (site / "release-startup.pth").write_text(f"import sys; exec({hook!r})\n")
    result = owned_fixture_command(root, ("scripts/git-flow-merge.sh",))
    (tmp_path / "workflow.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 1, result.stdout + result.stderr
    assert ready.exists(), "The terminal child must execute real site startup"
    observed = json.loads(observation.read_text())
    with pytest.raises(ProcessLookupError):
        os.killpg(observed["group"], 0)
    recorded = [
        Path(line.split("\t", 1)[1]) for line in receipts.read_text().splitlines()
    ]
    assert recorded
    assert all(path.read_text() == "owned_tree_empty\n" for path in recorded)
    assert git(root, "rev-parse", "master") == base
    assert not (root / ".git/MERGE_HEAD").exists()
    assert observed["lock_exists"], (
        "Admission disappeared while the last owned child was alive"
    )
    assert (lock / "owner.json").is_file()


@pytest.mark.parametrize("unlink_stage", ["owner", "directory"])
@pytest.mark.parametrize("cleanup_only", [False, True])
def test_failed_terminal_unlink_does_not_start_another_writer(
    tmp_path: Path, unlink_stage: str, cleanup_only: bool
) -> None:
    root = tmp_path / "repository"
    base = fixture_repository(root, succeeds=True, profile="documentation")
    entered = tmp_path / "terminal-release-entered"
    forbidden = tmp_path / "forbidden-writer"
    receipts = tmp_path / "receipts.tsv"
    record_workflow_receipts(root, receipts)
    admission_source = root / "src/h2h_dev_tools/merge_admission.py"
    admission_source.write_text(
        admission_source.read_text()
        + "\n_original_release = release\n"
        + "def release(state_path):\n"
        + f"    Path({str(entered)!r}).touch()\n"
        + "    original_unlink, original_rmdir = Path.unlink, Path.rmdir\n"
        + "    def fail_unlink(path, *args, **kwargs):\n"
        + f"        if {unlink_stage == 'owner'!r} and path.name == 'owner.json':\n"
        + "            raise OSError('terminal owner unlink failure')\n"
        + "        return original_unlink(path, *args, **kwargs)\n"
        + "    def fail_rmdir(path):\n"
        + f"        if {unlink_stage == 'directory'!r} and path.name == 'h2h-merge-workflow.lock':\n"
        + "            raise OSError('terminal lock rmdir failure')\n"
        + "        return original_rmdir(path)\n"
        + "    Path.unlink, Path.rmdir = fail_unlink, fail_rmdir\n"
        + "    try: return _original_release(state_path)\n"
        + "    finally: Path.unlink, Path.rmdir = original_unlink, original_rmdir\n"
    )
    process_source = root / "src/h2h_dev_tools/process.py"
    process_source.write_text(
        process_source.read_text()
        + "\n_original_report = report_diagnostic\n"
        + "def report_diagnostic(*args, **kwargs):\n"
        + f"    if Path({str(entered)!r}).exists():\n"
        + f"        Path({str(forbidden)!r}).touch()\n"
        + "        raise RuntimeError('diagnostic after terminal release')\n"
        + "    return _original_report(*args, **kwargs)\n"
    )
    git(root, "add", "src/h2h_dev_tools")
    git(root, "commit", "-m", "test: inject terminal filesystem failure")
    task = git(root, "rev-parse", "HEAD")
    if cleanup_only:
        git(root, "update-ref", "refs/heads/master", task)
    result = owned_fixture_command(root, ("scripts/git-flow-merge.sh",))
    (tmp_path / "workflow.log").write_text(result.stdout + result.stderr)
    assert entered.exists()
    assert not forbidden.exists(), result.stdout + result.stderr
    assert result.returncode == 1, "A cleanup receipt cannot claim release succeeded"
    records = [line.split("\t", 1) for line in receipts.read_text().splitlines()]
    assert records[-1][0] == "release"
    receipt = Path(records[-1][1])
    assert receipt.read_text() == "owned_tree_empty\n"
    assert (receipt.parent / "workflow.json").is_file()
    assert (root / ".git/h2h-merge-workflow.lock").is_dir()
    if cleanup_only:
        assert git(root, "rev-parse", "master") == task
    else:
        assert git(root, "show", "-s", "--format=%P", "master").split() == [base, task]
    assert not (root / ".git/MERGE_HEAD").exists()


@pytest.mark.skipif(os.name != "posix", reason="Real workflow owner interruption")
@pytest.mark.parametrize("cleanup_only", [False, True])
def test_normal_terminal_interruption_cleans_owner_without_reentering_recovery(
    tmp_path: Path, cleanup_only: bool
) -> None:
    root = tmp_path / "repository"
    base = fixture_repository(root, succeeds=True, profile="documentation")
    receipts = tmp_path / "receipts.tsv"
    record_workflow_receipts(root, receipts)
    entered = tmp_path / "validation-entered"
    admission = root / "src/h2h_dev_tools/merge_admission.py"
    admission.write_text(
        admission.read_text()
        + "\n_fixture_validate_release = validate_release\n"
        + "def validate_release(state_path):\n"
        + "    state = _fixture_validate_release(state_path)\n"
        + "    import signal, sys, time\n"
        + "    if sys.argv[1:3] == ['--workflow-state', 'validate-release']:\n"
        + f"        Path({str(entered)!r}).touch()\n"
        + "        os.kill(state.owner_pid, signal.SIGTERM)\n"
        + "        time.sleep(20)\n"
        + "    return state\n"
    )
    git(root, "add", "src/h2h_dev_tools")
    git(root, "commit", "-m", "test: interrupt the normal terminal validation")
    task = git(root, "rev-parse", "task")
    if cleanup_only:
        git(root, "update-ref", "refs/heads/master", task)
    result = owned_fixture_command(root, ("scripts/git-flow-merge.sh",))
    assert entered.exists(), result.stdout + result.stderr
    assert result.returncode == 143, result.stdout + result.stderr
    records = [line.split("\t", 1) for line in receipts.read_text().splitlines()]
    assert records[-1][0] == "release", "No recovery or diagnostic may start afterward"
    assert all(Path(path).read_text() == "owned_tree_empty\n" for _, path in records)
    assert (Path(records[-1][1]).parent / "workflow.json").is_file()
    assert (root / ".git/h2h-merge-workflow.lock/owner.json").is_file()
    assert git(root, "branch", "--show-current") == "master"
    assert git(root, "branch", "--list", "task") == ""
    if cleanup_only:
        assert git(root, "rev-parse", "master") == task
    else:
        assert git(root, "show", "-s", "--format=%P", "master").split() == [base, task]
    assert not (root / ".git/MERGE_HEAD").exists()


@pytest.mark.parametrize("recovery", [False, True])
def test_unknown_terminal_cleanup_does_not_release_or_publish_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recovery: bool
) -> None:
    from h2h_dev_tools import merge_admission, merge_command, process

    root = tmp_path / "repository"
    fixture_repository(root, succeeds=True)
    state = tmp_path / "workflow.json"
    receipt = tmp_path / "cleanup"
    merge_admission.acquire(root, state, os.getpid())

    def unknown_cleanup(*args: object, **kwargs: object) -> int:
        raise process.ProcessOwnershipError("nested cleanup is unproven")

    monkeypatch.setattr(merge_command, "_run_git", unknown_cleanup)
    status = merge_command._execute(
        root,
        (sys.executable, "-c", "pass"),
        review=False,
        recovery=recovery,
        receipt=receipt,
        release_workflow=state,
    )
    assert status == process.TERMINATION_FAILED_EXIT_CODE
    assert not receipt.exists()
    assert state.is_file()
    assert (root / ".git/h2h-merge-workflow.lock/owner.json").is_file()


@pytest.mark.parametrize("recovery", [False, True])
def test_terminal_release_uses_only_its_own_lifetime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recovery: bool
) -> None:
    from h2h_dev_tools import merge_admission, merge_command

    root = tmp_path / "repository"
    fixture_repository(root, succeeds=True)
    state = tmp_path / "workflow.json"
    receipt = tmp_path / "cleanup"
    merge_admission.acquire(root, state, os.getpid())
    now = 100.0

    def complete_at_deadline(*args: object, **kwargs: object) -> int:
        nonlocal now
        assert kwargs["deadline"] == (110.0 if recovery else None)
        now = 110.0
        return 0

    monkeypatch.setattr(time, "monotonic", lambda: now)
    monkeypatch.setattr(merge_command, "_run_git", complete_at_deadline)
    assert merge_command._execute(
        root,
        (sys.executable, "-c", "pass"),
        review=False,
        recovery=recovery,
        receipt=receipt,
        release_workflow=state,
    ) == (1 if recovery else 0)
    assert receipt.read_text() == "owned_tree_empty\n"
    assert state.is_file() is recovery
    assert (root / ".git/h2h-merge-workflow.lock/owner.json").is_file() is recovery


@pytest.mark.parametrize("recovery", [False, True])
def test_terminal_owner_releases_after_successful_owned_validation(
    tmp_path: Path, recovery: bool
) -> None:
    from h2h_dev_tools import merge_admission

    root = tmp_path / "repository"
    fixture_repository(root, succeeds=True)
    state = tmp_path / "workflow.json"
    receipt = tmp_path / "cleanup"
    merge_admission.acquire(root, state, os.getpid())
    launcher = root / "src/h2h_dev_tools/merge_command.py"
    python = str(root / ".venv/bin/python")
    result = owned_fixture_command(
        root,
        (
            python,
            "-I",
            "-S",
            "-B",
            str(launcher),
            "--cleanup-receipt",
            str(receipt),
            "--release-workflow",
            str(state),
            *(("--recovery",) if recovery else ()),
            str(root),
            "--",
            python,
            "-I",
            "-B",
            str(launcher),
            "--workflow-state",
            "validate-release",
            "--state",
            str(state),
        ),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert receipt.read_text() == "owned_tree_empty\n"
    assert not state.exists()
    assert not (root / ".git/h2h-merge-workflow.lock").exists()


def test_terminal_release_cannot_run_as_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from h2h_dev_tools import merge_command

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "merge-command",
            "--cleanup-receipt",
            str(tmp_path / "cleanup"),
            "--review",
            "--release-workflow",
            str(tmp_path / "state"),
            str(tmp_path),
            "--",
            "git",
            "status",
        ],
    )
    with pytest.raises(SystemExit) as rejected:
        merge_command.main()
    assert rejected.value.code == 2
    assert not (tmp_path / "cleanup").exists()
