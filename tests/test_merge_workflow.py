from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from h2h_dev_tools import bundle, bundle_verify, merge_command, process

_ROOT = Path(__file__).resolve().parents[1]
_REVIEWER = """import runpy
import subprocess
import sys
from pathlib import Path
sys.dont_write_bytecode = True
root = Path(__file__).resolve().parents[1]
package = root / "src/h2h_dev_tools"
runpy.run_path(str(package / "source_loader.py"))["load_package"](package, "h2h_dev_tools")
from h2h_dev_tools.code_review import main

def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()

directory = Path(git("rev-parse", "--git-common-dir"))
mode = sys.argv[1]
with (directory / "fixture-review-events").open("a") as events:
    events.write(mode + "\\n")
if mode == "run" and (directory / "reject-review").exists():
    raise SystemExit(1)
main(root)
"""


@pytest.fixture(autouse=True)
def offline_codex(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    binary = tmp_path / "fake-review-cli"
    binary.mkdir()
    executable(
        binary / "codex",
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('codex-cli merge-fixture')\n"
        "    raise SystemExit(0)\n"
        "sys.stdin.read()\n"
        "output = Path(sys.argv[sys.argv.index('--output-last-message') + 1])\n"
        "output.write_text(json.dumps(dict(verdict='pass', summary='Reviewed.', findings=[])))\n",
    )
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ.get("PATH", ""))


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


def fixture_repository(
    root: Path,
    *,
    succeeds: bool,
    profile: str = "full",
    ref_format: str = "files",
) -> str:
    root.mkdir()
    if ref_format == "files":
        git(root, "init", "-b", "master")
    else:
        initialized = subprocess.run(
            (
                "git",
                "-C",
                str(root),
                "init",
                "-b",
                "master",
                f"--ref-format={ref_format}",
            ),
            capture_output=True,
            text=True,
            check=False,
        )
        if "unknown option" in initialized.stderr:
            pytest.skip("Installed Git does not support the reftable backend")
        assert initialized.returncode == 0, initialized.stderr
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
    (root / "AGENTS.md").write_text("# Review policy\n\nReport concrete defects.\n")
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


@pytest.mark.parametrize("component", ["git-flow", "code-review"])
def test_selected_merge_component_exports_the_complete_owner(
    tmp_path: Path, component: str
) -> None:
    package = tmp_path / "h2h_dev_tools"
    package.mkdir()
    for name in bundle_verify.component_files([component]):
        shutil.copy2(_ROOT / "src/h2h_dev_tools" / name, package / name)
    result = subprocess.run(
        (
            sys.executable,
            "-I",
            "-B",
            str(package / "merge_command.py"),
            "--print-source-files",
        ),
        capture_output=True,
        text=True,
        check=True,
    )
    for name in result.stdout.splitlines():
        assert (package / name).is_file(), f"Missing exported owner dependency: {name}"


@pytest.mark.parametrize("profile", ["full", "documentation"])
def test_consumer_snapshot_merge_exports_its_complete_loading_authority(
    tmp_path: Path, profile: str
) -> None:
    root = tmp_path / "repository"
    base = fixture_repository(root, succeeds=True, profile=profile)
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'snapshot-fixture'\nversion = '0.1.0'\n"
        "[tool.h2h-dev-tools]\ncomponents = ['git-flow', 'code-review', 'hooks']\n"
    )
    bundle.sync_bundle(root)
    shutil.rmtree(root / "src")
    for name in (
        "detect-primary-branch.sh",
        "git-flow-merge.sh",
        "review-merge-candidate.sh",
    ):
        entry = root / "scripts" / name
        entry.write_text(
            entry.read_text().replace(
                "/src/h2h_dev_tools/", "/scripts/_shared/h2h_dev_tools/"
            )
        )
    commit_hook = root / ".githooks/commit-msg"
    commit_hook.write_text(
        commit_hook.read_text().replace(
            "/src/h2h_dev_tools/", "/scripts/_shared/h2h_dev_tools/"
        )
    )
    (root / "scripts/review-code.py").write_text(
        _REVIEWER.replace(
            'root / "src/h2h_dev_tools"', 'root / "scripts/_shared/h2h_dev_tools"'
        )
        .replace('package, "h2h_dev_tools"', 'package, "_shared.h2h_dev_tools"')
        .replace(
            "from h2h_dev_tools.code_review", "from _shared.h2h_dev_tools.code_review"
        )
    )
    git(root, "add", ".")
    git(root, "commit", "-m", "test: use a real consumer source snapshot")
    task = git(root, "rev-parse", "task")

    result = subprocess.run(
        ("scripts/git-flow-merge.sh",),
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (root / "src").exists()
    assert git(root, "show", "-s", "--format=%P", "master").split() == [base, task]
    assert git(root, "branch", "--show-current") == "master"
    assert git(root, "branch", "--list", "task") == ""
    assert git(root, "status", "--porcelain") == ""
    assert not (root / ".git/MERGE_HEAD").exists()
    events = root / ".git/fixture-review-events"
    if profile == "full":
        assert events.read_text() == "run\nverify\nverify\n"
        assert (root / ".git/fixture-gate").read_text() == "checked\n"
    else:
        assert not events.exists()
        assert not (root / ".git/fixture-gate").exists()


@pytest.mark.parametrize("strategy", ["direct", "candidate"])
def test_merge_uses_captured_commit_when_task_advances_before_git_launch(
    tmp_path: Path, strategy: str
) -> None:
    root = tmp_path / "repository"
    base = fixture_repository(root, succeeds=True)
    observed_parent = tmp_path / "observed-parent"
    if strategy == "direct":
        # This fixture's real release gate is for candidate-style commits.
        # Exercise Git's direct hook timing with its own explicit parent input.
        executable(
            root / ".githooks/pre-merge-commit",
            f"#!{sys.executable}\n"
            "import os\nfrom pathlib import Path\n"
            f"Path({str(observed_parent)!r}).write_text(os.environ['WORKFLOW_MERGE_TASK_REF'])\n",
        )
        git(root, "add", ".githooks/pre-merge-commit")
        git(root, "commit", "-m", "test: observe the direct hook parent")
    task = git(root, "rev-parse", "task")
    later = git(root, "commit-tree", "HEAD^{tree}", "-p", task, "-m", "later task")
    binary = tmp_path / "bin"
    binary.mkdir()
    real_git = shutil.which("git")
    assert real_git is not None
    executable(
        binary / "git",
        f"#!{sys.executable}\n"
        "import os, subprocess, sys\n"
        f"real_git = {real_git!r}\narguments = sys.argv[1:]\n"
        "if 'merge' in arguments and '--no-ff' in arguments:\n"
        f"    subprocess.run([real_git, '-C', {str(root)!r}, 'update-ref', "
        f"'refs/heads/task', {later!r}, {task!r}], check=True)\n"
        "os.execv(real_git, [real_git, *arguments])\n",
    )
    result = subprocess.run(
        (
            "bash",
            "src/h2h_dev_tools/git-flow-merge.sh",
            str(root),
            strategy,
            "scripts/review-merge-candidate.sh" if strategy == "candidate" else "",
        ),
        cwd=root,
        env=os.environ
        | {"PATH": str(binary) + os.pathsep + os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "Git ref changed during cleanup" in result.stderr
    assert git(root, "show", "-s", "--format=%P", "master").split() == [base, task]
    assert git(root, "rev-parse", "task") == later
    if strategy == "direct":
        assert observed_parent.read_text() == task
    assert "task" in git(root, "show", "-s", "--format=%s", "master")
    assert not (root / ".git/MERGE_HEAD").exists()
    assert git(root, "status", "--porcelain") == ""


def test_successful_merge_preserves_a_task_advanced_during_the_gate(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "primary"
    base = fixture_repository(primary, succeeds=True)
    state = tmp_path / "state"
    state.mkdir()
    executable(
        primary / "scripts/check-full.sh",
        f"#!{sys.executable}\n"
        "import time\nfrom pathlib import Path\n"
        f"state = Path({str(state)!r})\n"
        "(state / 'ready').touch()\n"
        "deadline = time.monotonic() + 15\n"
        "while not (state / 'release').exists():\n"
        "    assert time.monotonic() < deadline, 'fixture gate release timed out'\n"
        "    time.sleep(0.01)\n",
    )
    git(primary, "add", ".")
    git(primary, "commit", "-m", "test: pause the merge gate")
    reviewed_tip = git(primary, "rev-parse", "task")
    git(primary, "branch", "upstream", "task")
    git(primary, "branch", "--set-upstream-to=upstream", "task")
    git(primary, "switch", "master")
    task = tmp_path / "task"
    git(primary, "worktree", "add", str(task), "task")
    local_bin = task / ".venv/bin"
    local_bin.mkdir(parents=True)
    (local_bin / "python").symlink_to(sys.executable)
    with (state / "output").open("w+") as output:
        wrapper = subprocess.Popen(
            ("scripts/git-flow-merge.sh",),
            cwd=task,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 15
            while not (state / "ready").exists():
                assert wrapper.poll() is None, (state / "output").read_text()
                assert time.monotonic() < deadline
                time.sleep(0.01)
            (task / "later-work.txt").write_text("Must remain on the task branch.\n")
            git(task, "add", ".")
            git(task, "commit", "-m", "test: add work while the old tip is reviewed")
            advanced_tip = git(task, "rev-parse", "task")
            git(task, "update-ref", "refs/heads/upstream", advanced_tip)
            assert git(task, "status", "--porcelain") == ""
            (state / "release").touch()
            wrapper.wait(timeout=15)
            assert wrapper.returncode != 0, (state / "output").read_text()
            assert task.is_dir(), "The newly advanced task worktree was removed"
            assert git(primary, "rev-parse", "task") == advanced_tip
            assert git(primary, "show", "-s", "--format=%P", "HEAD").split() == [
                base,
                reviewed_tip,
            ]
            assert not Path(
                git(
                    primary,
                    "rev-parse",
                    "--path-format=absolute",
                    "--git-path",
                    "MERGE_HEAD",
                )
            ).exists()
            assert git(primary, "status", "--porcelain") == ""
            assert git(task, "config", "branch.task.merge") == "refs/heads/upstream"
        finally:
            (state / "release").touch()
            if wrapper.poll() is None:
                wrapper.terminate()
            wrapper.wait(timeout=15)


@pytest.mark.parametrize("cleanup_only", [False, True])
def test_branch_deletion_rechecks_primary_when_task_moves_after_cleanup_checks(
    tmp_path: Path, cleanup_only: bool
) -> None:
    root = tmp_path / "repository"
    base = fixture_repository(root, succeeds=True)
    task = git(root, "rev-parse", "task")
    git(root, "branch", "upstream", "task")
    git(root, "branch", "--set-upstream-to=upstream", "task")
    # merge is multivalued; a command-line value must not silently leave the
    # old first upstream authoritative for Git's deletion check.
    git(root, "config", "--add", "branch.task.merge", "refs/heads/another-upstream")
    original_config = git(root, "config", "--get-regexp", r"^branch\.task\.")
    later = git(
        root,
        "commit-tree",
        "HEAD^{tree}",
        "-p",
        task,
        "-m",
        "test: concurrent task commit at branch deletion",
    )
    if cleanup_only:
        git(root, "update-ref", "refs/heads/master", task)
    binary = tmp_path / "late-update-bin"
    binary.mkdir()
    real_git = shutil.which("git")
    assert real_git is not None
    executable(
        binary / "git",
        f"#!{sys.executable}\n"
        "import os, subprocess, sys\n"
        f"real_git = {real_git!r}\n"
        "if sys.argv[-3:] == ['branch', '-d', 'task']:\n"
        f"    subprocess.run([real_git, '-C', {str(root)!r}, 'update-ref', "
        f"'refs/heads/task', {later!r}, {task!r}], check=True)\n"
        f"    subprocess.run([real_git, '-C', {str(root)!r}, 'update-ref', "
        f"'refs/heads/upstream', {later!r}], check=True)\n"
        "os.execv(real_git, [real_git, *sys.argv[1:]])\n",
    )
    result = subprocess.run(
        ("scripts/git-flow-merge.sh", *(["--cleanup-only"] if cleanup_only else [])),
        cwd=root,
        env=os.environ
        | {"PATH": str(binary) + os.pathsep + os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "not fully merged" in result.stderr
    assert git(root, "rev-parse", "task") == later
    assert git(root, "config", "--get-regexp", r"^branch\.task\.") == original_config
    assert git(root, "branch", "--show-current") == (
        "task" if cleanup_only else "master"
    )
    assert git(root, "status", "--porcelain") == ""
    if cleanup_only:
        assert git(root, "rev-parse", "master") == task
        assert git(root, "rev-parse", "HEAD") == later
    else:
        assert git(root, "show", "-s", "--format=%P", "HEAD").split() == [base, task]


@pytest.mark.parametrize("ref_format", ["files", "reftable"])
def test_branch_deletion_checks_the_locked_ref_after_git_reads_a_stale_tip(
    tmp_path: Path, ref_format: str
) -> None:
    root = tmp_path / "repository"
    fixture_repository(root, succeeds=True, ref_format=ref_format)
    task = git(root, "rev-parse", "task")
    git(root, "update-ref", "refs/heads/master", task)
    later = git(root, "commit-tree", "HEAD^{tree}", "-p", task, "-m", "later task")
    state = tmp_path / "state"
    state.mkdir()
    hooks = state / "original-hooks"
    hooks.mkdir()
    executable(
        hooks / "reference-transaction",
        f"#!{sys.executable}\n"
        "import json, sys, time\nfrom pathlib import Path\n"
        f"state = Path({str(state)!r})\n"
        "rows = sys.stdin.read()\n"
        "with (state / 'hook-events').open('a') as output:\n"
        "    output.write(json.dumps([sys.argv[1:], rows]) + '\\n')\n"
        f"if sys.argv[1] == 'prepared' and {later!r} in rows:\n"
        "    (state / 'writer-locked').touch()\n"
        "    deadline = time.monotonic() + 15\n"
        "    while not (state / 'release-writer').exists():\n"
        "        assert time.monotonic() < deadline\n"
        "        time.sleep(0.01)\n",
    )
    git(root, "config", "core.hooksPath", str(hooks))
    binary = state / "bin"
    binary.mkdir()
    real_git = shutil.which("git")
    assert real_git is not None
    executable(
        binary / "git",
        f"#!{sys.executable}\n"
        "import os, sys, time\nfrom pathlib import Path\n"
        f"real_git = {real_git!r}\nstate = Path({str(state)!r})\n"
        "arguments = sys.argv[1:]\n"
        "if arguments[-3:] == ['branch', '-d', 'task']:\n"
        "    (state / 'deletion-ready').touch()\n"
        "    deadline = time.monotonic() + 15\n"
        "    while not (state / 'writer-locked').exists():\n"
        "        assert time.monotonic() < deadline\n"
        "        time.sleep(0.01)\n"
        "    os.environ['GIT_TRACE_REFS'] = str(state / 'refs-trace')\n"
        "    arguments = ['-c', 'core.filesRefLockTimeout=15000', "
        "'-c', 'reftable.lockTimeout=15000', *arguments]\n"
        "os.execv(real_git, [real_git, *arguments])\n",
    )
    writer: subprocess.Popen[str] | None = None
    with (state / "output").open("w+") as output:
        wrapper = subprocess.Popen(
            ("scripts/git-flow-merge.sh", "--cleanup-only"),
            cwd=root,
            env=os.environ
            | {"PATH": str(binary) + os.pathsep + os.environ.get("PATH", "")},
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 15
            while not (state / "deletion-ready").exists():
                assert wrapper.poll() is None, (state / "output").read_text()
                assert time.monotonic() < deadline
                time.sleep(0.01)
            writer = subprocess.Popen(
                (
                    real_git,
                    "-C",
                    str(root),
                    "update-ref",
                    "refs/heads/task",
                    later,
                    task,
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            trace = state / "refs-trace"
            while not trace.exists() or (
                f"read_raw_ref: refs/heads/task: {task}" not in trace.read_text()
            ):
                assert wrapper.poll() is None, (state / "output").read_text()
                assert writer.poll() is None, writer.communicate()
                assert time.monotonic() < deadline
                time.sleep(0.01)
            # Git has already checked the old tip, while this writer owns the
            # actual ref lock. Releasing it makes deletion acquire the NEW tip.
            (state / "release-writer").touch()
            stdout, stderr = writer.communicate(timeout=15)
            assert writer.returncode == 0, stdout + stderr
            wrapper.wait(timeout=15)
            assert wrapper.returncode != 0, (state / "output").read_text()
            assert (
                "ref changed while locking branch deletion"
                in (state / "output").read_text()
            )
            assert git(root, "rev-parse", "task") == later
            assert git(root, "rev-parse", "master") == task
            assert git(root, "status", "--porcelain") == ""
            assert git(root, "config", "core.hooksPath") == str(hooks)
            events = [
                json.loads(line)
                for line in (state / "hook-events").read_text().splitlines()
            ]
            deletion_events = [
                (arguments, rows)
                for arguments, rows in events
                if any(
                    row.split()[1:] == ["0" * len(task), "refs/heads/task"]
                    for row in rows.splitlines()
                )
            ]
            assert any(arguments == ["prepared"] for arguments, _ in deletion_events)
            assert any(arguments == ["aborted"] for arguments, _ in deletion_events)
            assert not any(
                arguments == ["committed"] for arguments, _ in deletion_events
            )
        finally:
            (state / "release-writer").touch()
            if writer is not None:
                if writer.poll() is None:
                    writer.terminate()
                writer.wait(timeout=15)
            if wrapper.poll() is None:
                wrapper.terminate()
            wrapper.wait(timeout=15)


@pytest.mark.parametrize("reject", [False, True])
def test_linked_merge_cleanup_preserves_reference_hook_input_and_rejection(
    tmp_path: Path, reject: bool
) -> None:
    primary = tmp_path / "primary"
    base = fixture_repository(primary, succeeds=True)
    events_path = tmp_path / "hook-events"
    executable(
        primary / ".githooks/reference-transaction",
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "rows = sys.stdin.read()\n"
        "deleting = any(row.split()[1:] == ['0' * len(row.split()[0]), "
        "'refs/heads/task'] for row in rows.splitlines())\n"
        "if deleting:\n"
        f"    with open({str(events_path)!r}, 'a') as output:\n"
        "        output.write(json.dumps([sys.argv[1:], rows]) + '\\n')\n"
        f"    if {reject!r} and sys.argv[1] == 'prepared':\n"
        "        print('original reference hook refused deletion', file=sys.stderr)\n"
        "        sys.exit(42)\n",
    )
    git(primary, "add", ".")
    git(primary, "commit", "-m", "test: preserve the existing reference hook")
    task_oid = git(primary, "rev-parse", "task")
    git(primary, "switch", "master")
    task = tmp_path / "linked-task"
    git(primary, "worktree", "add", str(task), "task")
    local_bin = task / ".venv/bin"
    local_bin.mkdir(parents=True)
    (local_bin / "python").symlink_to(sys.executable)
    result = subprocess.run(
        ("scripts/git-flow-merge.sh",),
        cwd=task,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert (result.returncode != 0) is reject, result.stdout + result.stderr
    assert not task.exists()
    assert git(primary, "show", "-s", "--format=%P", "HEAD").split() == [base, task_oid]
    assert git(primary, "status", "--porcelain") == ""
    assert git(primary, "config", "core.hooksPath") == ".githooks"
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    assert ["prepared"] in [arguments for arguments, _ in events]
    assert (["committed"] in [arguments for arguments, _ in events]) is not reject
    # All-zero old OIDs are Git's original branch -d payload; the chained hook
    # must receive those bytes rather than a substituted expected old value.
    assert {rows for _, rows in events} == {
        f"{'0' * len(task_oid)} {'0' * len(task_oid)} refs/heads/task\n"
    }
    if reject:
        assert "original reference hook refused deletion" in result.stderr
        assert git(primary, "rev-parse", "task") == task_oid
    else:
        assert git(primary, "branch", "--list", "task") == ""


@pytest.mark.parametrize(
    "failed_command,status", [("cat", 1), ("chmod", 1), ("chmod", 0)]
)
def test_cleanup_retains_task_if_reference_guard_cannot_be_prepared(
    tmp_path: Path, failed_command: str, status: int
) -> None:
    root = tmp_path / "repository"
    fixture_repository(root, succeeds=True)
    task = git(root, "rev-parse", "task")
    git(root, "update-ref", "refs/heads/master", task)
    binary = tmp_path / "failed-guard-bin"
    binary.mkdir()
    if failed_command == "cat":
        real_cat = shutil.which("cat")
        assert real_cat is not None
        # Fail only the guard's heredoc write, not an earlier owner's receipt
        # read. Every checkout now verifies that receipt before cleanup starts.
        failing_command = (
            f"#!{sys.executable}\nimport os, sys\n"
            f"if len(sys.argv) == 1:\n    raise SystemExit({status})\n"
            f"os.execv({real_cat!r}, [{real_cat!r}, *sys.argv[1:]])\n"
        )
    else:
        failing_command = f"#!/bin/sh\nexit {status}\n"
    executable(binary / failed_command, failing_command)
    result = subprocess.run(
        ("scripts/git-flow-merge.sh", "--cleanup-only"),
        cwd=root,
        env=os.environ
        | {"PATH": str(binary) + os.pathsep + os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode != 0
    assert "branch deletion guard; task retained" in result.stderr
    assert git(root, "rev-parse", "task") == task
    assert git(root, "rev-parse", "master") == task
    assert git(root, "status", "--porcelain") == ""


def test_cleanup_without_reference_transaction_support_retains_task(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    fixture_repository(root, succeeds=True)
    task = git(root, "rev-parse", "task")
    git(root, "update-ref", "refs/heads/master", task)
    binary = tmp_path / "old-git-bin"
    binary.mkdir()
    real_git = shutil.which("git")
    assert real_git is not None
    executable(
        binary / "git",
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "if sys.argv[1:] == ['version']:\n"
        "    print('git version 2.27.0')\n"
        "    sys.exit(0)\n"
        f"os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])\n",
    )
    result = subprocess.run(
        ("scripts/git-flow-merge.sh", "--cleanup-only"),
        cwd=root,
        env=os.environ
        | {"PATH": str(binary) + os.pathsep + os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode != 0
    assert "reference-transaction hooks" in result.stderr
    assert git(root, "branch", "--show-current") == "task"
    assert git(root, "rev-parse", "task") == task
    assert git(root, "rev-parse", "master") == task


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
            '#!/bin/sh\nrm "$(git rev-parse --git-path h2h-review/receipts)"/*.json\n',
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


def test_full_review_success_without_pending_evidence_is_rejected(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    base = fixture_repository(root, succeeds=True)
    (root / "scripts/review-code.py").write_text("raise SystemExit(0)\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "test: simulate reviewer losing its result")
    result = subprocess.run(
        ("scripts/git-flow-merge.sh",),
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "No valid code review" in result.stderr
    assert not (root / ".git/fixture-gate").exists()
    assert not (root / ".git/MERGE_HEAD").exists()
    assert git(root, "rev-parse", "master") == base
    assert git(root, "branch", "--show-current") == "task"


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
def test_noop_cleanup_without_python_preserves_the_task(
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
    assert result.returncode != 0, result.stdout + result.stderr
    assert "scripts/rebuild-env.sh" in result.stderr
    assert git(root, "branch", "--show-current") == "task"
    assert git(root, "rev-parse", "master") == primary
    assert git(root, "branch", "--list", "task")


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


def test_documentation_merge_does_not_require_a_local_review_entry(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    fixture_repository(root, succeeds=True, profile="documentation")
    (root / "scripts/review-code.py").unlink()
    git(root, "add", ".")
    git(root, "commit", "-m", "test: omit unused online review entry")
    result = subprocess.run(
        ("scripts/git-flow-merge.sh",),
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert git(root, "branch", "--show-current") == "master"
    assert not (root / ".git/fixture-review-events").exists()


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


@pytest.mark.skipif(os.name != "posix", reason="Uses POSIX fixture shell hooks")
def test_windows_documentation_stage_does_not_activate_online_review_protocol(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "repository"
    fixture_repository(root, succeeds=True, profile="documentation")
    git(root, "switch", "master")
    git(root, "merge", "--no-ff", "--no-commit", "task")
    # Only the dispatcher's platform changes. Its command runner separately owns
    # Windows Job Objects; documentation classification must not request a POSIX
    # review lock or managed-online-review environment on that platform.
    monkeypatch.setattr(
        merge_command,
        "os",
        SimpleNamespace(name="nt", environ=os.environ, getpid=os.getpid),
    )

    def run_command(
        phase: process.ProcessCommand, *, hard_deadline: float | None
    ) -> int:
        assert hard_deadline is None
        assert phase.environment is not None
        assert (
            not set(merge_command.REVIEW_OWNER_ENVIRONMENT) & phase.environment.keys()
        )
        assert phase.argv == (
            sys.executable,
            "scripts/check_change_scope.py",
            "--index",
            "--base",
            "HEAD",
        )
        assert phase.stdout is not None and not isinstance(phase.stdout, int)
        phase.stdout.write(b"documentation\n")
        return 0

    monkeypatch.setattr(process, "run_command", run_command)
    receipt = tmp_path / "cleanup"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "merge-command",
            "--cleanup-receipt",
            str(receipt),
            "--review",
            str(root),
            "--",
            "bash",
            "scripts/review-merge-candidate.sh",
        ],
    )
    assert merge_command.main() == 0
    assert receipt.read_text() == "owned_tree_empty\n"


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
@pytest.mark.parametrize("stage", ["review", "commit"])
def test_launcher_unexpected_exit_without_receipt_preserves_candidate(
    tmp_path: Path, exit_status: int, stage: str
) -> None:
    root = tmp_path / "repository"
    base = fixture_repository(root, succeeds=True)
    source = root / "src/h2h_dev_tools/process.py"
    source.write_text(
        source.read_text()
        + "\n_original_run_command = run_command\n"
        + "def run_command(phase, **kwargs):\n"
        + f"    if phase.argv[-1] == {('scripts/review-merge-candidate.sh' if stage == 'review' else '--no-edit')!r}:\n"
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


_WORKFLOW_HOOK = r'''import os
import subprocess
import sys
import time
from pathlib import Path

location = os.environ.get("WORKFLOW_HOOK_STATE")
if not location:
    raise SystemExit(0)
state = Path(location)
stage = os.environ["WORKFLOW_HOOK_STAGE"]
hook = Path(sys.argv[0]).name
if hook == "post-checkout":
    branch = subprocess.check_output(["git", "branch", "--show-current"], text=True).strip()
    applies = (stage == "checkout" and branch == "master") or (stage == "restore" and branch == "task")
else:
    rows = sys.stdin.read()
    applies = sys.argv[1] == "prepared" and (
        (stage == "abort" and Path(subprocess.check_output(["git", "rev-parse", "--git-path", "MERGE_HEAD"], text=True).strip()).exists())
        or (stage == "delete" and any(row.split()[1:] == ["0" * len(row.split()[0]), "refs/heads/task"] for row in rows.splitlines()))
    )
if not applies or (state / "started").exists():
    raise SystemExit(0)
(state / "started").touch()
(state / "group").write_text(str(os.getpgrp()))
child = r"""
import fcntl, os, signal, sys, time
from pathlib import Path
state = Path(sys.argv[1])
signal.signal(signal.SIGTERM, signal.SIG_IGN)
lock = (state / "child-lock").open("a")
fcntl.flock(lock, fcntl.LOCK_EX)
(state / "ready").touch()
while True:
    time.sleep(0.1)
"""
subprocess.Popen([sys.executable, "-c", child, str(state)])
while not (state / "ready").exists():
    time.sleep(0.01)
if os.environ["WORKFLOW_HOOK_MODE"] == "leader-exit":
    raise SystemExit(0)
while True:
    time.sleep(0.1)
'''


@pytest.mark.skipif(os.name == "nt", reason="Exercises real POSIX Git hook descendants")
@pytest.mark.parametrize(
    ("stage", "strategy"),
    [
        ("checkout", "direct"),
        ("checkout", "candidate"),
        ("noop", "candidate"),
        ("restore", "candidate"),
        ("abort", "candidate"),
        ("delete", "candidate"),
    ],
)
@pytest.mark.parametrize("mode", ["cancel", "leader-exit"])
def test_all_mutating_workflow_hooks_are_owned(
    tmp_path: Path, stage: str, strategy: str, mode: str
) -> None:
    root = tmp_path / "repository"
    fixture_repository(root, succeeds=stage not in {"restore", "abort"})
    state = tmp_path / "hook-state"
    state.mkdir()
    hook_name = (
        "post-checkout"
        if stage in {"checkout", "noop", "restore"}
        else "reference-transaction"
    )
    hook_text = f"#!{sys.executable}\n" + _WORKFLOW_HOOK
    # Checkout invokes the hook from the destination tree. Keep the identical
    # real hook in both branches, inactive unless this invocation opts in.
    executable(root / ".githooks" / hook_name, hook_text)
    git(root, "add", ".")
    git(root, "commit", "-m", "test: install workflow hook")
    task = git(root, "rev-parse", "task")
    git(root, "switch", "master")
    (root / ".githooks").mkdir(exist_ok=True)
    executable(root / ".githooks" / hook_name, hook_text)
    git(root, "add", ".")
    git(root, "commit", "-m", "test: install primary checkout hook")
    if stage == "noop":
        git(
            root,
            "-c",
            "core.hooksPath=/dev/null",
            "merge",
            "--no-ff",
            "--no-edit",
            "task",
        )
    primary = git(root, "rev-parse", "master")
    git(root, "switch", "task")
    with (state / "output").open("w") as output:
        wrapper = subprocess.Popen(
            (
                "bash",
                "src/h2h_dev_tools/git-flow-merge.sh",
                str(root),
                strategy,
                "scripts/review-merge-candidate.sh" if strategy == "candidate" else "",
                *(["--cleanup-only"] if stage == "noop" else []),
            ),
            cwd=root,
            env=os.environ
            | {
                "WORKFLOW_HOOK_STATE": str(state),
                "WORKFLOW_HOOK_STAGE": "checkout" if stage == "noop" else stage,
                "WORKFLOW_HOOK_MODE": mode,
            },
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 20
            while not (state / "ready").exists():
                assert wrapper.poll() is None, (state / "output").read_text()
                assert time.monotonic() < deadline, "The workflow hook never started"
                time.sleep(0.01)
            if mode == "cancel":
                wrapper.terminate()
            wrapper.wait(timeout=15)
            recorded = (state / "output").read_text()
            assert wrapper.returncode != 0, recorded
            if mode == "cancel" and stage in {"checkout", "noop", "delete"}:
                assert wrapper.returncode == 143, recorded
            _assert_gate_lock_released(state / "child-lock")
            if stage == "delete":
                assert git(root, "show", "-s", "--format=%P", "master").split() == [
                    primary,
                    task,
                ]
            else:
                assert git(root, "rev-parse", "master") == primary
                assert git(root, "rev-parse", "task") == task
            assert (root / ".git/MERGE_HEAD").exists() is (
                stage == "abort" and mode == "cancel"
            )
            if stage == "abort":
                assert git(root, "branch", "--show-current") == "master"
                assert "recovery incomplete" in recorded
            if stage in {"restore", "abort"} and mode == "cancel":
                assert "command budget expired" in recorded
            if stage in {"checkout", "noop"}:
                assert not (root / ".git/fixture-gate").exists()
        finally:
            groups = {wrapper.pid}
            if (state / "group").exists():
                groups.add(int((state / "group").read_text()))
            for group in groups:
                try:
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            wrapper.wait(timeout=5)


@pytest.mark.parametrize("stage", ["checkout", "abort", "restore", "delete"])
def test_workflow_stops_mutating_after_unproven_stage_cleanup(
    tmp_path: Path, stage: str
) -> None:
    root = tmp_path / "repository"
    base = fixture_repository(root, succeeds=stage not in {"abort", "restore"})
    command = {
        "checkout": ("switch", "master"),
        "abort": ("merge", "--abort"),
        "restore": ("switch", "task"),
        "delete": ("branch", "-d", "task"),
    }[stage]
    events = tmp_path / "commands.jsonl"
    source = root / "src/h2h_dev_tools/process.py"
    source.write_text(
        source.read_text()
        + "\n_original_run_command = run_command\n"
        + "def run_command(phase, **kwargs):\n"
        + "    import json\n"
        + f"    with open({str(events)!r}, 'a') as events:\n"
        + "        events.write(json.dumps(phase.argv) + '\\n')\n"
        + f"    if phase.argv[-{len(command)}:] == {command!r}:\n"
        + "        raise ProcessOwnershipError('fixture cleanup unproven')\n"
        + "    return _original_run_command(phase, **kwargs)\n"
    )
    git(root, "add", ".")
    git(
        root, "commit", "-m", "test: refuse workflow continuation without cleanup proof"
    )
    task = git(root, "rev-parse", "task")
    result = subprocess.run(
        ("scripts/git-flow-merge.sh",),
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "process cleanup unproven" in result.stderr
    commands = [json.loads(line) for line in events.read_text().splitlines()]
    assert commands[-1][-len(command) :] == list(command)
    assert git(root, "rev-parse", "task") == task
    assert git(root, "branch", "--show-current") == (
        "task" if stage == "checkout" else "master"
    )
    assert (root / ".git/MERGE_HEAD").exists() is (stage == "abort")
    if stage == "delete":
        assert git(root, "show", "-s", "--format=%P", "master").split() == [base, task]
    else:
        assert git(root, "rev-parse", "master") == base


@pytest.mark.parametrize("cleanup_only", [False, True])
def test_linked_cleanup_requires_surviving_primary_environment(
    tmp_path: Path, cleanup_only: bool
) -> None:
    primary = tmp_path / "primary"
    base = fixture_repository(primary, succeeds=True)
    executable(primary / ".githooks/pre-merge-commit", "#!/bin/sh\nexit 0\n")
    git(primary, "add", ".")
    git(primary, "commit", "-m", "test: use a shell-only merge gate")
    task_oid = git(primary, "rev-parse", "task")
    git(primary, "switch", "master")
    if cleanup_only:
        git(primary, "merge", "--no-ff", "--no-edit", "task")
    task = tmp_path / "linked-task"
    git(primary, "worktree", "add", str(task), "task")
    local_bin = task / ".venv/bin"
    local_bin.mkdir(parents=True)
    (local_bin / "python").symlink_to(sys.executable)
    shutil.rmtree(primary / ".venv")
    result = subprocess.run(
        (
            "bash",
            "src/h2h_dev_tools/git-flow-merge.sh",
            str(task),
            "direct",
            "",
            *(["--cleanup-only"] if cleanup_only else []),
        ),
        cwd=task,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert f"missing repository-local Python in {primary}" in result.stderr
    assert "scripts/rebuild-env.sh" in result.stderr
    assert task.is_dir()
    assert git(primary, "rev-parse", "task") == task_oid
    assert git(primary, "show", "-s", "--format=%P", "master").split() == [
        base,
        task_oid,
    ]
    assert not (primary / ".git/MERGE_HEAD").exists()


@pytest.mark.skipif(os.name == "nt", reason="Online review requires POSIX ownership")
@pytest.mark.parametrize("surviving_child", [False, True])
@pytest.mark.parametrize(
    ("diverged_primary", "separate_filesystem"),
    [(False, False), (True, False), (False, True)],
)
def test_passing_managed_review_requires_an_empty_stage_before_commit(
    tmp_path: Path,
    surviving_child: bool,
    diverged_primary: bool,
    separate_filesystem: bool,
) -> None:
    root = tmp_path / "repository"
    base = fixture_repository(root, succeeds=True)
    (root / ".gitignore").write_text(".venv/\n__pycache__/\n")
    (root / "AGENTS.md").write_text("# Review policy\n\nReport concrete defects.\n")
    shutil.copy2(_ROOT / "scripts/review-code.py", root / "scripts/review-code.py")
    if separate_filesystem:
        # Model a Git common directory on another volume: atomic renames may not
        # cross between review metadata and the system temporary directory.
        reviewer_source = root / "src/h2h_dev_tools/code_review.py"
        reviewer_source.write_text(
            reviewer_source.read_text()
            + "\n_original_replace = os.replace\n"
            + "def _replace_same_filesystem(source, destination):\n"
            + "    import errno\n"
            + "    source_is_metadata = 'h2h-review' in Path(source).parts\n"
            + "    destination_is_metadata = 'h2h-review' in Path(destination).parts\n"
            + "    if source_is_metadata != destination_is_metadata:\n"
            + "        raise OSError(errno.EXDEV, 'Cross-device atomic rename')\n"
            + "    return _original_replace(source, destination)\n"
            + "os.replace = _replace_same_filesystem\n"
        )
    git(root, "add", ".")
    git(root, "commit", "-m", "test: validate successful managed review")
    if diverged_primary:
        # Both branches share the tools, then primary changes the owner while the
        # task independently changes a different file. No rebase is required.
        git(root, "switch", "master")
        git(root, "merge", "--ff-only", "task")
        source = root / "src/h2h_dev_tools/process.py"
        source.write_text(source.read_text() + "\n# Updated primary owner.\n")
        git(root, "add", ".")
        git(
            root,
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-m",
            "fix: update primary process owner",
        )
        base = git(root, "rev-parse", "HEAD")
        git(root, "switch", "task")
        (root / "independent.py").write_text("VALUE = 1\n")
        git(root, "add", ".")
        git(root, "commit", "-m", "feat: change an unrelated task file")
    task = git(root, "rev-parse", "HEAD")
    binary = tmp_path / "bin"
    binary.mkdir()
    executable(
        binary / "codex",
        f"#!{sys.executable}\n"
        "import json, os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "assert not any(name.startswith('H2H_REVIEW_') for name in os.environ)\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('codex-cli managed-review-fixture')\n"
        "    raise SystemExit(0)\n"
        "sys.stdin.read()\n"
        "output = Path(sys.argv[sys.argv.index('--output-last-message') + 1])\n"
        "output.write_text(json.dumps(dict(verdict='pass', summary='Reviewed.', findings=[])))\n"
        f"if {surviving_child!r}:\n"
        "    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "    Path(os.environ['REVIEW_GROUP_FILE']).write_text(str(os.getpgrp()))\n",
    )
    group_file = tmp_path / "review-group"
    try:
        result = subprocess.run(
            ("scripts/git-flow-merge.sh",),
            cwd=root,
            env=os.environ
            | {
                "PATH": str(binary) + os.pathsep + os.environ.get("PATH", ""),
                "REVIEW_GROUP_FILE": str(group_file),
            },
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        assert (result.returncode == 0) is not surviving_child, (
            result.stdout + result.stderr
        )
        assert not (root / ".git/MERGE_HEAD").exists()
        if surviving_child:
            with pytest.raises(ProcessLookupError):
                os.killpg(int(group_file.read_text()), 0)
            assert git(root, "rev-parse", "master") == base
            assert git(root, "rev-parse", "task") == task
            assert not (root / ".git/fixture-gate").exists()
            # Recreating the exact failed candidate must not resurrect a passing
            # result written before its outer process owner rejected the stage.
            git(root, "switch", "master")
            git(root, "merge", "--no-ff", "--no-commit", "task")
            verification = subprocess.run(
                (sys.executable, "scripts/review-code.py", "verify", "--index"),
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            assert verification.returncode != 0, verification.stdout
            git(root, "merge", "--abort")
        else:
            assert git(root, "show", "-s", "--format=%P", "master").split() == [
                base,
                task,
            ]
            assert (root / ".git/fixture-gate").read_text() == "checked\n"
    finally:
        if group_file.exists():
            try:
                os.killpg(int(group_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(os.name == "nt", reason="Online review requires POSIX ownership")
@pytest.mark.parametrize("owner_result", ["publish", "crash", "corrupt"])
def test_managed_review_lock_covers_cleanup_and_evidence_publication(
    tmp_path: Path, owner_result: str
) -> None:
    root = tmp_path / "repository"
    base = fixture_repository(root, succeeds=True)
    (root / ".gitignore").write_text(".venv/\n__pycache__/\n")
    shutil.copy2(_ROOT / "scripts/review-code.py", root / "scripts/review-code.py")
    owner = root / "src/h2h_dev_tools/merge_command.py"
    owner.write_text(
        owner.read_text().replace(
            "        if status == 0:",
            "        import time\n"
            "        state = Path(os.environ['REVIEW_OWNER_STATE'])\n"
            "        (state / 'ready').touch()\n"
            "        while not (state / 'release').exists():\n"
            "            time.sleep(0.01)\n"
            f"        if {owner_result!r} == 'crash':\n"
            "            os._exit(1)\n"
            f"        if {owner_result!r} == 'corrupt':\n"
            "            pending.write_text('{}')\n"
            "        if status == 0:",
        )
    )
    git(root, "add", ".")
    git(root, "commit", "-m", "test: pause owner after command cleanup")
    state = tmp_path / "state"
    state.mkdir()
    binary = tmp_path / "bin"
    binary.mkdir()
    executable(
        binary / "codex",
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('codex-cli publication-fixture')\n"
        "    raise SystemExit(0)\n"
        "sys.stdin.read()\n"
        "output = Path(sys.argv[sys.argv.index('--output-last-message') + 1])\n"
        "output.write_text(json.dumps(dict(verdict='pass', summary='Reviewed.', findings=[])))\n",
    )
    environment = os.environ | {
        "PATH": str(binary) + os.pathsep + os.environ.get("PATH", ""),
        "REVIEW_OWNER_STATE": str(state),
    }
    with (state / "output").open("w+") as output:
        wrapper = subprocess.Popen(
            ("scripts/git-flow-merge.sh",),
            cwd=root,
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 15
            while not (state / "ready").exists():
                assert wrapper.poll() is None, (state / "output").read_text()
                assert time.monotonic() < deadline, "Owner did not reach publication"
                time.sleep(0.01)
            for command in ("verify", "run"):
                overlapping = subprocess.run(
                    (sys.executable, "scripts/review-code.py", command, "--index"),
                    cwd=root,
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
                assert overlapping.returncode != 0, overlapping.stdout
                assert "already running" in overlapping.stderr
            assert not list((root / ".git/h2h-review/receipts").glob("*.json"))
            (state / "release").touch()
            wrapper.wait(timeout=20)
            assert (wrapper.returncode == 0) is (owner_result == "publish"), (
                state / "output"
            ).read_text()
            verify_arguments: tuple[str, ...]
            if owner_result == "publish":
                verify_arguments = ("--revision", "HEAD")
                assert not (root / ".git/MERGE_HEAD").exists()
            else:
                assert git(root, "rev-parse", "master") == base
                assert git(root, "branch", "--list", "task")
                if owner_result == "corrupt":
                    assert not (root / ".git/MERGE_HEAD").exists()
                    git(root, "switch", "master")
                    git(root, "merge", "--no-ff", "--no-commit", "task")
                else:
                    assert (root / ".git/MERGE_HEAD").exists()
                    assert "process cleanup unproven" in (state / "output").read_text()
                verify_arguments = ("--index",)
            verification = subprocess.run(
                (sys.executable, "scripts/review-code.py", "verify", *verify_arguments),
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            assert (verification.returncode == 0) is (owner_result == "publish"), (
                verification.stdout + verification.stderr
            )
        finally:
            (state / "release").touch()
            if wrapper.poll() is None:
                wrapper.send_signal(signal.SIGTERM)
            wrapper.wait(timeout=10)


@pytest.mark.skipif(os.name != "posix", reason="Exercises POSIX pipe backpressure")
@pytest.mark.parametrize("missing", ["python", "launcher"])
def test_recovery_owner_startup_failure_does_not_wait_for_diagnostic_reader(
    tmp_path: Path, missing: str
) -> None:
    source = (_ROOT / "src/h2h_dev_tools/git-flow-merge.sh").read_text()
    functions = source.split("repository_root=${1:", 1)[0]
    script = tmp_path / "launch.sh"
    script.write_text(
        functions + "\nrecovering=1\ncommand_number=0\nowner_cleanup_failed=0\n"
        "starting_child=0\npending_signal=0\n"
        "merge_python=$1\nmerge_launcher=$2\nexecution_directory=$3\n"
        'if run_tracked process "$execution_directory" git status; then\n'
        "    status=0\nelse\n    status=$?\nfi\n"
        'printf "%s" "$owner_cleanup_failed" > "$execution_directory/failed"\n'
        'exit "$status"\n'
    )
    read_descriptor, write_descriptor = os.pipe()
    try:
        os.set_blocking(write_descriptor, False)
        try:
            while True:
                os.write(write_descriptor, b"x" * 8192)
        except BlockingIOError:
            pass
        os.set_blocking(write_descriptor, True)
        launcher = _ROOT / "src/h2h_dev_tools/merge_command.py"
        wrapper = subprocess.Popen(
            (
                "bash",
                str(script),
                str(tmp_path / "missing-python")
                if missing == "python"
                else sys.executable,
                str(tmp_path / "missing-launcher")
                if missing == "launcher"
                else str(launcher),
                str(tmp_path),
            ),
            stdout=subprocess.DEVNULL,
            stderr=write_descriptor,
            start_new_session=True,
        )
        try:
            assert wrapper.wait(timeout=5) == 125
            assert (tmp_path / "failed").read_text() == "1"
            assert not (tmp_path / "cleanup-1").exists()
            assert os.get_blocking(write_descriptor)
        finally:
            if wrapper.poll() is None:
                os.killpg(wrapper.pid, signal.SIGKILL)
                wrapper.wait(timeout=5)
    finally:
        os.close(read_descriptor)
        os.close(write_descriptor)


@pytest.mark.skipif(os.name == "nt", reason="Exercises real POSIX process signals")
@pytest.mark.parametrize(
    ("stage", "interruption"),
    [
        ("classifier", "cancel"),
        ("version", "cancel"),
        ("execute", "cancel"),
        ("execute", "closed-pipe"),
        ("execute", "closed-pipe-crash"),
        ("execute", "backpressure"),
        ("execute", "backpressure-failure"),
        ("execute", "backpressure-helper-missing"),
        ("execute", "crash"),
        ("execute", "timeout"),
    ],
)
def test_merge_review_stage_owns_preflight_and_crashed_reviewer_descendants(
    tmp_path: Path, stage: str, interruption: str
) -> None:
    root = tmp_path / "repository"
    base = fixture_repository(root, succeeds=True)
    state = tmp_path / "state"
    state.mkdir()
    (root / ".gitignore").write_text(".venv/\n__pycache__/\n")
    (root / "AGENTS.md").write_text("# Review policy\n\nReport concrete defects.\n")
    shutil.copy2(_ROOT / "scripts/review-code.py", root / "scripts/review-code.py")
    (root / "scripts/check_change_scope.py").write_text(
        "import os, runpy\n"
        "if os.environ.get('MERGE_REVIEW_STAGE') == 'classifier':\n"
        "    runpy.run_path('scripts/blocking-review.py', run_name='__main__')\n"
        "print('full')\n"
    )
    (root / "scripts/blocking-review.py").write_text(_BLOCKING_GATE)
    piped_output = interruption.startswith(("closed-pipe", "backpressure"))
    if piped_output:
        executable(
            root / ".githooks/post-checkout",
            f"#!{sys.executable}\nimport os\nfrom pathlib import Path\n"
            "state = os.environ.get('MERGE_INTERRUPTION_STATE')\n"
            "if state and Path(state, 'ready').exists():\n"
            "    print('recovery diagnostic' * (16 * 1024), flush=True)\n"
            "    Path(state, 'restore-hook-ran').touch()\n"
            + (
                "    Path('.venv/bin/python').unlink()\n"
                if interruption == "backpressure-helper-missing"
                else ""
            )
            + f"    raise SystemExit({7 if interruption.startswith('backpressure-') else 0})\n",
        )
    binary = tmp_path / "bin"
    binary.mkdir()
    executable(
        binary / "codex",
        f"#!{sys.executable}\n"
        "import os, runpy, sys\n"
        "from pathlib import Path\n"
        "stage = os.environ['MERGE_REVIEW_STAGE']\n"
        "if sys.argv[1:] == ['--version'] and stage != 'version':\n"
        "    print('codex-cli review-lifecycle-fixture')\n"
        "else:\n"
        "    state = Path(os.environ['MERGE_INTERRUPTION_STATE'])\n"
        "    (state / 'reviewer-pid').write_text(str(os.getppid()))\n"
        "    runpy.run_path('scripts/blocking-review.py', run_name='__main__')\n",
    )
    git(root, "add", ".")
    git(root, "commit", "-m", "test: exercise the complete review lifecycle")
    task = git(root, "rev-parse", "HEAD")
    with (state / "output").open("w+") as output:
        wrapper = subprocess.Popen(
            ("scripts/git-flow-merge.sh",),
            cwd=root,
            env=os.environ
            | {
                "PATH": str(binary) + os.pathsep + os.environ.get("PATH", ""),
                "MERGE_INTERRUPTION_STATE": str(state),
                "MERGE_REVIEW_STAGE": stage,
                "H2H_CODE_REVIEW_TIMEOUT_SECONDS": "1"
                if interruption == "timeout"
                else "900",
            },
            stdout=subprocess.PIPE if piped_output else output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 15
            while not (state / "ready").exists():
                assert wrapper.poll() is None, (state / "output").read_text()
                assert time.monotonic() < deadline, (
                    "The review descendant never started"
                )
                time.sleep(0.01)
            if interruption.startswith("closed-pipe"):
                assert wrapper.stdout is not None
                wrapper.stdout.close()
            if interruption in {"crash", "closed-pipe-crash"}:
                os.kill(int((state / "reviewer-pid").read_text()), signal.SIGKILL)
            elif interruption in {"cancel", "closed-pipe"} or interruption.startswith(
                "backpressure"
            ):
                wrapper.send_signal(signal.SIGTERM)
            wrapper.wait(timeout=15)
            assert wrapper.returncode != 0, (state / "output").read_text()
            if interruption in {"cancel", "closed-pipe"} or interruption.startswith(
                "backpressure"
            ):
                assert wrapper.returncode == 143
            _assert_gate_lock_released(state / "leader-lock")
            _assert_gate_lock_released(state / "child-lock")
            assert not (root / ".git/MERGE_HEAD").exists()
            assert git(root, "rev-parse", "master") == base
            assert git(root, "rev-parse", "task") == task
            assert git(root, "branch", "--show-current") == "task"
            if piped_output:
                assert (state / "restore-hook-ran").exists()
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
            wrapper.wait(timeout=5)


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
                '    if [[ "$active_owner" == process && " $* " != *" --no-commit "* '
                '&& ( " $* " == *" merge "* || " $* " == *" commit "* ) ]]; then\n'
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


@pytest.mark.skipif(os.name != "posix", reason="Exercises POSIX pipe backpressure")
@pytest.mark.parametrize("missing_python", [False, True])
def test_failed_gate_recovers_without_waiting_for_diagnostic_reader(
    tmp_path: Path, *, missing_python: bool
) -> None:
    root = tmp_path / "repository"
    base = fixture_repository(root, succeeds=True)
    ready = tmp_path / "gate-failed"
    gate = root / "scripts/fill-diagnostic-pipe.py"
    gate.write_text(
        "import os\nfrom pathlib import Path\n"
        "os.set_blocking(2, False)\n"
        "try:\n"
        "    while True:\n        os.write(2, b'x' * 8192)\n"
        "except BlockingIOError:\n    pass\n"
        "finally:\n    os.set_blocking(2, True)\n"
        + ("Path('.venv/bin/python').unlink()\n" if missing_python else "")
        + f"Path({str(ready)!r}).touch()\n"
    )
    executable(
        root / "scripts/check-full.sh",
        "#!/bin/sh\nexit 7\n",
    )
    # Git itself can print after a hook returns, so fill the pipe only once
    # the real failed command has exited and its owner proved cleanup. This
    # isolates the shell's pre-EXIT failure diagnostic from child command I/O.
    shell = root / "src/h2h_dev_tools/git-flow-merge.sh"
    terminal_status = '    [[ "$owner_cleanup_failed" -eq 0 ]] || return 125\n'
    shell.write_text(
        shell.read_text().replace(
            terminal_status,
            terminal_status
            + '    if [[ "$status" -ne 0 && " $* " == *" commit "* ]]; then\n'
            + '        "$merge_python" "$repository_root/scripts/fill-diagnostic-pipe.py"\n'
            + "    fi\n",
        )
    )
    git(root, "add", ".")
    git(root, "commit", "-m", "test: fail the gate with an undrained diagnostic pipe")
    task = git(root, "rev-parse", "HEAD")
    wrapper = subprocess.Popen(
        ("scripts/git-flow-merge.sh",),
        cwd=root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        # Keep the pipe open without reading it. No external signal is needed:
        # the failed gate must automatically reach recovery and exit.
        assert wrapper.wait(timeout=15) == 1
        assert ready.exists()
        assert git(root, "rev-parse", "master") == base
        assert git(root, "rev-parse", "task") == task
        assert (root / ".git/MERGE_HEAD").exists() is missing_python
        if missing_python:
            assert git(root, "branch", "--show-current") == "master"
        else:
            assert git(root, "branch", "--show-current") == "task"
            assert git(root, "status", "--porcelain") == ""
    finally:
        if wrapper.poll() is None:
            os.killpg(wrapper.pid, signal.SIGKILL)
        wrapper.communicate(timeout=5)
