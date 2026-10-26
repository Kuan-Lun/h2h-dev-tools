from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from h2h_dev_tools import (
    bundle,
    bundle_verify,
    code_review,
    merge_command,
    process,
    review_test_support,
)
from h2h_dev_tools.executable_fixture import write_python_executable

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


_PASS_REVIEW = (
    review_test_support.synthetic_review_response_source()
    + """
result = synthetic_review_response(prompt)
output.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
"""
)


@pytest.fixture(autouse=True)
def offline_codex(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    binary = tmp_path / "fake-review-cli"
    binary.mkdir()
    write_python_executable(
        binary / "codex",
        "import json, sys\n"
        "from pathlib import Path\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('codex-cli merge-fixture')\n"
        "    raise SystemExit(0)\n"
        "prompt = sys.stdin.read()\n"
        "output = Path(sys.argv[sys.argv.index('--output-last-message') + 1])\n"
        + _PASS_REVIEW,
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


def record_workflow_receipts(root: Path, destination: Path) -> None:
    shell = root / "src/h2h_dev_tools/git-flow-merge.sh"
    assignment = (
        '    active_cleanup_receipt="$execution_directory/cleanup-$command_number"\n'
    )
    source = shell.read_text()
    assert source.count(assignment) == 1
    shell.write_text(
        source.replace(
            assignment,
            assignment
            + '    printf \'%s\\t%s\\n\' "$active_owner" "$active_cleanup_receipt" >> '
            + shlex.quote(str(destination))
            + "\n",
        )
    )


def owned_fixture_command(
    root: Path,
    argv: tuple[str, ...],
    *,
    environment: dict[str, str] | None = None,
    timeout_seconds: float = 90,
    termination_reserve_seconds: float = 30,
) -> subprocess.CompletedProcess[str]:
    """Bound the whole fixture, including nested owners and Git recovery.

    Regular files keep output capture independent of descendants closing pipes.
    The default reserve leaves room for both ten-second Git recovery stages and
    nested process teardown; this is a harness ceiling, not a performance oracle.
    """
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        result = process.run_command(
            process.ProcessCommand(
                "Merge workflow fixture", argv, root, environment, stdout, stderr
            ),
            hard_deadline=time.monotonic() + timeout_seconds,
            termination_reserve_seconds=termination_reserve_seconds,
        )
        stdout.seek(0)
        stderr.seek(0)
        return subprocess.CompletedProcess(
            argv,
            result,
            stdout.read().decode("utf-8", errors="replace"),
            stderr.read().decode("utf-8", errors="replace"),
        )


@pytest.mark.skipif(os.name != "posix", reason="POSIX nested owner cleanup contract")
def test_fixture_timeout_closes_nested_owner_and_descendant(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    scopes = tmp_path / "scopes"
    scopes.mkdir()
    leaf = tmp_path / "leaf.py"
    leaf.write_text(
        "import json, os, subprocess, sys\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "Path('leaf-ready.json').write_text(json.dumps({'pid': os.getpid(), "
        "'group': os.getpgrp(), 'child': child.pid}))\n"
        "child.wait()\n"
    )
    nested = tmp_path / "nested.py"
    nested.write_text(
        "import json, os, runpy, sys\n"
        "from pathlib import Path\n"
        f"package = Path({str(_ROOT / 'src/h2h_dev_tools')!r})\n"
        "runpy.run_path(str(package / 'source_loader.py'))['load_package'](package, 'h2h_dev_tools')\n"
        "from h2h_dev_tools import process\n"
        "Path('nested-ready.json').write_text(json.dumps({'pid': os.getpid(), 'group': os.getpgrp()}))\n"
        "try:\n"
        "    process.run_command(process.ProcessCommand('Nested fixture', "
        "(sys.executable, '-I', '-B', 'leaf.py'), Path.cwd()), hard_deadline=None)\n"
        "finally:\n"
        "    Path('nested-finished').write_text('closed')\n"
    )
    result = owned_fixture_command(
        tmp_path,
        (sys.executable, "-I", "-B", str(nested)),
        environment=os.environ | {"TMPDIR": str(scopes)},
        timeout_seconds=20,
        termination_reserve_seconds=10,
    )
    assert result.returncode == process.TIMEOUT_EXIT_CODE, result.stdout + result.stderr
    leaf_identity = json.loads((tmp_path / "leaf-ready.json").read_text())
    nested_identity = json.loads((tmp_path / "nested-ready.json").read_text())
    assert leaf_identity["group"] != nested_identity["group"]
    assert (tmp_path / "nested-finished").read_text() == "closed"
    for pid in (leaf_identity["pid"], leaf_identity["child"], nested_identity["pid"]):
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    for group in (leaf_identity["group"], nested_identity["group"]):
        with pytest.raises(ProcessLookupError):
            os.killpg(group, 0)
    assert list(scopes.iterdir()) == []
    assert "owned_tree_empty=True" in capfd.readouterr().out


def fixture_repository(
    root: Path,
    *,
    succeeds: bool,
    profile: str = "full",
    ref_format: str = "files",
    primary_hooks: bool = False,
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
    if primary_hooks:
        (root / ".githooks").mkdir()
        for name in ("pre-commit", "pre-merge-commit", "commit-msg", "pre-rebase"):
            executable(root / ".githooks" / name, "#!/bin/sh\nexit 0\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "chore: initialize repository")
    base = git(root, "rev-parse", "HEAD")
    git(root, "switch", "-c", "task")
    scripts = root / "scripts"
    scripts.mkdir()
    hooks = root / ".githooks"
    hooks.mkdir(exist_ok=True)
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
            "-S",
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
@pytest.mark.parametrize("parent_initializer", [False, True])
def test_consumer_snapshot_merge_exports_its_complete_loading_authority(
    tmp_path: Path, profile: str, parent_initializer: bool
) -> None:
    root = tmp_path / "repository"
    base = fixture_repository(root, succeeds=True, profile=profile)
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'snapshot-fixture'\nversion = '0.1.0'\n"
        "[tool.h2h-dev-tools]\ncomponents = ['git-flow', 'code-review', 'hooks']\n"
    )
    bundle.sync_bundle(root)
    if parent_initializer:
        (root / "scripts/_shared/__init__.py").write_text(
            "VALUE = 'current parent source'\n"
        )
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
    gate = root / "scripts/release-gate.py"
    gate.write_text(
        gate.read_text()
        .replace(' / "src/h2h_dev_tools"', ' / "scripts/_shared/h2h_dev_tools"')
        .replace('package, "h2h_dev_tools"', 'package, "_shared.h2h_dev_tools"')
        .replace("from h2h_dev_tools.git_index", "from _shared.h2h_dev_tools.git_index")
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


def test_merge_uses_captured_commit_when_task_advances_before_git_launch(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    base = fixture_repository(root, succeeds=True, primary_hooks=False)
    task = git(root, "rev-parse", "task")
    later = git(root, "commit-tree", "HEAD^{tree}", "-p", task, "-m", "later task")
    binary = tmp_path / "bin"
    binary.mkdir()
    real_git = shutil.which("git")
    assert real_git is not None
    write_python_executable(
        binary / "git",
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
            "scripts/review-merge-candidate.sh",
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
    assert "Merge outcome has unknown ownership" in result.stderr
    assert git(root, "rev-parse", "task") == later
    assert git(root, "branch", "--show-current") == "master"
    assert (root / ".git/h2h-merge-workflow.lock/owner.json").is_file()
    assert git(root, "rev-parse", "master") == base
    assert git(root, "rev-parse", "HEAD") == base
    assert (root / ".git/MERGE_HEAD").read_text() == task + "\n"
    assert git(root, "write-tree") == git(root, "rev-parse", task + "^{tree}")
    assert git(root, "diff", "--exit-code") == ""
    assert not (root / ".git/fixture-review-events").exists()
    assert not (root / ".git/fixture-gate").exists()


def test_successful_merge_preserves_a_task_advanced_during_the_gate(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "primary"
    base = fixture_repository(primary, succeeds=True)
    state = tmp_path / "state"
    state.mkdir()
    write_python_executable(
        primary / "scripts/check-full.sh",
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


@pytest.mark.skipif(os.name != "posix", reason="POSIX newline path and shell workflow")
@pytest.mark.parametrize("cdpath", [None, "."])
@pytest.mark.parametrize("entry", ["local", "shared-relative"])
def test_full_workflow_preserves_task_root_trailing_newlines(
    tmp_path: Path, cdpath: str | None, entry: str
) -> None:
    root = tmp_path / "repository\n\n"
    base = fixture_repository(root, succeeds=True)
    task = git(root, "rev-parse", "task")
    command = (
        ["scripts/git-flow-merge.sh"]
        if entry == "local"
        else [
            "bash",
            str(root / "src/h2h_dev_tools/git-flow-merge.sh"),
            root.name,
            "scripts/review-merge-candidate.sh",
        ]
    )
    result = subprocess.run(
        command,
        cwd=root if entry == "local" else tmp_path,
        env=os.environ | ({"CDPATH": cdpath} if cdpath is not None else {}),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert git(root, "branch", "--show-current") == "master"
    assert git(root, "branch", "--list", "task") == ""
    assert git(root, "show", "-s", "--format=%P", "master").split() == [base, task]
    assert git(root, "status", "--porcelain") == ""
    assert not (root / ".git/MERGE_HEAD").exists()
    assert (root / ".git/fixture-review-events").read_text() == "run\nverify\nverify\n"
    assert (root / ".git/fixture-gate").read_text() == "checked\n"


@pytest.mark.skipif(
    os.name != "posix", reason="POSIX worktree paths and shell workflow"
)
@pytest.mark.parametrize("cleanup_only", [False, True])
@pytest.mark.parametrize(
    "primary_name",
    [
        "primary with spaces",
        "primary\\name",
        "primary\rname",
        "primary\nname",
        "primary-tail\n",
    ],
    ids=["space", "backslash", "carriage-return", "newline", "trailing-newline"],
)
def test_shared_workflow_preserves_primary_worktree_path_bytes(
    tmp_path: Path, primary_name: str, cleanup_only: bool
) -> None:
    primary = tmp_path / primary_name
    base = fixture_repository(
        primary, succeeds=True, primary_hooks=True, profile="documentation"
    )
    # This test isolates shared discovery and owned Git mutations. Local wrapper
    # path decoding and review/gate policy have their own entrypoint contracts.
    for name in ("pre-commit", "pre-merge-commit", "commit-msg"):
        executable(primary / ".githooks" / name, "#!/bin/sh\nexit 0\n")
    git(primary, "add", ".")
    git(primary, "commit", "-m", "test: isolate shared worktree path discovery")
    task_oid = git(primary, "rev-parse", "task")
    git(primary, "switch", "master")
    if cleanup_only:
        git(primary, "merge", "--no-ff", "--no-edit", "task")
    task = tmp_path / "linked-task"
    git(primary, "worktree", "add", str(task), "task")
    local_bin = task / ".venv/bin"
    local_bin.mkdir(parents=True)
    (local_bin / "python").symlink_to(sys.executable)

    result = subprocess.run(
        (
            "bash",
            "src/h2h_dev_tools/git-flow-merge.sh",
            str(task),
            "scripts/review-merge-candidate.sh",
            *(["--cleanup-only"] if cleanup_only else []),
        ),
        cwd=task,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )

    result = _retry_retained_linked_task_cleanup(primary, task, result)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not task.exists()
    assert git(primary, "branch", "--show-current") == "master"
    assert git(primary, "branch", "--list", "task") == ""
    assert git(primary, "show", "-s", "--format=%P", "master").split() == [
        base,
        task_oid,
    ]
    assert git(primary, "status", "--porcelain") == ""
    assert not (primary / ".git/MERGE_HEAD").exists()


@pytest.mark.parametrize(
    "listing",
    [
        "git-failed",
        "truncated-field",
        "truncated-record",
        "wrong-ref",
        "extra-terminator",
        "duplicate-primary",
        "empty",
    ],
)
def test_worktree_query_failure_or_malformed_records_preserve_task(
    tmp_path: Path, listing: str
) -> None:
    root = tmp_path / "repository"
    base = fixture_repository(root, succeeds=True)
    task = git(root, "rev-parse", "task")
    binary = tmp_path / "git-query-failure"
    binary.mkdir()
    real_git = shutil.which("git")
    assert real_git is not None
    variants = {
        "truncated-field": b"refs/heads/master",
        "truncated-record": b"refs/heads/master\0/incomplete\0",
        "wrong-ref": b"refs/heads/master-extra\0/path\0\n",
        "extra-terminator": b"refs/heads/master\0/path\0unexpected\n",
        "duplicate-primary": b"refs/heads/master\0/path\0\n" * 2,
        "empty": b"",
    }
    write_python_executable(
        binary / "git",
        f"import os, subprocess, sys\n"
        f"real_git = {real_git!r}\narguments = sys.argv[1:]\n"
        "if arguments[:1] == ['for-each-ref']:\n"
        + (
            "    subprocess.run([real_git, *arguments], check=True)\n"
            "    raise SystemExit(19)\n"
            if listing == "git-failed"
            else f"    sys.stdout.buffer.write({variants[listing]!r})\n"
            "    raise SystemExit(0)\n"
        )
        + "os.execv(real_git, [real_git, *arguments])\n",
    )
    result = subprocess.run(
        ("scripts/git-flow-merge.sh",),
        cwd=root,
        env=os.environ
        | {"PATH": str(binary) + os.pathsep + os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "could not inspect repository worktrees" in result.stderr
    assert git(root, "rev-parse", "master") == base
    assert git(root, "rev-parse", "task") == task
    assert git(root, "branch", "--show-current") == "task"
    assert git(root, "status", "--porcelain") == ""
    assert not (root / ".git/MERGE_HEAD").exists()


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
    injected = tmp_path / "late-update-injected"
    write_python_executable(
        binary / "git",
        "import os, subprocess, sys\n"
        "from pathlib import Path\n"
        f"real_git = {real_git!r}\n"
        "if sys.argv[-3:] == ['branch', '-d', 'task']:\n"
        f"    subprocess.run([real_git, '-C', {str(root)!r}, 'update-ref', "
        f"'refs/heads/task', {later!r}, {task!r}], check=True)\n"
        f"    subprocess.run([real_git, '-C', {str(root)!r}, 'update-ref', "
        f"'refs/heads/upstream', {later!r}], check=True)\n"
        f"    Path({str(injected)!r}).write_text({later!r})\n"
        "os.execv(real_git, [real_git, *sys.argv[1:]])\n",
    )
    result = owned_fixture_command(
        root,
        ("scripts/git-flow-merge.sh", *(["--cleanup-only"] if cleanup_only else [])),
        environment=os.environ
        | {"PATH": str(binary) + os.pathsep + os.environ.get("PATH", "")},
    )
    assert result.returncode not in (
        0,
        process.TIMEOUT_EXIT_CODE,
        process.TERMINATION_FAILED_EXIT_CODE,
    ), result.stdout + result.stderr
    assert injected.read_text() == later
    assert "not fully merged" in result.stderr
    assert git(root, "rev-parse", "task") == later
    assert git(root, "config", "--get-regexp", r"^branch\.task\.") == original_config
    # Never restore a task checkout after its ref changed outside this owner.
    assert git(root, "branch", "--show-current") == "master"
    assert git(root, "status", "--porcelain") == ""
    assert (root / ".git/h2h-merge-workflow.lock/owner.json").is_file()
    assert not (root / ".git/MERGE_HEAD").exists()
    if cleanup_only:
        assert git(root, "rev-parse", "master") == task
        assert git(root, "rev-parse", "HEAD") == task
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
    write_python_executable(
        hooks / "reference-transaction",
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
    write_python_executable(
        binary / "git",
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
    write_python_executable(
        primary / ".githooks/reference-transaction",
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
    assert not events_path.exists()
    result = _retry_retained_linked_task_cleanup(primary, task, result)
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
        write_python_executable(
            binary / failed_command,
            "import os, sys\n"
            f"if len(sys.argv) == 1:\n    raise SystemExit({status})\n"
            f"os.execv({real_cat!r}, [{real_cat!r}, *sys.argv[1:]])\n",
        )
    else:
        executable(binary / failed_command, f"#!/bin/sh\nexit {status}\n")
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


@pytest.mark.parametrize("git_version", ["2.27.0", "2.37.9"])
def test_cleanup_without_required_git_support_retains_task(
    tmp_path: Path,
    git_version: str,
) -> None:
    root = tmp_path / "repository"
    fixture_repository(root, succeeds=True)
    task = git(root, "rev-parse", "task")
    git(root, "update-ref", "refs/heads/master", task)
    binary = tmp_path / "old-git-bin"
    binary.mkdir()
    real_git = shutil.which("git")
    assert real_git is not None
    write_python_executable(
        binary / "git",
        "import os, sys\n"
        "if sys.argv[1:] == ['version']:\n"
        f"    print('git version {git_version}')\n"
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
    assert "Git 2.38 or newer" in result.stderr
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
    fixture_repository(root, succeeds=True, primary_hooks=True, profile="documentation")
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
            "scripts/review-merge-candidate.sh",
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
        assert "git-flow-merge: process cleanup unproven" not in result.stderr


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
    assert "git-flow-merge: process cleanup unproven" not in result.stderr


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
hook = hook_name
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
    "stage",
    ["checkout", "noop", "restore", "abort", "delete"],
)
@pytest.mark.parametrize("mode", ["cancel", "leader-exit"])
def test_all_mutating_workflow_hooks_are_owned(
    tmp_path: Path, stage: str, mode: str
) -> None:
    root = tmp_path / "repository"
    fixture_repository(
        root,
        succeeds=stage not in {"restore", "abort"},
        primary_hooks=False,
    )
    state = tmp_path / "hook-state"
    state.mkdir()
    hook_name = (
        "post-checkout"
        if stage in {"checkout", "noop", "restore"}
        else "reference-transaction"
    )
    hook_text = f"hook_name = {hook_name!r}\n" + _WORKFLOW_HOOK
    # Checkout invokes the hook from the destination tree. Keep the identical
    # real hook in both branches, inactive unless this invocation opts in.
    write_python_executable(root / ".githooks" / hook_name, hook_text)
    git(root, "add", ".")
    git(root, "commit", "-m", "test: install workflow hook")
    task = git(root, "rev-parse", "task")
    git(root, "switch", "master")
    (root / ".githooks").mkdir(exist_ok=True)
    write_python_executable(root / ".githooks" / hook_name, hook_text)
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
    cleanup_deadline = time.monotonic() + 40
    with (
        process.controlled_termination_signals() as controller,
        (state / "output").open("wb") as output,
    ):
        owner: process.OwnedProcess | None = None
        clean = False
        try:
            with controller.defer():
                owner = process.start_owned_process(
                    process.ProcessCommand(
                        "workflow hook interruption fixture",
                        (
                            "bash",
                            "src/h2h_dev_tools/git-flow-merge.sh",
                            str(root),
                            "scripts/review-merge-candidate.sh",
                            *(["--cleanup-only"] if stage == "noop" else []),
                        ),
                        root,
                        environment=os.environ
                        | {
                            "WORKFLOW_HOOK_STATE": str(state),
                            "WORKFLOW_HOOK_STAGE": "checkout"
                            if stage == "noop"
                            else stage,
                            "WORKFLOW_HOOK_MODE": mode,
                        },
                        stdout=output,
                        stderr=subprocess.STDOUT,
                    ),
                    deadline=cleanup_deadline,
                    signal_controller=controller,
                )
            wrapper = owner.process
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
            # Wrapper exit and released locks cannot discharge nested obligations.
            # The canonical outer owner must prove cleanup before fixture rescue.
            _assert_interrupted_workflow_cleanup(
                owner, (state / "output").read_text(), deadline=cleanup_deadline
            )
            with pytest.raises(ProcessLookupError):
                os.killpg(int((state / "group").read_text()), 0)
            clean = True
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
                assert "recovery ownership or cleanup unproven" in recorded
                assert (root / ".git/h2h-merge-workflow.lock/owner.json").is_file()
            if stage in {"restore", "abort"} and mode == "cancel":
                assert "command budget expired" in recorded
            if stage in {"checkout", "noop"}:
                assert not (root / ".git/fixture-gate").exists()
        finally:
            if owner is not None and not clean:
                # Rescue cannot turn unknown cleanup into a passing assertion.
                with controller.defer():
                    empty = process.terminate_owned_tree(
                        owner, deadline=cleanup_deadline
                    )
                    closed = process.close_owner(owner, deadline=cleanup_deadline)
                    assert empty and closed, "Fixture cleanup remains unproven"
            if owner is not None and owner.process.stdout is not None:
                owner.process.stdout.close()


@pytest.mark.parametrize("stage", ["checkout", "abort", "restore", "delete"])
def test_workflow_stops_mutating_after_unproven_stage_cleanup(
    tmp_path: Path, stage: str
) -> None:
    root = tmp_path / "repository"
    base = fixture_repository(root, succeeds=stage not in {"abort", "restore"})
    command = {
        "checkout": ("--workflow-state", "switch-primary"),
        "abort": ("--workflow-state", "recover-abort"),
        "restore": ("--workflow-state", "recover-restore"),
        "delete": ("branch", "-d", "task"),
    }[stage]
    events = tmp_path / "commands.jsonl"
    hits = tmp_path / "injection-hits.jsonl"
    source = root / "src/h2h_dev_tools/process.py"
    source.write_text(
        source.read_text()
        + "\n_original_run_command = run_command\n"
        + "def run_command(phase, **kwargs):\n"
        + "    import json\n"
        + f"    with open({str(events)!r}, 'a') as events:\n"
        + "        events.write(json.dumps(phase.argv) + '\\n')\n"
        + f"    target = {command!r}\n"
        + "    if any(phase.argv[index:index + len(target)] == target\n"
        + "           for index in range(len(phase.argv) - len(target) + 1)):\n"
        + f"        with open({str(hits)!r}, 'a') as hits:\n"
        + "            hits.write(json.dumps(phase.argv) + '\\n')\n"
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
    assert "git-flow-merge: process cleanup unproven" not in result.stderr
    commands = [json.loads(line) for line in events.read_text().splitlines()]
    injected = [json.loads(line) for line in hits.read_text().splitlines()]
    assert len(injected) == 1
    assert commands[-1] == injected[0], "No later Git or state action may start"
    assert any(
        injected[0][index : index + len(command)] == list(command)
        for index in range(len(injected[0]) - len(command) + 1)
    )
    assert (root / ".git/h2h-merge-workflow.lock/owner.json").is_file()
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
    base = fixture_repository(
        primary, succeeds=True, primary_hooks=True, profile="documentation"
    )
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
            "scripts/review-merge-candidate.sh",
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
    write_python_executable(
        binary / "codex",
        "import json, os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "assert not any(name.startswith('H2H_REVIEW_') for name in os.environ)\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('codex-cli managed-review-fixture')\n"
        "    raise SystemExit(0)\n"
        "prompt = sys.stdin.read()\n"
        "output = Path(sys.argv[sys.argv.index('--output-last-message') + 1])\n"
        + _PASS_REVIEW
        + f"if {surviving_child!r}:\n"
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
    injection = (
        "original_run_owned = _run_owned\n"
        "def _run_owned(phase, **kwargs):\n"
        "    status = original_run_owned(phase, **kwargs)\n"
        "    if phase.label == 'Code review':\n"
        "        import time as hold_clock\n"
        "        state = Path(os.environ['REVIEW_OWNER_STATE'])\n"
        "        (state / 'ready').touch()\n"
        "        while not (state / 'release').exists():\n"
        "            hold_clock.sleep(0.01)\n"
        f"        if {owner_result!r} == 'crash':\n"
        "            os._exit(1)\n"
        f"        if {owner_result!r} == 'corrupt':\n"
        "            Path(phase.environment['H2H_REVIEW_PENDING_RECEIPT']).write_text('{}')\n"
        "    return status\n\n"
    )
    source = owner.read_text()
    anchor = 'if __name__ == "__main__":'
    assert source.count(anchor) == 1
    source = source.replace(anchor, injection + anchor)
    compile(source, str(owner), "exec")
    owner.write_text(source)
    git(root, "add", ".")
    git(root, "commit", "-m", "test: pause owner after command cleanup")
    state = tmp_path / "state"
    state.mkdir()
    binary = tmp_path / "bin"
    binary.mkdir()
    write_python_executable(
        binary / "codex",
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('codex-cli publication-fixture')\n"
        "    raise SystemExit(0)\n"
        "prompt = sys.stdin.read()\n"
        "output = Path(sys.argv[sys.argv.index('--output-last-message') + 1])\n"
        + _PASS_REVIEW,
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
                    assert (
                        "git-flow-merge: process cleanup unproven"
                        not in (state / "output").read_text()
                    )
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
        write_python_executable(
            root / ".githooks/post-checkout",
            "import os\nfrom pathlib import Path\n"
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
    write_python_executable(
        binary / "codex",
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
    cleanup_deadline = time.monotonic() + (65 if interruption == "timeout" else 35)
    with (
        process.controlled_termination_signals() as controller,
        (state / "output").open("w+b") as output,
    ):
        owner: process.OwnedProcess | None = None
        clean = False
        try:
            with controller.defer():
                owner = process.start_owned_process(
                    process.ProcessCommand(
                        "review interruption fixture",
                        ("scripts/git-flow-merge.sh",),
                        root,
                        environment=os.environ
                        | {
                            "PATH": str(binary)
                            + os.pathsep
                            + os.environ.get("PATH", ""),
                            "MERGE_INTERRUPTION_STATE": str(state),
                            "MERGE_REVIEW_STAGE": stage,
                            "H2H_CODE_REVIEW_TIMEOUT_SECONDS": "40"
                            if interruption == "timeout"
                            else "900",
                        },
                        stdout=subprocess.PIPE if piped_output else output,
                        stderr=subprocess.STDOUT,
                    ),
                    deadline=cleanup_deadline,
                    signal_controller=controller,
                )
            wrapper = owner.process
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
            # Two shards reserve 20% for integration, including six cleanup seconds.
            wrapper.wait(timeout=45 if interruption == "timeout" else 15)
            assert wrapper.returncode != 0, (state / "output").read_text()
            if interruption in {"cancel", "closed-pipe"} or interruption.startswith(
                "backpressure"
            ):
                assert wrapper.returncode == 143
            # Wrapper exit and released locks cannot discharge nested obligations.
            # The canonical outer owner must prove cleanup before fixture rescue.
            _assert_interrupted_workflow_cleanup(
                owner, (state / "output").read_text(), deadline=cleanup_deadline
            )
            with pytest.raises(ProcessLookupError):
                os.killpg(int((state / "group").read_text()), 0)
            clean = True
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
            if owner is not None and not clean:
                # Rescue cannot turn unknown cleanup into a passing assertion.
                with controller.defer():
                    empty = process.terminate_owned_tree(
                        owner, deadline=cleanup_deadline
                    )
                    closed = process.close_owner(owner, deadline=cleanup_deadline)
                    assert empty and closed, "Fixture cleanup remains unproven"
            if owner is not None and owner.process.stdout is not None:
                owner.process.stdout.close()


def _assert_interrupted_workflow_cleanup(
    owner: process.OwnedProcess, output: str, *, deadline: float
) -> None:
    assert "process cleanup unproven" not in output, output
    assert "cleanup could not be proven" not in output, output
    assert process.wait_for_owned_tree_exit(owner, deadline=deadline), output
    assert process.close_owner(owner, deadline=deadline), output


@pytest.mark.skipif(os.name == "nt", reason="Exercises real POSIX process signals")
@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("handoff_signal", [False, True])
@pytest.mark.parametrize("nested", [False, True])
def test_merge_interruption_cleans_gate_descendants_before_restoring_git_state(
    tmp_path: Path, committed: bool, handoff_signal: bool, nested: bool
) -> None:
    root = tmp_path / "repository"
    base = fixture_repository(root, succeeds=True, primary_hooks=False)
    git(root, "switch", "master")
    (root / "primary-only.txt").write_text("preserved primary-only content\n")
    git(root, "add", "primary-only.txt")
    git(root, "commit", "-m", "test: diverge primary before interrupted merge")
    base = git(root, "rev-parse", "HEAD")
    git(root, "switch", "task")
    state = tmp_path / "state"
    state.mkdir()
    (root / "scripts/blocking-gate.py").write_text(_BLOCKING_GATE)
    if nested:
        (root / "scripts/nested-gate.py").write_text(
            "import os, runpy, sys\nfrom pathlib import Path\n"
            "root = Path.cwd()\npackage = root / 'src/h2h_dev_tools'\n"
            "sys.path.insert(0, str(package.parent))\n"
            "runpy.run_path(str(package / 'source_loader.py'))['load_package'](package, 'h2h_dev_tools')\n"
            "from h2h_dev_tools import process\n"
            "state = Path(os.environ['MERGE_INTERRUPTION_STATE'])\n"
            "(state / 'outer-group').write_text(str(os.getpgrp()))\n"
            "phase = process.ProcessCommand('nested gate', (sys.executable, 'scripts/blocking-gate.py'), root)\n"
            "try:\n    result = process.run_command(phase, hard_deadline=None)\n"
            "except process.RunnerSignalInterrupt as error:\n    result = 128 + error.signal_number\n"
            "raise SystemExit(result)\n"
        )
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
        f"    exec .venv/bin/python scripts/{'nested' if nested else 'blocking'}-gate.py\n"
        "fi\n"
    )
    hook = "post-commit"
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
    deadline = time.monotonic() + 25
    with process.controlled_termination_signals() as controller:
        owner: process.OwnedProcess | None = None
        clean = False
        try:
            with controller.defer():
                owner = process.start_owned_process(
                    process.ProcessCommand(
                        "merge interruption fixture",
                        (
                            "bash",
                            str(root / "src/h2h_dev_tools/git-flow-merge.sh"),
                            str(root),
                            "scripts/review-merge-candidate.sh",
                        ),
                        root,
                        environment=os.environ
                        | {"MERGE_INTERRUPTION_STATE": str(state)},
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                    ),
                    deadline=deadline,
                    signal_controller=controller,
                )
            wrapper = owner.process
            while not (state / "ready").exists():
                assert wrapper.poll() is None, wrapper.communicate()[0]
                assert time.monotonic() < deadline - 6, (
                    "The gate descendant never started"
                )
                time.sleep(0.01)
            if committed:
                assert git(root, "rev-parse", "master") != base
            if not handoff_signal:
                wrapper.send_signal(signal.SIGTERM)
            time.sleep(0.05)
            wrapper.send_signal(signal.SIGTERM)
            try:
                raw_output, _ = wrapper.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                pytest.fail(
                    "TERM did not stop the merge gate and its owned descendants"
                )
            output = raw_output.decode("utf-8", errors="replace")
            (state / "wrapper-output.txt").write_text(output)
            (state / "wrapper-returncode.txt").write_text(str(wrapper.returncode))
            assert wrapper.returncode == 143, output
            # Exit 143 and released file locks do not prove that each cooperating
            # owner discharged its obligation. Verify before any fixture rescue.
            _assert_interrupted_workflow_cleanup(owner, output, deadline=deadline)
            clean = True
            _assert_gate_lock_released(state / "leader-lock")
            _assert_gate_lock_released(state / "child-lock")
            assert not (root / ".git/MERGE_HEAD").exists(), output
            assert git(root, "rev-parse", "task") == task
            if committed:
                assert git(root, "branch", "--show-current") == "master"
                parents = git(root, "show", "-s", "--format=%P", "master").split()
                assert parents == [base, task]
            else:
                assert git(root, "branch", "--show-current") == "task"
                assert git(root, "rev-parse", "master") == base
            assert git(root, "status", "--porcelain") == ""
            assert not (root / ".git/h2h-merge-workflow.lock").exists(), output
            assert (
                git(root, "show", "master:primary-only.txt")
                == "preserved primary-only content"
            )
            if committed:
                completed = git(root, "rev-parse", "master")
                git(root, "switch", "task")
                retry = owned_fixture_command(
                    root, ("scripts/git-flow-merge.sh", "--cleanup-only")
                )
                assert retry.returncode == 0, retry.stdout + retry.stderr
                assert git(root, "rev-parse", "master") == completed
                assert not git(root, "branch", "--list", "task")
                assert not (root / ".git/h2h-merge-workflow.lock").exists()
        finally:
            if owner is not None and not clean:
                # Rescue is never a passing oracle. Retain an uncertain ledger;
                # force-killing its owner cannot manufacture nested cleanup proof.
                with controller.defer():
                    empty = process.terminate_owned_tree(owner, deadline=deadline)
                    closed = process.close_owner(owner, deadline=deadline)
                    assert empty and closed, "Fixture cleanup remains unproven"
            if owner is not None and owner.process.stdout is not None:
                owner.process.stdout.close()


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


@pytest.mark.skipif(os.name != "posix", reason="Real isolated shell diagnostic entry")
@pytest.mark.parametrize("failure", ["ownership", "exit-zero", "exit-125"])
def test_failed_diagnostic_preserves_candidate_and_stops_further_commands(
    tmp_path: Path, failure: str
) -> None:
    root = tmp_path / "repository"
    base = fixture_repository(root, succeeds=False)
    calls = tmp_path / "diagnostic-calls"
    commands = tmp_path / "commands.jsonl"
    boundary = tmp_path / "commands-before-diagnostic.jsonl"
    receipts = tmp_path / "receipts.tsv"
    record_workflow_receipts(root, receipts)
    exports = tmp_path / "exports"
    exports.mkdir()
    source = root / "src/h2h_dev_tools/process.py"
    outcome = {
        "ownership": "raise ProcessOwnershipError('fixture diagnostic cleanup unproven')",
        "exit-zero": "raise SystemExit(0)",
        "exit-125": "raise SystemExit(125)",
    }[failure]
    source.write_text(
        source.read_text()
        + "\n_original_run_command = run_command\n"
        + "def run_command(phase, **kwargs):\n"
        + "    import json\n"
        + f"    with open({str(commands)!r}, 'a') as events:\n"
        + "        events.write(json.dumps(phase.argv) + '\\n')\n"
        + "    return _original_run_command(phase, **kwargs)\n"
        + "_original_report_diagnostic = report_diagnostic\n"
        + "def report_diagnostic(message, **kwargs):\n"
        + "    if message.startswith('git-flow-merge:'):\n"
        + f"        with open({str(commands)!r}, 'rb') as before, open({str(boundary)!r}, 'wb') as saved:\n"
        + "            saved.write(before.read())\n"
        + f"        with open({str(calls)!r}, 'a') as events: events.write('called\\n')\n"
        + f"        {outcome}\n"
        + "    return _original_report_diagnostic(message, **kwargs)\n"
    )
    git(root, "add", ".")
    git(root, "commit", "-m", "test: stop after diagnostic cleanup failure")
    task = git(root, "rev-parse", "HEAD")
    output = tmp_path / "output"
    with output.open("wb") as stream:
        status = process.run_command(
            process.ProcessCommand(
                "failed diagnostic workflow",
                ("scripts/git-flow-merge.sh",),
                root,
                environment=os.environ | {"TMPDIR": str(exports)},
                stdout=stream,
                stderr=stream,
            ),
            hard_deadline=time.monotonic() + 20,
        )
    assert status == 1, output.read_text()
    assert calls.read_text().splitlines() == ["called"]
    events = [json.loads(line) for line in commands.read_text().splitlines()]
    assert sum(event[-2:] == ["commit", "--no-edit"] for event in events) == 1
    # Commit hooks legitimately issue nested owned queries. The diagnostic
    # boundary, not the last outer command's position, closes command admission.
    assert commands.read_bytes() == boundary.read_bytes(), (
        "Commands started after diagnostic failure"
    )
    assert git(root, "rev-parse", "master") == base
    assert git(root, "rev-parse", "task") == task
    assert git(root, "branch", "--show-current") == "master"
    assert (root / ".git/MERGE_HEAD").exists()
    retained = list(exports.glob("h2h-merge-owner.*"))
    assert len(retained) == 1
    recorded = [line.split("\t") for line in receipts.read_text().splitlines()]
    assert [entry[0] for entry in recorded[-2:]] == ["process", "diagnostic"]
    commit_receipt, diagnostic_receipt = (Path(entry[1]) for entry in recorded[-2:])
    assert commit_receipt.parent == diagnostic_receipt.parent == retained[0]
    assert commit_receipt.read_text() == "owned_tree_empty\n"
    assert not diagnostic_receipt.exists()


@pytest.mark.skipif(
    os.name != "posix", reason="Real venv site startup and shell recovery"
)
@pytest.mark.parametrize("startup", ["sitecustomize", "pth"])
@pytest.mark.parametrize("launch", ["diagnostic", "recovery", "owner", "source-probe"])
def test_workflow_and_diagnostic_launchers_exclude_site_startup_code(
    tmp_path: Path, startup: str, launch: str
) -> None:
    root = tmp_path / "repository"
    base = fixture_repository(root, succeeds=False)
    task = git(root, "rev-parse", "HEAD")
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
    control = tmp_path / "startup-control"
    marker = tmp_path / "forbidden-startup"
    condition = {
        "diagnostic": "'--workflow-diagnostic' in sys.orig_argv",
        "recovery": "'--recovery' in sys.orig_argv",
        "owner": "'--cleanup-receipt' in sys.orig_argv and '--recovery' not in sys.orig_argv",
        "source-probe": "'--print-source-files' in sys.orig_argv",
    }[launch]
    code = (
        "import subprocess, sys, time\nfrom pathlib import Path\n"
        "if '--site-control' in sys.orig_argv:\n"
        f"    Path({str(control)!r}).touch()\n"
        f"if {condition}:\n"
        f"    Path({str(marker)!r}).touch()\n"
        "    subprocess.Popen([sys.executable, '-I', '-S', '-c', 'import time; time.sleep(60)'])\n"
        "    time.sleep(60)\n"
    )
    if startup == "sitecustomize":
        (site / "sitecustomize.py").write_text(code)
    else:
        (site / "diagnostic-startup.pth").write_text(f"import sys; exec({code!r})\n")
    subprocess.run(
        (str(python), "-I", "-B", "-c", "pass", "--site-control"),
        check=True,
        capture_output=True,
        timeout=5,
    )
    assert control.exists(), "The regression must install a real active startup hook"
    output = tmp_path / "output"
    with output.open("wb") as stream:
        status = process.run_command(
            process.ProcessCommand(
                "site-isolated failure diagnostic",
                ("scripts/git-flow-merge.sh",),
                root,
                stdout=stream,
                stderr=stream,
            ),
            hard_deadline=time.monotonic() + 15,
        )
    assert status == 1, output.read_text()
    assert not marker.exists(), f"{launch} launch executed site startup code"
    assert "merge commit or release gate failed" in output.read_text()
    assert git(root, "rev-parse", "master") == base
    assert git(root, "rev-parse", "task") == task
    assert git(root, "branch", "--show-current") == "task"
    assert not (root / ".git/MERGE_HEAD").exists()
    assert git(root, "status", "--porcelain") == ""


@pytest.mark.skipif(
    os.name != "posix", reason="Real diagnostic launcher signal handoff"
)
@pytest.mark.parametrize("handoff_signal", [False, True])
def test_interrupted_diagnostic_is_reaped_before_preserving_candidate(
    tmp_path: Path, *, handoff_signal: bool
) -> None:
    root = tmp_path / "repository"
    base = fixture_repository(root, succeeds=False)
    ready = tmp_path / "diagnostic-ready"
    receipts = tmp_path / "receipts.tsv"
    record_workflow_receipts(root, receipts)
    exports = tmp_path / "exports"
    exports.mkdir()
    source = root / "src/h2h_dev_tools/process.py"
    source.write_text(
        source.read_text()
        + "\n_original_report_diagnostic = report_diagnostic\n"
        + "def report_diagnostic(message, **kwargs):\n"
        + "    if message.startswith('git-flow-merge:'):\n"
        + f"        Path({str(ready)!r}).write_text(str(os.getpid()))\n"
        + "        time.sleep(60)\n"
        + "    return _original_report_diagnostic(message, **kwargs)\n"
    )
    if handoff_signal:
        shell = root / "src/h2h_dev_tools/git-flow-merge.sh"
        shell.write_text(
            shell.read_text().replace(
                "    active_pid=$!\n",
                '    if [[ "$active_owner" == diagnostic ]]; then\n'
                f"        while [[ ! -e {str(ready)!r} ]]; do sleep 0.01; done\n"
                '        kill -TERM "$$"\n'
                "    fi\n"
                "    active_pid=$!\n",
            )
        )
    git(root, "add", ".")
    git(root, "commit", "-m", "test: interrupt diagnostic launcher handoff")
    task = git(root, "rev-parse", "HEAD")
    deadline = time.monotonic() + 15
    with process.controlled_termination_signals() as controller:
        owner: process.OwnedProcess | None = None
        clean = False
        try:
            with controller.defer():
                owner = process.start_owned_process(
                    process.ProcessCommand(
                        "interrupted diagnostic launcher",
                        ("scripts/git-flow-merge.sh",),
                        root,
                        environment=os.environ | {"TMPDIR": str(exports)},
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                    ),
                    deadline=deadline,
                    signal_controller=controller,
                )
            wrapper = owner.process
            while not ready.exists():
                assert wrapper.poll() is None, wrapper.communicate()[0]
                assert time.monotonic() < deadline - 6
                time.sleep(0.01)
            if not handoff_signal:
                wrapper.send_signal(signal.SIGTERM)
            raw_output, _ = wrapper.communicate(timeout=5)
            output = raw_output.decode("utf-8", errors="replace")
            assert wrapper.returncode == 143, output
            assert process.wait_for_owned_tree_exit(owner, deadline=deadline)
            assert process.close_owner(owner, deadline=deadline)
            clean = True
            with pytest.raises(ProcessLookupError):
                os.kill(int(ready.read_text()), 0)
            assert git(root, "rev-parse", "master") == base
            assert git(root, "rev-parse", "task") == task
            assert git(root, "branch", "--show-current") == "master"
            assert (root / ".git/MERGE_HEAD").exists()
            retained = list(exports.glob("h2h-merge-owner.*"))
            assert len(retained) == 1
            recorded = [line.split("\t") for line in receipts.read_text().splitlines()]
            assert recorded[-1][0] == "diagnostic"
            diagnostic_receipt = Path(recorded[-1][1])
            assert diagnostic_receipt.parent == retained[0]
            assert not diagnostic_receipt.exists()
        finally:
            if owner is not None and not clean:
                with controller.defer():
                    empty = process.terminate_owned_tree(owner, deadline=deadline)
                    closed = process.close_owner(owner, deadline=deadline)
                    assert empty and closed, "Diagnostic fixture cleanup is unproven"
            if owner is not None and owner.process.stdout is not None:
                owner.process.stdout.close()


@pytest.mark.skipif(os.name != "posix", reason="Real POSIX descendant cleanup oracle")
@pytest.mark.parametrize("surviving_descendant", [False, True])
def test_interruption_oracle_tracks_non_locking_descendants_after_wrapper_exit(
    tmp_path: Path, surviving_descendant: bool
) -> None:
    child_pid = tmp_path / "child-pid"
    lock = tmp_path / "wrapper-lock"
    child = (
        "import os, signal, sys, time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        f"time.sleep({60 if surviving_descendant else 0})\n"
    )
    wrapper_source = (
        "import fcntl, subprocess, sys, time\nfrom pathlib import Path\n"
        "lock = Path(sys.argv[1]).open('a')\n"
        "fcntl.flock(lock, fcntl.LOCK_EX)\n"
        f"child = subprocess.Popen([sys.executable, '-I', '-c', {child!r}, sys.argv[2]])\n"
        "while not Path(sys.argv[2]).exists():\n    time.sleep(0.01)\n"
        + ("" if surviving_descendant else "child.wait()\n")
        + "raise SystemExit(143)\n"
    )
    deadline = time.monotonic() + 6
    with process.controlled_termination_signals() as controller:
        owner: process.OwnedProcess | None = None
        clean = False
        try:
            with controller.defer():
                owner = process.start_owned_process(
                    process.ProcessCommand(
                        "non-locking descendant oracle control",
                        (
                            sys.executable,
                            "-I",
                            "-c",
                            wrapper_source,
                            str(lock),
                            str(child_pid),
                        ),
                        tmp_path,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    ),
                    deadline=deadline,
                    signal_controller=controller,
                )
            assert owner.process.wait(timeout=3) == 143
            _assert_gate_lock_released(lock)
            assert owner.posix_scope is not None
            assert not owner.posix_scope.has_pending(deadline=deadline)
            if surviving_descendant:
                os.kill(int(child_pid.read_text()), 0)
                with pytest.raises(AssertionError):
                    _assert_interrupted_workflow_cleanup(
                        owner, "normal signal exit", deadline=time.monotonic() + 0.1
                    )
            else:
                with pytest.raises(ProcessLookupError):
                    os.kill(int(child_pid.read_text()), 0)
                _assert_interrupted_workflow_cleanup(
                    owner, "normal signal exit", deadline=deadline
                )
                clean = True
        finally:
            # Only after observing the oracle's result may the fixture rescue
            # its deliberately surviving child; released locks are insufficient.
            if owner is not None and not clean:
                with controller.defer():
                    assert process.terminate_owned_tree(owner, deadline=deadline)
                    assert process.close_owner(owner, deadline=deadline)


@pytest.mark.skipif(os.name != "posix", reason="Real POSIX launch interruption cleanup")
@pytest.mark.parametrize(
    "fixture",
    [
        "hook",
        "review",
        "non-locking",
        "cleanliness",
        "capture",
        "merge-interruption",
        "diagnostic",
        "nested-obligation",
        "publication-stage",
        "publication-terminal",
    ],
)
@pytest.mark.parametrize("signal_number", [signal.SIGINT, signal.SIGTERM])
def test_interruption_fixtures_clean_owner_when_deferred_launch_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture: str, signal_number: int
) -> None:
    start = process.start_owned_process
    close = process.close_owner
    owners: list[tuple[process.OwnedProcess, float]] = []
    closed: list[tuple[process.OwnedProcess, float, bool]] = []

    def start_and_interrupt(
        command: process.ProcessCommand,
        *,
        deadline: float,
        signal_controller: process.SignalController | None,
    ) -> process.OwnedProcess:
        assert signal_controller is not None
        assert signal_controller.defer_depth > 0
        owner = start(command, deadline=deadline, signal_controller=signal_controller)
        owners.append((owner, deadline))
        signal_controller.receive(signal_number)
        return owner

    def record_close(
        owner: process.OwnedProcess,
        *,
        deadline: float,
        diagnostic: Callable[[str], None] | None = None,
    ) -> bool:
        result = close(owner, deadline=deadline, diagnostic=diagnostic)
        closed.append((owner, deadline, result))
        return result

    monkeypatch.setattr(process, "start_owned_process", start_and_interrupt)
    monkeypatch.setattr(process, "close_owner", record_close)
    try:
        with pytest.raises(
            (KeyboardInterrupt, process.RunnerSignalInterrupt)
        ) as caught:
            if fixture == "hook":
                test_all_mutating_workflow_hooks_are_owned(
                    tmp_path, "checkout", "cancel"
                )
            elif fixture == "review":
                test_merge_review_stage_owns_preflight_and_crashed_reviewer_descendants(
                    tmp_path, "execute", "cancel"
                )
            elif fixture == "non-locking":
                test_interruption_oracle_tracks_non_locking_descendants_after_wrapper_exit(
                    tmp_path, True
                )
            elif fixture == "cleanliness":
                test_cleanliness_stage_owns_git_callbacks_before_task_cleanup(
                    tmp_path, "normal"
                )
            elif fixture == "capture":
                test_candidate_export_uses_owned_capture_before_review(
                    tmp_path, "normal"
                )
            elif fixture == "merge-interruption":
                test_merge_interruption_cleans_gate_descendants_before_restoring_git_state(
                    tmp_path, committed=False, handoff_signal=False, nested=False
                )
            elif fixture == "diagnostic":
                test_interrupted_diagnostic_is_reaped_before_preserving_candidate(
                    tmp_path, handoff_signal=False
                )
            elif fixture == "nested-obligation":
                test_interruption_oracle_rejects_exited_group_with_unclosed_obligation(
                    tmp_path
                )
            else:
                test_cancel_merge_during_receipt_publication_restores_task(
                    tmp_path, fixture.removeprefix("publication-")
                )
        if signal_number == signal.SIGINT:
            assert isinstance(caught.value, KeyboardInterrupt)
        else:
            assert isinstance(caught.value, process.RunnerSignalInterrupt)
            assert caught.value.signal_number == signal_number
        assert len(owners) == 1
        owner, deadline = owners[0]
        assert closed == [(owner, deadline, True)]
        with pytest.raises(ProcessLookupError):
            os.killpg(owner.process.pid, 0)
    finally:
        # Observe the fixture's canonical close before rescuing a failed control.
        failure = sys.exception()
        for owner, deadline in owners:
            if (owner, deadline, True) not in closed:
                try:
                    with process.controlled_termination_signals() as controller:
                        with controller.defer():
                            try:
                                if fixture == "nested-obligation":
                                    _finish_unlaunched_scope_negative_control(
                                        owner,
                                        tmp_path / "unlaunched-scope.json",
                                        deadline=deadline,
                                    )
                            finally:
                                empty = process.terminate_owned_tree(
                                    owner, deadline=deadline
                                )
                                released = close(owner, deadline=deadline)
                                assert empty and released
                except BaseException as rescue_error:
                    if failure is None:
                        raise
                    failure.add_note(
                        f"Launch control rescue also failed: {rescue_error}"
                    )


def _finish_unlaunched_scope_negative_control(
    owner: process.OwnedProcess, marker: Path, *, deadline: float
) -> None:
    # Only the exact negative-control program below creates this marker. It
    # creates one closed-admission scope and never launches a child from it.
    # First observe that program's completion: checking the marker before then
    # races scope creation when interruption is raised at the launch handoff.
    from h2h_dev_tools._process_scope import PosixScope, _Capability

    owner.process.wait(timeout=max(0.0, deadline - time.monotonic()))
    if marker.exists():
        assert owner.posix_scope is not None
        unlaunched = PosixScope(
            _Capability.decode(marker.read_text()),
            owner.posix_scope.capability,
            proven_empty=True,
        )
        unlaunched.close(deadline=deadline)


@pytest.mark.skipif(os.name != "posix", reason="Real POSIX nested obligation oracle")
def test_interruption_oracle_rejects_exited_group_with_unclosed_obligation(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "unlaunched-scope.json"
    package = Path(process.__file__).resolve().parent
    child = (
        "import runpy, sys, time\nfrom pathlib import Path\n"
        "package = Path(sys.argv[1])\n"
        "runpy.run_path(str(package / 'source_loader.py'))['load_package'](package, 'h2h_dev_tools')\n"
        "from h2h_dev_tools._process_scope import PosixScope\n"
        "scope = PosixScope.create(deadline=time.monotonic() + 3)\n"
        "scope.close_admission(deadline=time.monotonic() + 3)\n"
        "Path(sys.argv[2]).write_text(scope.capability.encode())\n"
        # Deliberately omit close for an admitted scope that never launched a
        # child. Actual process exit and an empty group cannot discharge it.
        "raise SystemExit(143)\n"
    )
    deadline = time.monotonic() + 6
    with process.controlled_termination_signals() as controller:
        owner: process.OwnedProcess | None = None
        try:
            with controller.defer():
                owner = process.start_owned_process(
                    process.ProcessCommand(
                        "missing nested cleanup negative control",
                        (
                            sys.executable,
                            "-I",
                            "-B",
                            "-c",
                            child,
                            str(package),
                            str(marker),
                        ),
                        tmp_path,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    ),
                    deadline=deadline,
                    signal_controller=controller,
                )
            assert owner.process.wait(timeout=3) == 143
            with pytest.raises(ProcessLookupError):
                os.killpg(owner.process.pid, 0)
            assert owner.posix_scope is not None
            assert owner.posix_scope.has_pending(deadline=deadline)
            with pytest.raises(AssertionError):
                _assert_interrupted_workflow_cleanup(
                    owner, "normal signal exit", deadline=time.monotonic() + 0.1
                )
        finally:
            if owner is not None:
                with controller.defer():
                    try:
                        _finish_unlaunched_scope_negative_control(
                            owner, marker, deadline=deadline
                        )
                    finally:
                        empty = process.terminate_owned_tree(owner, deadline=deadline)
                        closed = process.close_owner(owner, deadline=deadline)
                        assert empty and closed


def _ignored_file_fixture(root: Path) -> None:
    fixture_repository(root, succeeds=True, primary_hooks=True)
    # These native Git obstruction cases isolate checkout/merge preservation.
    # Full review and gate behavior is exercised by the other workflow tests.
    for name in ("pre-commit", "commit-msg"):
        executable(root / ".githooks" / name, "#!/bin/sh\nexit 0\n")
    executable(
        root / ".githooks/pre-merge-commit",
        '#!/bin/sh\nprintf "checked\\n" >> "$(git rev-parse --git-path fixture-gate)"\n',
    )
    git(root, "add", ".githooks")
    git(root, "commit", "-m", "test: isolate native Git file preservation")


@pytest.mark.parametrize("private_file", [False, True])
def test_initial_switch_preserves_ignored_private_files(
    tmp_path: Path, private_file: bool
) -> None:
    root = tmp_path / "repository"
    _ignored_file_fixture(root)
    protected = root / "private.txt"
    protected.write_bytes(b"primary tracked bytes\n")
    git(root, "add", "private.txt")
    git(root, "commit", "-m", "test: establish tracked primary file")
    primary = git(root, "rev-parse", "HEAD")
    git(root, "update-ref", "refs/heads/master", primary)
    git(root, "rm", "private.txt")
    with (root / ".gitignore").open("a") as ignore:
        ignore.write("private.txt\n")
    git(root, "add", ".gitignore")
    git(root, "commit", "-m", "test: stop tracking private file on task")
    task = git(root, "rev-parse", "HEAD")
    private_bytes = b"private bytes must survive\x00\xff\n"
    if private_file:
        protected.write_bytes(private_bytes)
        assert git(root, "check-ignore", "private.txt") == "private.txt"
    assert git(root, "status", "--porcelain") == ""

    result = subprocess.run(
        (
            "bash",
            "src/h2h_dev_tools/git-flow-merge.sh",
            str(root),
            "scripts/review-merge-candidate.sh",
        ),
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )

    if private_file:
        assert protected.read_bytes() == private_bytes
        assert result.returncode != 0, result.stdout + result.stderr
        assert "checkout obstruction" in result.stderr
        assert git(root, "branch", "--show-current") == "task"
        assert git(root, "rev-parse", "master") == primary
        assert git(root, "rev-parse", "task") == task
        assert git(root, "write-tree") == git(root, "rev-parse", "task^{tree}")
        assert not (root / ".git/fixture-gate").exists()
        assert not (root / ".git/fixture-review-events").exists()
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert git(root, "branch", "--show-current") == "master"
        assert git(root, "show", "-s", "--format=%P", "HEAD").split() == [primary, task]
        assert git(root, "branch", "--list", "task") == ""
        assert not protected.exists()
    assert git(root, "status", "--porcelain") == ""
    assert not (root / ".git/MERGE_HEAD").exists()
    assert not (root / ".git/h2h-merge-workflow.lock").exists()


@pytest.mark.parametrize("cleanup_only", [False, True])
@pytest.mark.parametrize("private_file", [False, True])
def test_noop_cleanup_switch_preserves_ignored_private_files(
    tmp_path: Path, cleanup_only: bool, private_file: bool
) -> None:
    root = tmp_path / "repository"
    _ignored_file_fixture(root)
    with (root / ".gitignore").open("a") as ignore:
        ignore.write("private.txt\n")
    git(root, "add", ".gitignore")
    git(root, "commit", "-m", "test: ignore private file on task")
    task = git(root, "rev-parse", "HEAD")
    git(root, "update-ref", "refs/heads/master", task)
    git(root, "switch", "master")
    protected = root / "private.txt"
    tracked_bytes = b"new primary tracked bytes\n"
    protected.write_bytes(tracked_bytes)
    git(root, "add", "-f", "private.txt")
    git(root, "commit", "-m", "test: track previously ignored path on primary")
    primary = git(root, "rev-parse", "HEAD")
    git(root, "switch", "task")
    private_bytes = b"private cleanup bytes\x00\xff\n"
    if private_file:
        protected.write_bytes(private_bytes)
        assert git(root, "check-ignore", "private.txt") == "private.txt"
    assert git(root, "status", "--porcelain") == ""

    result = subprocess.run(
        ("scripts/git-flow-merge.sh", *(["--cleanup-only"] if cleanup_only else [])),
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )

    if private_file:
        assert protected.read_bytes() == private_bytes
        assert result.returncode != 0, result.stdout + result.stderr
        assert "checkout obstruction" in result.stderr
        assert git(root, "branch", "--show-current") == "task"
        assert git(root, "rev-parse", "task") == task
        assert git(root, "write-tree") == git(root, "rev-parse", "task^{tree}")
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert protected.read_bytes() == tracked_bytes
        assert git(root, "branch", "--show-current") == "master"
        assert git(root, "branch", "--list", "task") == ""
    assert git(root, "rev-parse", "master") == primary
    assert git(root, "status", "--porcelain") == ""
    assert not (root / ".git/fixture-gate").exists()
    assert not (root / ".git/fixture-review-events").exists()
    assert not (root / ".git/MERGE_HEAD").exists()
    assert not (root / ".git/h2h-merge-workflow.lock").exists()


@pytest.mark.parametrize("private_file", [False, True])
def test_merge_preserves_ignored_private_files_in_linked_primary(
    tmp_path: Path, private_file: bool
) -> None:
    primary_root = tmp_path / "primary"
    root = primary_root
    _ignored_file_fixture(root)
    with (root / ".gitignore").open("a") as ignore:
        ignore.write("private.txt\n")
    git(root, "add", ".gitignore")
    git(root, "commit", "-m", "test: establish ignored path on primary")
    primary = git(root, "rev-parse", "HEAD")
    git(root, "update-ref", "refs/heads/master", primary)
    tracked_bytes = b"new task tracked bytes\n"
    (root / "private.txt").write_bytes(tracked_bytes)
    git(root, "add", "-f", "private.txt")
    git(root, "commit", "-m", "test: track ignored path on task")
    task = git(root, "rev-parse", "HEAD")
    git(primary_root, "switch", "master")
    root = tmp_path / "linked-task"
    git(primary_root, "worktree", "add", str(root), "task")
    local_bin = root / ".venv/bin"
    local_bin.mkdir(parents=True)
    (local_bin / "python").symlink_to(sys.executable)
    protected = primary_root / "private.txt"
    private_bytes = b"private merge bytes\x00\xff\n"
    if private_file:
        protected.write_bytes(private_bytes)
        assert git(primary_root, "check-ignore", "private.txt") == "private.txt"
    assert git(primary_root, "status", "--porcelain") == ""
    common = Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    metadata = Path(
        git(primary_root, "rev-parse", "--path-format=absolute", "--git-dir")
    )

    result = subprocess.run(
        (
            "bash",
            "src/h2h_dev_tools/git-flow-merge.sh",
            str(root),
            "scripts/review-merge-candidate.sh",
        ),
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )

    if private_file:
        assert protected.read_bytes() == private_bytes
        assert result.returncode != 0, result.stdout + result.stderr
        assert "Untracked or ignored" in result.stderr
        assert git(primary_root, "rev-parse", "master") == primary
        assert git(primary_root, "rev-parse", "task") == task
        assert git(primary_root, "write-tree") == git(
            primary_root, "rev-parse", "master^{tree}"
        )
        assert root.exists() and git(root, "branch", "--show-current") == "task"
        assert not (metadata / "fixture-gate").exists()
        assert not (common / "fixture-review-events").exists()
    else:
        result = _retry_retained_linked_task_cleanup(primary_root, root, result)
        assert result.returncode == 0, result.stdout + result.stderr
        assert protected.read_bytes() == tracked_bytes
        assert git(primary_root, "show", "-s", "--format=%P", "HEAD").split() == [
            primary,
            task,
        ]
        assert git(primary_root, "branch", "--list", "task") == ""
        assert not root.exists()
    assert git(primary_root, "branch", "--show-current") == "master"
    assert git(primary_root, "status", "--porcelain") == ""
    assert not (metadata / "MERGE_HEAD").exists()
    assert not (common / "h2h-merge-workflow.lock").exists()


def _retry_retained_linked_task_cleanup(
    primary: Path, task: Path, result: subprocess.CompletedProcess[str]
) -> subprocess.CompletedProcess[str]:
    assert result.returncode != 0, result.stdout + result.stderr
    assert "task worktree remains" in result.stderr
    assert task.is_dir() and (task / ".venv/bin/python").is_symlink()
    assert git(primary, "branch", "--show-current") == "master"
    assert git(task, "branch", "--show-current") == "task"
    assert not (primary / ".git/h2h-merge-workflow.lock").exists()
    committed = git(primary, "rev-parse", "HEAD")
    assert {
        path.relative_to(task / ".venv").as_posix()
        for path in (task / ".venv").rglob("*")
    } == {"bin", "bin/python"}
    # Only this fixture-owned synthetic environment is removed after inspection.
    # The production workflow must retain arbitrary material, including .venv.
    shutil.rmtree(task / ".venv")
    retry = subprocess.run(
        ("scripts/git-flow-merge.sh", "--cleanup-only", "task"),
        cwd=primary,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert git(primary, "rev-parse", "HEAD") == committed
    return retry


@pytest.mark.parametrize(
    "material", ["none", "file", "empty-directory", "symlink", "venv"]
)
def test_named_cleanup_uses_primary_environment_and_preserves_all_private_material(
    tmp_path: Path, material: str
) -> None:
    primary = tmp_path / "primary"
    _ignored_file_fixture(primary)
    with (primary / ".gitignore").open("a") as ignore:
        ignore.write("private*\n")
    git(primary, "add", ".gitignore")
    git(primary, "commit", "-m", "test: ignored cleanup material")
    task_oid = git(primary, "rev-parse", "HEAD")
    git(primary, "update-ref", "refs/heads/master", task_oid)
    git(primary, "switch", "master")
    task = tmp_path / "task"
    git(primary, "worktree", "add", str(task), "task")
    external = tmp_path / "external"
    external.write_bytes(b"external must remain\x00\xff")
    protected = task / "private-material"
    if material == "file":
        protected.write_bytes(b"private must remain\x00\xff")
    elif material == "empty-directory":
        protected.mkdir()
    elif material == "symlink":
        protected.symlink_to(external)
    elif material == "venv":
        protected = task / ".venv/personal-notes.txt"
        protected.parent.mkdir()
        protected.write_bytes(b"private must remain\x00\xff")
    assert not (task / ".venv/bin/python").exists()
    assert git(task, "status", "--porcelain") == ""

    result = subprocess.run(
        ("scripts/git-flow-merge.sh", "--cleanup-only", "task"),
        cwd=primary,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )

    assert git(primary, "rev-parse", "HEAD") == task_oid
    assert git(primary, "branch", "--show-current") == "master"
    assert external.read_bytes() == b"external must remain\x00\xff"
    assert not (primary / ".git/h2h-merge-workflow.lock").exists()
    assert not (primary / ".git/MERGE_HEAD").exists()
    assert not (primary / ".git/fixture-review-events").exists()
    assert not (primary / ".git/fixture-gate").exists()
    if material == "none":
        assert result.returncode == 0, result.stdout + result.stderr
        assert not task.exists()
        assert git(primary, "branch", "--list", "task") == ""
    else:
        assert result.returncode != 0, result.stdout + result.stderr
        assert "task worktree remains" in result.stderr
        assert git(task, "branch", "--show-current") == "task"
        assert git(primary, "rev-parse", "task") == task_oid
        if material == "empty-directory":
            assert protected.is_dir() and list(protected.iterdir()) == []
        elif material == "symlink":
            assert protected.is_symlink() and protected.readlink() == external
        else:
            assert protected.read_bytes() == b"private must remain\x00\xff"


def test_named_cleanup_deletes_integrated_branch_without_a_worktree(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "primary"
    _ignored_file_fixture(primary)
    task_oid = git(primary, "rev-parse", "HEAD")
    git(primary, "update-ref", "refs/heads/master", task_oid)
    git(primary, "switch", "master")

    result = subprocess.run(
        ("scripts/git-flow-merge.sh", "--cleanup-only", "task"),
        cwd=primary,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert git(primary, "rev-parse", "HEAD") == task_oid
    assert git(primary, "branch", "--list", "task") == ""
    assert (primary / ".venv/bin/python").is_symlink()
    assert not (primary / ".git/h2h-merge-workflow.lock").exists()
    assert not (primary / ".git/MERGE_HEAD").exists()
    assert not (primary / ".git/fixture-review-events").exists()
    assert not (primary / ".git/fixture-gate").exists()


@pytest.mark.parametrize(
    "invalid",
    [
        "task-caller",
        "unmerged",
        "primary-target",
        "missing-target",
        "detached",
        "missing-environment",
    ],
)
def test_named_cleanup_rejects_invalid_caller_or_task_without_mutation(
    tmp_path: Path, invalid: str
) -> None:
    root = tmp_path / "repository"
    _ignored_file_fixture(root)
    primary = git(root, "rev-parse", "HEAD")
    git(root, "update-ref", "refs/heads/master", primary)
    if invalid == "unmerged":
        (root / "README.md").write_text("unmerged task\n")
        git(root, "add", "README.md")
        git(root, "commit", "-m", "test: retain unmerged task")
    task = git(root, "rev-parse", "task")
    if invalid != "task-caller":
        git(root, "switch", "master")
    if invalid == "detached":
        git(root, "switch", "--detach")
    if invalid == "missing-environment":
        shutil.rmtree(root / ".venv")
    branch = git(root, "branch", "--show-current")
    head = git(root, "rev-parse", "HEAD")
    target = {"primary-target": "master", "missing-target": "absent"}.get(
        invalid, "task"
    )

    result = subprocess.run(
        ("scripts/git-flow-merge.sh", "--cleanup-only", target),
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )

    assert result.returncode != 0, result.stdout + result.stderr
    diagnostic = {
        "task-caller": "named cleanup must run from the primary checkout",
        "unmerged": "task contains commits not present in primary",
        "primary-target": "primary cannot be the cleanup task",
        "missing-target": "task branch is not a commit",
        "detached": "detached HEAD is not supported",
        "missing-environment": "missing repository-local Python",
    }[invalid]
    assert diagnostic in result.stderr
    assert git(root, "rev-parse", "master") == primary
    assert git(root, "rev-parse", "task") == task
    assert git(root, "rev-parse", "HEAD") == head
    assert git(root, "branch", "--show-current") == branch
    assert git(root, "status", "--porcelain") == ""
    assert not (root / ".git/h2h-merge-workflow.lock").exists()
    assert not (root / ".git/MERGE_HEAD").exists()


def test_canonical_hook_installation_options_allow_normal_merge(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    _ignored_file_fixture(root)
    # Candidate commits must use the real pending-merge hook delegation.
    shutil.copy2(_ROOT / ".githooks/pre-commit", root / ".githooks/pre-commit")
    git(root, "add", ".githooks/pre-commit")
    git(root, "commit", "-m", "test: use canonical candidate hook delegation")
    primary = git(root, "rev-parse", "master")
    task = git(root, "rev-parse", "task")
    installed = subprocess.run(
        ("bash", "src/h2h_dev_tools/install-git-hooks.sh", str(root)),
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    assert git(root, "config", "--local", "branch.master.mergeOptions") == "--no-ff"
    configuration = (root / ".git/config").read_bytes()

    result = subprocess.run(
        (
            "bash",
            "src/h2h_dev_tools/git-flow-merge.sh",
            str(root),
            "scripts/review-merge-candidate.sh",
        ),
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert git(root, "branch", "--show-current") == "master"
    assert git(root, "show", "-s", "--format=%P", "HEAD").split() == [primary, task]
    assert git(root, "branch", "--list", "task") == ""
    assert (root / ".git/config").read_bytes() == configuration
    assert git(root, "status", "--porcelain") == ""
    assert not (root / ".git/MERGE_HEAD").exists()
    assert not (root / ".git/h2h-merge-workflow.lock").exists()
    assert (root / ".git/fixture-gate").read_text() == "checked\n"
    assert (root / ".git/fixture-review-events").read_text() == "run\n"


@pytest.mark.parametrize(
    "path_name",
    [
        "README.md",
        pytest.param(
            " ",
            marks=pytest.mark.skipif(
                os.name == "nt",
                reason="Win32 does not support a path component made only of spaces",
            ),
        ),
    ],
)
@pytest.mark.parametrize("mutation_stage", ["entry", "checks", "verification"])
def test_release_gate_preserves_filename_bytes_in_workspace_checks(
    tmp_path: Path, path_name: str, mutation_stage: str
) -> None:
    root = tmp_path / "repository"
    primary = fixture_repository(root, succeeds=True)
    (root / path_name).write_text("tracked candidate\n")
    (root / "scripts/check-version.py").write_text(
        "from pathlib import Path\n"
        "Path('.git/version-checked').write_text('checked\\n')\n"
    )
    # Isolate the release gate's own checks; real receipt rejection and review
    # execution remain covered by the adjacent unchanged workflow tests.
    (root / "scripts/review-code.py").write_text(
        "import sys\nfrom pathlib import Path\n"
        "assert sys.argv[1] == 'verify'\n"
        "counter = Path('.git/workspace-verify-count')\n"
        "count = int(counter.read_text()) + 1 if counter.exists() else 1\n"
        "counter.write_text(str(count))\n"
        + (
            f"if count == 2:\n    Path({path_name!r}).write_text('changed\\n')\n"
            if mutation_stage == "verification"
            else ""
        )
    )
    executable(
        root / "scripts/check-full.sh",
        "#!/bin/sh\n"
        "printf 'checked\\n' > .git/fixture-gate\n"
        + (
            f"printf 'changed\\n' > {path_name!r}\n"
            if mutation_stage == "checks"
            else ""
        ),
    )
    git(root, "add", ".")
    git(root, "commit", "-m", "test: exercise gate workspace path bytes")
    task = git(root, "rev-parse", "HEAD")
    git(root, "switch", "master")
    git(root, "merge", "--no-ff", "--no-commit", "task")
    tree = git(root, "write-tree")
    if mutation_stage == "entry":
        (root / path_name).write_text("changed\n")

    result = subprocess.run(
        (sys.executable, "scripts/release-gate.py"),
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    expected = (
        "merge candidate has unstaged changes"
        if mutation_stage == "entry"
        else "merge candidate changed during the gate"
    )
    assert expected in result.stderr
    assert "Verified full merge candidate" not in result.stdout
    assert (root / path_name).read_text() == "changed\n"
    assert (
        subprocess.check_output(("git", "diff", "--name-only", "-z"), cwd=root)
        == os.fsencode(path_name) + b"\0"
    )
    assert git(root, "rev-parse", "HEAD") == primary
    assert git(root, "rev-parse", "task") == task
    assert git(root, "rev-parse", "MERGE_HEAD") == task
    assert git(root, "write-tree") == tree
    counter = root / ".git/workspace-verify-count"
    if mutation_stage == "entry":
        assert not counter.exists()
        assert not (root / ".git/version-checked").exists()
        assert not (root / ".git/fixture-gate").exists()
    else:
        assert counter.read_text() == ("1" if mutation_stage == "checks" else "2")
        assert (root / ".git/version-checked").read_text() == "checked\n"
        assert (root / ".git/fixture-gate").read_text() == "checked\n"


@pytest.mark.parametrize("flag", ["assume-unchanged", "skip-worktree"])
@pytest.mark.parametrize("mutation_stage", ["entry", "checks", "verification"])
def test_release_gate_rejects_index_flags_at_every_candidate_boundary(
    tmp_path: Path, flag: str, mutation_stage: str
) -> None:
    root = tmp_path / "repository"
    primary = fixture_repository(root, succeeds=True)
    (root / "README.md").write_text("tracked candidate\n")
    mutation = (
        "import subprocess\nfrom pathlib import Path\n"
        f"subprocess.run(['git', 'update-index', '--{flag}', '--', 'README.md'], check=True)\n"
        "Path('README.md').write_text('hidden unreviewed change\\n')\n"
    )
    (root / "scripts/mutate-index.py").write_text(mutation)
    (root / "scripts/check-version.py").write_text(
        "from pathlib import Path\n"
        "Path('.git/version-checked').write_text('checked\\n')\n"
    )
    (root / "scripts/review-code.py").write_text(
        "import runpy, sys\nfrom pathlib import Path\n"
        "assert sys.argv[1] == 'verify'\n"
        "counter = Path('.git/index-verify-count')\n"
        "count = int(counter.read_text()) + 1 if counter.exists() else 1\n"
        "counter.write_text(str(count))\n"
        + (
            "if count == 2:\n    runpy.run_path('scripts/mutate-index.py')\n"
            if mutation_stage == "verification"
            else ""
        )
    )
    executable(
        root / "scripts/check-full.sh",
        "#!/bin/sh\nprintf 'checked\\n' > .git/fixture-gate\n"
        + (
            "exec .venv/bin/python scripts/mutate-index.py\n"
            if mutation_stage == "checks"
            else ""
        ),
    )
    git(root, "add", ".")
    git(root, "commit", "-m", "test: hide source changes at gate boundaries")
    task = git(root, "rev-parse", "HEAD")
    git(root, "switch", "master")
    git(root, "merge", "--no-ff", "--no-commit", "task")
    tree = git(root, "write-tree")
    if mutation_stage == "entry":
        git(root, "update-index", "--" + flag, "--", "README.md")
        (root / "README.md").write_text("hidden unreviewed change\n")

    result = owned_fixture_command(root, (sys.executable, "scripts/release-gate.py"))

    assert result.returncode == 1, result.stdout + result.stderr
    assert "assume-unchanged or skip-worktree" in result.stderr
    assert "Verified full merge candidate" not in result.stdout
    assert (root / "README.md").read_text() == "hidden unreviewed change\n"
    assert (
        subprocess.check_output(("git", "diff", "--name-only", "-z"), cwd=root) == b""
    )
    inventory = subprocess.check_output(
        ("git", "ls-files", "-v", "-z", "--", "README.md"), cwd=root
    )
    assert inventory == (
        b"h README.md\0" if flag == "assume-unchanged" else b"S README.md\0"
    )
    assert git(root, "rev-parse", "HEAD") == primary
    assert git(root, "rev-parse", "task") == task
    assert git(root, "rev-parse", "MERGE_HEAD") == task
    assert git(root, "write-tree") == tree
    counter = root / ".git/index-verify-count"
    if mutation_stage == "entry":
        assert not counter.exists()
        assert not (root / ".git/version-checked").exists()
        assert not (root / ".git/fixture-gate").exists()
    else:
        assert counter.read_text() == ("1" if mutation_stage == "checks" else "2")
        assert (root / ".git/version-checked").read_text() == "checked\n"
        assert (root / ".git/fixture-gate").read_text() == "checked\n"


@pytest.mark.parametrize("stage", ["candidate", "noop", "cleanup-only"])
@pytest.mark.parametrize("ignored", [False, True])
def test_initial_primary_checkout_preserves_private_empty_directory(
    tmp_path: Path, stage: str, ignored: bool
) -> None:
    root = tmp_path / "repository"
    _ignored_file_fixture(root)
    protected = root / "reserved"
    if ignored:
        with (root / ".gitignore").open("a") as stream:
            stream.write("reserved\n")
        git(root, "add", ".gitignore")
        git(root, "commit", "-m", "test: ignore private directory")
    if stage in {"noop", "cleanup-only"}:
        task = git(root, "rev-parse", "HEAD")
        git(root, "update-ref", "refs/heads/master", task)
        git(root, "switch", "master")
    protected.write_text("primary tracked destination\n")
    git(root, "add", "-f", "reserved")
    git(root, "commit", "-m", "test: track primary destination")
    primary = git(root, "rev-parse", "HEAD")
    if stage in {"noop", "cleanup-only"}:
        git(root, "switch", "task")
    else:
        git(root, "update-ref", "refs/heads/master", primary)
        git(root, "rm", "reserved")
        git(root, "commit", "-m", "test: remove destination on task")
        task = git(root, "rev-parse", "HEAD")
    protected.mkdir()
    identity = protected.stat()
    assert git(root, "status", "--porcelain") == ""
    config = (root / ".git/config").read_bytes()
    command: tuple[str, ...]
    if stage == "candidate":
        command = (
            "bash",
            "src/h2h_dev_tools/git-flow-merge.sh",
            str(root),
            "scripts/review-merge-candidate.sh",
        )
    else:
        command = (
            "scripts/git-flow-merge.sh",
            *(["--cleanup-only"] if stage == "cleanup-only" else []),
        )
    result = owned_fixture_command(root, command)
    assert result.returncode not in {0, 124, 125}, result.stdout + result.stderr
    assert "empty-directory obstruction" in result.stderr
    assert protected.is_dir() and not list(protected.iterdir())
    after = protected.stat()
    assert (after.st_dev, after.st_ino, after.st_mode) == (
        identity.st_dev,
        identity.st_ino,
        identity.st_mode,
    )
    assert git(root, "branch", "--show-current") == "task"
    assert git(root, "rev-parse", "task") == task
    assert git(root, "rev-parse", "master") == primary
    assert git(root, "write-tree") == git(root, "rev-parse", "task^{tree}")
    assert git(root, "status", "--porcelain") == ""
    assert (root / ".git/config").read_bytes() == config
    for marker in (
        "MERGE_HEAD",
        "h2h-merge-workflow.lock",
        "fixture-gate",
        "fixture-review-events",
    ):
        assert not (root / ".git" / marker).exists()


@pytest.mark.parametrize("stage", ["normal", "noop", "cleanup-only"])
def test_initial_primary_checkout_uses_normal_owner_for_post_checkout_hook(
    tmp_path: Path, stage: str
) -> None:
    root = tmp_path / "repository"
    _ignored_file_fixture(root)
    events = tmp_path / "owner-commands.jsonl"
    marker = tmp_path / "post-checkout-completed"
    source = root / "src/h2h_dev_tools/process.py"
    source.write_text(
        source.read_text()
        + "\n_original_run_command = run_command\n"
        + "def run_command(phase, **kwargs):\n"
        + "    import json\n"
        + f"    with open({str(events)!r}, 'a') as events:\n"
        + "        events.write(json.dumps({'argv': phase.argv, "
        + "'bounded': kwargs.get('hard_deadline') is not None}) + '\\n')\n"
        + "    return _original_run_command(phase, **kwargs)\n"
    )
    write_python_executable(
        root / ".githooks/post-checkout",
        f"from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('hook completed')\n",
    )
    git(root, "add", ".")
    git(root, "commit", "-m", "test: observe normal checkout owner and hook")
    primary = git(root, "rev-parse", "HEAD")
    git(root, "update-ref", "refs/heads/master", primary)
    if stage == "normal":
        (root / "task.txt").write_text("new task content\n")
        git(root, "add", "task.txt")
        git(root, "commit", "-m", "test: make a nonempty task")
    task = git(root, "rev-parse", "task")
    result = owned_fixture_command(
        root,
        (
            "scripts/git-flow-merge.sh",
            *(["--cleanup-only"] if stage == "cleanup-only" else []),
        ),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.read_text() == "hook completed"
    commands = [json.loads(line) for line in events.read_text().splitlines()]
    switches = [item for item in commands if "switch-primary" in item["argv"]]
    assert len(switches) == 1 and switches[0]["bounded"] is False
    assert git(root, "branch", "--show-current") == "master"
    if stage == "normal":
        assert git(root, "show", "-s", "--format=%P", "HEAD").split() == [primary, task]
    else:
        assert git(root, "rev-parse", "HEAD") == primary
    assert not git(root, "branch", "--list", "task")
    assert git(root, "status", "--porcelain") == ""
    assert not (root / ".git/h2h-merge-workflow.lock").exists()
    assert not (root / ".git/MERGE_HEAD").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX inherited hook process group")
@pytest.mark.parametrize("cleanup_only", [False, True])
def test_initial_primary_checkout_cleans_hook_child_holding_output_streams(
    tmp_path: Path, cleanup_only: bool
) -> None:
    root = tmp_path / "repository"
    _ignored_file_fixture(root)
    pid_path = tmp_path / "hook-child.pid"
    stopped = tmp_path / "hook-child-stopped"
    child = (
        "import os,signal,sys,time\nfrom pathlib import Path\n"
        "def stop(*args):\n"
        f"    Path({str(stopped)!r}).write_text('terminated')\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        f"Path({str(pid_path)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(120)\n"
    )
    write_python_executable(
        root / ".githooks/post-checkout",
        f"import subprocess,sys,time\nfrom pathlib import Path\n"
        "branch=subprocess.check_output(['git','branch','--show-current'],text=True).strip()\n"
        "if branch == 'master':\n"
        f"    subprocess.Popen([sys.executable, '-c', {child!r}])\n"
        "    deadline=time.monotonic()+5\n"
        f"    while not Path({str(pid_path)!r}).exists():\n"
        "        assert time.monotonic()<deadline\n"
        "        time.sleep(0.01)\n",
    )
    git(root, "add", ".githooks/post-checkout", ".githooks/post-checkout.py")
    git(root, "commit", "-m", "test: leave inherited hook streams open")
    primary = git(root, "rev-parse", "HEAD")
    git(root, "update-ref", "refs/heads/master", primary)
    if not cleanup_only:
        (root / "task.txt").write_text("new task content\n")
        git(root, "add", "task.txt")
        git(root, "commit", "-m", "test: make a nonempty task")
    task = git(root, "rev-parse", "task")
    result = owned_fixture_command(
        root,
        ("scripts/git-flow-merge.sh", *(["--cleanup-only"] if cleanup_only else [])),
        timeout_seconds=45,
        termination_reserve_seconds=30,
    )
    assert result.returncode not in {0, 124, 125}, result.stdout + result.stderr
    assert "surviving process-tree members" in result.stdout + result.stderr
    assert stopped.read_text() == "terminated"
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_path.read_text()), 0)
    assert git(root, "rev-parse", "master") == primary
    assert git(root, "rev-parse", "task") == task
    # Cleanup-only has completed no merge and intentionally retains primary;
    # a normal failed merge workflow restores its captured task entry checkout.
    assert git(root, "branch", "--show-current") == (
        "master" if cleanup_only else "task"
    )
    assert git(root, "status", "--porcelain") == ""
    assert not (root / ".git/h2h-merge-workflow.lock").exists()
    assert not (root / ".git/MERGE_HEAD").exists()
    assert not (root / ".git/fixture-gate").exists()


@pytest.mark.parametrize(
    "change", ["none", "index", "unstaged", "primary", "merge-parent"]
)
def test_release_gate_rejects_drift_during_final_review_verification(
    tmp_path: Path, change: str
) -> None:
    root = tmp_path / "repository"
    fixture_repository(root, succeeds=True)
    verifier = (
        "import subprocess\nfrom pathlib import Path\n"
        "def git(*args):\n"
        "    return subprocess.check_output(['git', *args], text=True).strip()\n"
        "counter = Path(git('rev-parse', '--git-path', 'verify-count'))\n"
        "count = int(counter.read_text()) + 1 if counter.exists() else 1\n"
        "counter.write_text(str(count))\n"
        "if count == 2:\n"
    )
    mutations = {
        "none": "    pass\n",
        "index": "    Path('README.md').write_text('changed candidate\\n')\n    git('add', 'README.md')\n",
        "unstaged": "    Path('README.md').write_text('unstaged candidate\\n')\n",
        "primary": "    git('update-ref', 'HEAD', git('rev-parse', 'MERGE_HEAD'))\n",
        "merge-parent": "    Path(git('rev-parse', '--git-path', 'MERGE_HEAD')).write_text(git('rev-parse', 'HEAD') + '\\n')\n",
    }
    (root / "scripts/review-code.py").write_text(verifier + mutations[change])
    git(root, "add", ".")
    git(
        root,
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "-m",
        "test: move candidate during final verification",
    )
    git(root, "switch", "master")
    git(root, "merge", "--no-ff", "--no-commit", "task")
    result = subprocess.run(
        (sys.executable, "scripts/release-gate.py"),
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert (root / ".git/verify-count").read_text() == "2"
    if change == "none":
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Verified full merge candidate" in result.stdout
    else:
        assert result.returncode == 1, result.stdout + result.stderr
        assert "candidate changed during the gate" in result.stderr
        assert "Verified full merge candidate" not in result.stdout


@pytest.mark.parametrize("timeout", [1, 20])
def test_review_owner_deadline_covers_tiny_budget_and_stalled_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timeout: int
) -> None:
    root = tmp_path / "repository"
    fixture_repository(root, succeeds=True)
    binary = tmp_path / "stall-version"
    binary.mkdir()
    marker = tmp_path / "version-started"
    lock = tmp_path / "version-lock"
    write_python_executable(
        binary / "codex",
        f'import fcntl,time\nfrom pathlib import Path\nstream=Path({str(lock)!r}).open("w")\nfcntl.flock(stream.fileno(),fcntl.LOCK_EX)\nPath({str(marker)!r}).touch()\ntime.sleep(120)\n',
    )
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("H2H_CODE_REVIEW_TIMEOUT_SECONDS", str(timeout))
    started = time.monotonic()
    result = subprocess.run(
        ("scripts/git-flow-merge.sh",),
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout + 20,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert time.monotonic() - started < timeout + 10, result.stdout + result.stderr
    assert git(root, "branch", "--show-current") == "task"
    assert not (root / ".git/MERGE_HEAD").exists()
    assert not list((root / ".git/h2h-review").glob("receipts/*.json"))
    assert "owned_tree_empty=True" in result.stdout + result.stderr
    if timeout == 20:
        import fcntl

        assert marker.exists(), (
            "This oracle must actually enter the stalled CLI version probe."
        )
        with lock.open("a") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.skipif(os.name != "posix", reason="Real POSIX merge wrapper cancellation")
@pytest.mark.parametrize("phase", ["stage", "terminal"])
def test_cancel_merge_during_receipt_publication_restores_task(
    tmp_path: Path, phase: str
) -> None:
    root = tmp_path / "repository"
    base = fixture_repository(root, succeeds=True)
    (root / ".gitignore").write_text(".venv/\n__pycache__/\n")
    module = root / "src/h2h_dev_tools/code_review.py"
    source = module.read_text()
    committed = phase == "terminal"
    indent = " " * (20 if committed else 12)
    anchor = indent + (
        "os.replace(staging, path)\n" if committed else "os.replace(pending, path)\n"
    )
    assert source.count(anchor) == 1
    hold = (
        "state = Path(os.environ['PUBLICATION_HOLD'])\n"
        "state.write_text(str(os.getpid()))\n"
    )
    if committed:
        # The terminal rename defers signals. Let the actual pending signal
        # release this pause so context exit can replay it before fixture rescue.
        hold += (
            "while controller.pending_signal is None and not state.with_suffix('.release').exists():\n"
            "    time.sleep(0.01)\n"
            "if controller.pending_signal is not None:\n"
            "    state.with_suffix('.signal').write_text(str(controller.pending_signal))\n"
        )
    else:
        hold += (
            "while not state.with_suffix('.release').exists():\n    time.sleep(0.01)\n"
        )
    # Managed review also commits its private pending result through _publication.
    # Pause only the outer destination in this fixture's canonical receipt directory.
    hold = "if path.parent == Path(os.environ['PUBLICATION_RECEIPTS']):\n" + "".join(
        "    " + line for line in hold.splitlines(keepends=True)
    )
    module.write_text(
        source.replace(
            anchor,
            anchor + "".join(indent + line for line in hold.splitlines(keepends=True)),
        )
    )
    git(root, "add", ".")
    git(root, "commit", "-m", f"test: pause receipt {phase} rename")
    task = git(root, "rev-parse", "HEAD")
    marker = tmp_path / "published"
    output_path = tmp_path / "output"
    deadline = time.monotonic() + 15
    with output_path.open("w+b") as output:
        with process.controlled_termination_signals() as controller:
            owner: process.OwnedProcess | None = None
            clean = False
            try:
                with controller.defer():
                    owner = process.start_owned_process(
                        process.ProcessCommand(
                            f"cancel receipt {phase} publication",
                            ("scripts/git-flow-merge.sh",),
                            root,
                            environment=os.environ
                            | {
                                "PUBLICATION_HOLD": str(marker),
                                "PUBLICATION_RECEIPTS": str(
                                    root / ".git/h2h-review/receipts"
                                ),
                            },
                            stdout=output,
                            stderr=subprocess.STDOUT,
                        ),
                        deadline=deadline,
                        signal_controller=controller,
                    )
                wrapper = owner.process
                while not marker.exists():
                    assert wrapper.poll() is None, output_path.read_text()
                    assert time.monotonic() < deadline, (
                        "Owner did not reach receipt boundary"
                    )
                    time.sleep(0.01)
                receipts_directory = root / ".git/h2h-review/receipts"
                receipts = tuple(receipts_directory.glob("*.json"))
                stages = tuple(receipts_directory.glob("*.pending"))
                assert len(receipts) == int(committed)
                assert len(stages) == int(not committed)
                prepared = receipts[0] if committed else stages[0]
                prepared_bytes = prepared.read_bytes()
                document = json.loads(prepared_bytes)["target"]
                target = code_review.Target(
                    document["tree"],
                    tuple(document["parents"]),
                    document["policy_digest"],
                )
                assert target.parents == (base, task)
                assert target.tree == git(root, "write-tree")
                receipt = receipts_directory / f"{target.key}.json"
                assert prepared == receipt if committed else prepared != receipt
                wrapper.send_signal(signal.SIGTERM)
                assert (
                    wrapper.wait(timeout=max(0.0, deadline - time.monotonic())) == 143
                ), output_path.read_text()
                _assert_interrupted_workflow_cleanup(
                    owner, output_path.read_text(), deadline=deadline
                )
                clean = True
                with pytest.raises(ProcessLookupError):
                    os.killpg(wrapper.pid, 0)
                verification = subprocess.run(
                    (sys.executable, "scripts/review-code.py", "verify", "--index"),
                    cwd=root,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
                (tmp_path / "wrapper-signal-proof.json").write_text(
                    json.dumps(
                        {
                            "phase": phase,
                            "target": target.document(),
                            "wrapper_returncode": wrapper.returncode,
                            "receipt_exists": receipt.exists(),
                            "receipt_unchanged": receipt.exists()
                            and receipt.read_bytes() == prepared_bytes,
                            "restored_index_verify_returncode": verification.returncode,
                            "merge_head_exists": (root / ".git/MERGE_HEAD").exists(),
                            "branch": git(root, "branch", "--show-current"),
                            "owned_tree_empty_before_rescue": clean,
                            "rescue_release_exists": marker.with_suffix(
                                ".release"
                            ).exists(),
                        }
                    )
                )
                assert receipt.exists() == committed, output_path.read_text()
                assert not tuple(receipts_directory.glob("*.pending"))
                assert not marker.with_suffix(".release").exists()
                reviewer = code_review.Reviewer(root)
                with reviewer._review_lock(target, exclusive=False):
                    if committed:
                        assert receipt.read_bytes() == prepared_bytes
                        assert marker.with_suffix(".signal").read_text() == str(
                            signal.SIGTERM
                        )
                        assert reviewer._verify(target) == receipt
                    else:
                        with pytest.raises(code_review.ReviewError):
                            reviewer._verify(target)
                # Recovery restored the task's index, not the captured merge target.
                assert verification.returncode != 0
                assert not (root / ".git/MERGE_HEAD").exists(), output_path.read_text()
                assert not (root / ".git/h2h-merge-workflow.lock").exists()
                assert not (root / ".git/fixture-gate").exists()
                assert git(root, "rev-parse", "master") == base
                assert git(root, "rev-parse", "task") == task
                assert git(root, "branch", "--show-current") == "task"
                assert git(root, "status", "--porcelain") == ""
            finally:
                with controller.defer():
                    try:
                        marker.with_suffix(".release").touch()
                    finally:
                        if owner is not None and not clean:
                            empty = process.terminate_owned_tree(
                                owner, deadline=deadline
                            )
                            closed = process.close_owner(owner, deadline=deadline)
                            assert empty and closed, (
                                "Publication fixture cleanup is unproven"
                            )


@pytest.mark.skipif(os.name != "posix", reason="Real publication owner cleanup")
@pytest.mark.parametrize("phase", ["stage", "terminal"])
def test_publication_fixture_closes_owner_when_release_marker_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    start = process.start_owned_process
    close = process.close_owner
    touch = Path.touch
    marker_error = OSError("fixture release marker write failed")
    owners: list[tuple[process.OwnedProcess, float]] = []
    closed: list[tuple[process.OwnedProcess, float, bool]] = []

    def start_and_interrupt(
        command: process.ProcessCommand,
        *,
        deadline: float,
        signal_controller: process.SignalController | None,
    ) -> process.OwnedProcess:
        assert signal_controller is not None and signal_controller.defer_depth > 0
        owner = start(command, deadline=deadline, signal_controller=signal_controller)
        owners.append((owner, deadline))
        signal_controller.receive(signal.SIGTERM)
        return owner

    def refuse_marker(path: Path, mode: int = 0o666, exist_ok: bool = True) -> None:
        if path == tmp_path / "published.release":
            raise marker_error
        touch(path, mode=mode, exist_ok=exist_ok)

    def record_close(
        owner: process.OwnedProcess,
        *,
        deadline: float,
        diagnostic: Callable[[str], None] | None = None,
    ) -> bool:
        result = close(owner, deadline=deadline, diagnostic=diagnostic)
        closed.append((owner, deadline, result))
        return result

    monkeypatch.setattr(process, "start_owned_process", start_and_interrupt)
    monkeypatch.setattr(process, "close_owner", record_close)
    monkeypatch.setattr(Path, "touch", refuse_marker)
    try:
        with pytest.raises(OSError) as caught:
            test_cancel_merge_during_receipt_publication_restores_task(tmp_path, phase)
        assert caught.value is marker_error
        assert len(owners) == 1
        owner, deadline = owners[0]
        assert closed == [(owner, deadline, True)]
        assert owner.posix_scope is not None
        assert not owner.posix_scope.capability.directory.exists()
        with pytest.raises(ProcessLookupError):
            os.killpg(owner.process.pid, 0)
    finally:
        failure = sys.exception()
        for owner, deadline in owners:
            if (owner, deadline, True) not in closed:
                try:
                    with process.controlled_termination_signals() as controller:
                        with controller.defer():
                            empty = process.terminate_owned_tree(
                                owner, deadline=deadline
                            )
                            released = close(owner, deadline=deadline)
                            assert empty and released
                except BaseException as rescue_error:
                    if failure is None:
                        raise
                    failure.add_note(
                        f"Publication control rescue also failed: {rescue_error}"
                    )


@pytest.mark.skipif(os.name != "posix", reason="Real POSIX Git fsmonitor ownership")
@pytest.mark.parametrize("mode", ["normal", "fsmonitor-stray", "unknown-cleanup"])
def test_cleanliness_stage_owns_git_callbacks_before_task_cleanup(
    tmp_path: Path, mode: str
) -> None:
    root = tmp_path / "repository"
    fixture_repository(root, succeeds=True, primary_hooks=True)
    state = tmp_path / "state"
    state.mkdir()
    scopes = tmp_path / "scopes"
    scopes.mkdir()
    if mode == "unknown-cleanup":
        source = root / "src/h2h_dev_tools/process.py"
        source.write_text(
            source.read_text() + "\n_original_run_command = run_command\n"
            "def run_command(phase, **kwargs):\n"
            "    target = ('--workflow-state', 'check-clean')\n"
            "    if any(phase.argv[index:index + len(target)] == target\n"
            "           for index in range(len(phase.argv) - len(target) + 1)):\n"
            f"        Path({str(state / 'unknown-cleanup')!r}).touch()\n"
            "        raise ProcessOwnershipError('fixture cleanup unproven')\n"
            "    return _original_run_command(phase, **kwargs)\n"
        )
        git(root, "add", ".")
        git(root, "commit", "-m", "test: refuse unproven cleanliness cleanup")
    task = git(root, "rev-parse", "task")
    git(root, "update-ref", "refs/heads/master", task)
    child = (
        "import json, os, signal, sys, time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "Path(sys.argv[1]).write_text(json.dumps({'pid': os.getpid(), "
        "'group': os.getpgrp()}))\n"
        "while True:\n    time.sleep(0.1)\n"
    )
    hook = tmp_path / "fsmonitor"
    write_python_executable(
        hook,
        "import json, os, subprocess, sys, time\nfrom pathlib import Path\n"
        f"state = Path({str(state)!r})\n"
        "with (state / 'queries.jsonl').open('a') as events:\n"
        "    events.write(json.dumps({'group': os.getpgrp()}) + '\\n')\n"
        + (
            "try:\n    (state / 'spawned').touch(exist_ok=False)\n"
            "except FileExistsError:\n    pass\n"
            "else:\n"
            f"    subprocess.Popen([sys.executable, '-I', '-S', '-B', '-c', {child!r}, "
            "str(state / 'child.json')], stdin=subprocess.DEVNULL, "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            "    while not (state / 'child.json').exists():\n"
            "        time.sleep(0.005)\n"
            if mode == "fsmonitor-stray"
            else ""
        )
        + "os.write(1, b'cleanliness-token\\0/\\0')\n",
    )
    git(root, "config", "core.fsmonitor", str(hook))
    git(root, "config", "core.fsmonitorHookVersion", "2")
    deadline = time.monotonic() + 20
    output_path = state / "workflow-output"
    with output_path.open("wb") as output:
        with process.controlled_termination_signals() as controller:
            owner = None
            clean = False
            try:
                with controller.defer():
                    owner = process.start_owned_process(
                        process.ProcessCommand(
                            "owned cleanliness before task cleanup",
                            ("scripts/git-flow-merge.sh", "--cleanup-only"),
                            root,
                            environment=os.environ | {"TMPDIR": str(scopes)},
                            stdout=output,
                            stderr=subprocess.STDOUT,
                        ),
                        deadline=deadline,
                        signal_controller=controller,
                    )
                status = owner.process.wait(timeout=12)
                recorded = output_path.read_text()
                assert process.wait_for_owned_tree_exit(
                    owner, deadline=min(deadline - 6, time.monotonic() + 0.25)
                ), recorded
                _assert_interrupted_workflow_cleanup(owner, recorded, deadline=deadline)
                clean = True
                if mode == "unknown-cleanup":
                    assert (state / "unknown-cleanup").exists()
                    assert not (state / "queries.jsonl").exists()
                    retained = list(scopes.glob("h2h-merge-owner.*"))
                    assert len(retained) == 1
                    assert (retained[0] / "cleanup-1").read_text() == (
                        "owned_tree_empty\n"
                    )
                    assert not (retained[0] / "cleanup-2").exists()
                    assert (root / ".git/h2h-merge-workflow.lock/owner.json").is_file()
                else:
                    queries = [
                        json.loads(line)
                        for line in (state / "queries.jsonl").read_text().splitlines()
                    ]
                    assert queries
                    assert all(row["group"] != owner.process.pid for row in queries)
                    assert not (root / ".git/h2h-merge-workflow.lock").exists()
                    assert not list(scopes.glob("h2h-merge-owner.*"))
                if mode == "fsmonitor-stray":
                    identity = json.loads((state / "child.json").read_text())
                    with pytest.raises(ProcessLookupError):
                        os.kill(identity["pid"], 0)
                    with pytest.raises(ProcessLookupError):
                        os.killpg(identity["group"], 0)
                    assert "surviving descendants required cleanup" in recorded
                    assert "owned_tree_empty=True" in recorded
                assert git(root, "rev-parse", "master") == task
                assert not (root / ".git/MERGE_HEAD").exists()
                assert not (root / ".git/fixture-gate").exists()
                assert not (root / ".git/fixture-review-events").exists()
                if mode == "normal":
                    assert status == 0, recorded
                    assert not git(root, "branch", "--list", "task")
                    assert git(root, "branch", "--show-current") == "master"
                else:
                    assert status == 1, recorded
                    assert git(root, "rev-parse", "task") == task
                    assert git(root, "branch", "--show-current") == "task"
            finally:
                if owner is not None and not clean:
                    with controller.defer():
                        empty = process.terminate_owned_tree(owner, deadline=deadline)
                        closed = process.close_owner(owner, deadline=deadline)
                        assert empty and closed, (
                            "Cleanliness fixture cleanup is unproven"
                        )


@pytest.mark.skipif(os.name != "posix", reason="Real POSIX Git fsmonitor ownership")
@pytest.mark.parametrize(
    "mode",
    [
        "normal",
        "fsmonitor-stray",
        "unknown-cleanup",
        "export-failure",
        "missing-export",
        "empty-export",
    ],
)
def test_candidate_export_uses_owned_capture_before_review(
    tmp_path: Path, mode: str
) -> None:
    root = tmp_path / "repository"
    base = fixture_repository(root, succeeds=True, primary_hooks=True)
    state = tmp_path / "state"
    state.mkdir()
    scopes = tmp_path / "scopes"
    scopes.mkdir()
    shell = root / "src/h2h_dev_tools/git-flow-merge.sh"
    marker = 'archive_paths=("$source_relative")\n'
    assert shell.read_text().count(marker) == 1
    shell.write_text(
        shell.read_text().replace(
            marker,
            "printf '%s\\n' \"$candidate_tree\" > "
            + shlex.quote(str(state / "exported-tree"))
            + "\n"
            + marker,
        )
    )
    if mode in {"missing-export", "empty-export"}:
        read_tree = 'candidate_tree="$(cat "${workflow_state%.json}.tree")"'
        assert shell.read_text().count(read_tree) == 1
        alteration = (
            'rm -- "${workflow_state%.json}.tree"'
            if mode == "missing-export"
            else ': > "${workflow_state%.json}.tree"'
        )
        shell.write_text(
            shell.read_text().replace(read_tree, alteration + "\n" + read_tree)
        )
    if mode == "unknown-cleanup":
        source = root / "src/h2h_dev_tools/process.py"
        source.write_text(
            source.read_text() + "\n_original_run_command = run_command\n"
            "def run_command(phase, **kwargs):\n"
            "    target = ('--workflow-state', 'capture')\n"
            "    if any(phase.argv[index:index + len(target)] == target\n"
            "           for index in range(len(phase.argv) - len(target) + 1)):\n"
            f"        Path({str(state / 'unknown-cleanup')!r}).touch()\n"
            "        raise ProcessOwnershipError('fixture cleanup unproven')\n"
            "    return _original_run_command(phase, **kwargs)\n"
        )
    git(root, "add", ".")
    git(root, "commit", "-m", "test: observe captured candidate export")
    task = git(root, "rev-parse", "task")
    child = (
        "import json, os, signal, sys, time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "Path(sys.argv[1]).write_text(json.dumps({'pid': os.getpid(), "
        "'group': os.getpgrp()}))\n"
        "while True:\n    time.sleep(0.1)\n"
    )
    hook = tmp_path / "fsmonitor"
    write_python_executable(
        hook,
        "import json, os, subprocess, sys, time\nfrom pathlib import Path\n"
        f"state = Path({str(state)!r})\n"
        f"scopes = Path({str(scopes)!r})\n"
        f"mode = {mode!r}\n"
        "suffix = f'-P{os.getppid():08x}'\n"
        "trace = [json.loads(line) for line in "
        "(state / 'git-trace').read_text().splitlines()]\n"
        "command = next(row['argv'] for row in reversed(trace) "
        "if row['event'] == 'start' and row['sid'].endswith(suffix))\n"
        "workflow = next(scopes.glob('h2h-merge-owner.*/workflow.json'))\n"
        "phase = json.loads(workflow.read_text())['phase']\n"
        "write_tree = command[-1] == 'write-tree'\n"
        "with (state / 'queries.jsonl').open('a') as events:\n"
        "    events.write(json.dumps({'group': os.getpgrp(), 'phase': phase, "
        "'write_tree': write_tree}) + '\\n')\n"
        "while not (state / 'outer-group').exists():\n"
        "    time.sleep(0.005)\n"
        "outer_group = int((state / 'outer-group').read_text())\n"
        "spawn = write_tree and ((mode == 'normal' and os.getpgrp() == "
        "outer_group) or (mode == 'fsmonitor-stray' and phase == 'retained'))\n"
        "if spawn and not (state / 'spawned').exists():\n"
        "    (state / 'spawned').touch()\n"
        f"    subprocess.Popen([sys.executable, '-I', '-S', '-B', '-c', {child!r}, "
        "str(state / 'child.json')], stdin=subprocess.DEVNULL, "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "    while not (state / 'child.json').exists():\n"
        "        time.sleep(0.005)\n"
        "if write_tree and phase == 'retained' and mode == 'export-failure':\n"
        "    workflow.with_suffix('.tree').mkdir()\n"
        "os.write(1, b'candidate-token\\0/\\0')\n",
    )
    git(root, "config", "core.fsmonitor", str(hook))
    git(root, "config", "core.fsmonitorHookVersion", "2")
    deadline = time.monotonic() + 90
    output_path = state / "workflow-output"
    with output_path.open("wb") as output:
        with process.controlled_termination_signals() as controller:
            owner = None
            clean = False
            try:
                with controller.defer():
                    owner = process.start_owned_process(
                        process.ProcessCommand(
                            "owned candidate capture before review",
                            ("scripts/git-flow-merge.sh",),
                            root,
                            environment=os.environ
                            | {
                                "TMPDIR": str(scopes),
                                "GIT_TRACE2_EVENT": str(state / "git-trace"),
                            },
                            stdout=output,
                            stderr=subprocess.STDOUT,
                        ),
                        deadline=deadline,
                        signal_controller=controller,
                    )
                (state / "outer-group").write_text(str(owner.process.pid))
                status = owner.process.wait(timeout=60)
                recorded = output_path.read_text()
                assert process.wait_for_owned_tree_exit(
                    owner, deadline=min(deadline - 30, time.monotonic() + 0.25)
                ), recorded
                _assert_interrupted_workflow_cleanup(owner, recorded, deadline=deadline)
                clean = True
                queries = [
                    json.loads(line)
                    for line in (state / "queries.jsonl").read_text().splitlines()
                ]
                write_tree_queries = [row for row in queries if row["write_tree"]]
                assert write_tree_queries
                assert all(
                    row["group"] != owner.process.pid for row in write_tree_queries
                )
                if mode == "normal":
                    assert status == 0, recorded
                    assert (state / "exported-tree").read_text().strip() == git(
                        root, "rev-parse", "HEAD^{tree}"
                    )
                    assert git(root, "rev-parse", "HEAD^1") == base
                    assert git(root, "rev-parse", "HEAD^2") == task
                    assert not git(root, "branch", "--list", "task")
                    assert git(root, "branch", "--show-current") == "master"
                    assert not (state / "child.json").exists()
                else:
                    assert status == 1, recorded
                    assert git(root, "rev-parse", "master") == base
                    assert git(root, "rev-parse", "task") == task
                    assert not (state / "exported-tree").exists()
                    assert not (root / ".git/fixture-gate").exists()
                    assert not (root / ".git/fixture-review-events").exists()
                if mode == "unknown-cleanup":
                    assert (state / "unknown-cleanup").exists()
                    assert (root / ".git/MERGE_HEAD").exists()
                    assert git(root, "branch", "--show-current") == "master"
                    retained = list(scopes.glob("h2h-merge-owner.*"))
                    assert len(retained) == 1
                    assert not (retained[0] / "workflow.tree").exists()
                    assert (root / ".git/h2h-merge-workflow.lock/owner.json").is_file()
                else:
                    assert not (root / ".git/MERGE_HEAD").exists()
                    assert not (root / ".git/h2h-merge-workflow.lock").exists()
                    assert not list(scopes.glob("h2h-merge-owner.*"))
                    if mode != "normal":
                        assert git(root, "branch", "--show-current") == "task"
                if mode == "fsmonitor-stray":
                    identity = json.loads((state / "child.json").read_text())
                    with pytest.raises(ProcessLookupError):
                        os.kill(identity["pid"], 0)
                    with pytest.raises(ProcessLookupError):
                        os.killpg(identity["group"], 0)
                    assert "surviving descendants required cleanup" in recorded
                if mode == "export-failure":
                    assert "Is a directory" in recorded
                if mode == "missing-export":
                    assert "could not read captured merge candidate tree" in recorded
                if mode == "empty-export":
                    assert "captured merge candidate tree is empty" in recorded
            finally:
                if owner is not None and not clean:
                    with controller.defer():
                        empty = process.terminate_owned_tree(owner, deadline=deadline)
                        closed = process.close_owner(owner, deadline=deadline)
                        assert empty and closed, "Candidate fixture cleanup is unproven"
