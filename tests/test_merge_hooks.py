"""Real Git hooks configuration admission, including effective worktree policy."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_merge_workflow import (
    executable,
    fixture_repository,
    git,
    owned_fixture_command,
)
from test_merge_workflow import offline_codex as offline_codex


@pytest.mark.parametrize("origin", ["local", "worktree", "environment", "onbranch"])
@pytest.mark.parametrize("value", [".githooks\n", ".githooks\n\n", "", "missing-hooks"])
def test_merge_rejects_effective_hooks_configuration(
    tmp_path: Path, origin: str, value: str
) -> None:
    root = tmp_path / "repository"
    base = fixture_repository(root, succeeds=False)
    task = git(root, "rev-parse", "task")
    environment = dict(os.environ)
    if origin == "local":
        git(root, "config", "--local", "core.hooksPath", value)
    elif origin == "worktree":
        git(root, "config", "extensions.worktreeConfig", "true")
        git(root, "config", "--worktree", "core.hooksPath", value)
    elif origin == "environment":
        environment.update(
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0="core.hooksPath",
            GIT_CONFIG_VALUE_0=value,
        )
    else:
        config = tmp_path / "primary-config"
        git(root, "config", "--file", str(config), "core.hooksPath", value)
        git(root, "config", "--local", "includeIf.onbranch:master.path", str(config))
    result = subprocess.run(
        ("scripts/git-flow-merge.sh",),
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    observation = {
        "origin": origin,
        "configured_value": value,
        "exit": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "primary_before": base,
        "primary_after": git(root, "rev-parse", "master"),
        "task_retained": bool(git(root, "branch", "--list", "task")),
        "gate_executed": (root / ".git/fixture-gate").exists(),
    }
    (tmp_path / "hooks-observation.json").write_text(json.dumps(observation, indent=2))
    assert result.returncode != 0, observation
    assert "repository hooks are not installed" in result.stderr
    assert observation["primary_after"] == base
    assert observation["task_retained"]
    assert git(root, "rev-parse", "task") == task
    assert not observation["gate_executed"]
    assert not (root / ".git/MERGE_HEAD").exists()


@pytest.mark.parametrize("succeeds", [False, True])
def test_exact_effective_hooks_setting_runs_the_real_gate(
    tmp_path: Path, succeeds: bool
) -> None:
    root = tmp_path / "repository"
    base = fixture_repository(root, succeeds=succeeds)
    task = git(root, "rev-parse", "task")
    environment = os.environ | {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": ".githooks",
    }
    result = subprocess.run(
        ("scripts/git-flow-merge.sh",),
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert (result.returncode == 0) is succeeds, result.stdout + result.stderr
    assert (root / ".git/fixture-gate").read_text() == "checked\n"
    if succeeds:
        assert git(root, "show", "-s", "--format=%P", "master").split() == [base, task]
        assert not git(root, "branch", "--list", "task")
    else:
        assert git(root, "rev-parse", "master") == base
        assert git(root, "rev-parse", "task") == task
    assert not (root / ".git/MERGE_HEAD").exists()


@pytest.mark.parametrize("valid_primary", [False, True])
def test_linked_primary_uses_its_own_effective_hooks_setting(
    tmp_path: Path, valid_primary: bool
) -> None:
    primary = tmp_path / "primary"
    base = fixture_repository(primary, succeeds=False)
    task_oid = git(primary, "rev-parse", "task")
    git(primary, "switch", "master")
    task = tmp_path / "linked-task"
    git(primary, "worktree", "add", str(task), "task")
    local_bin = task / ".venv/bin"
    local_bin.mkdir(parents=True)
    (local_bin / "python").symlink_to(sys.executable)
    git(primary, "config", "extensions.worktreeConfig", "true")
    git(
        primary,
        "config",
        "--worktree",
        "core.hooksPath",
        ".githooks" if valid_primary else ".githooks\n",
    )
    git(task, "config", "--worktree", "core.hooksPath", ".githooks")
    result = subprocess.run(
        ("scripts/git-flow-merge.sh",),
        cwd=task,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert git(primary, "rev-parse", "master") == base
    assert git(primary, "rev-parse", "task") == task_oid
    assert task.is_dir()
    gate = primary / ".git/fixture-gate"
    if valid_primary:
        assert gate.read_text() == "checked\n"
    else:
        assert "repository hooks are not installed" in result.stderr
        assert not gate.exists()
    assert not (primary / ".git/MERGE_HEAD").exists()


@pytest.mark.parametrize("hook", ["pre-commit", "pre-merge-commit"])
def test_candidate_checks_hooks_after_three_way_deletion(
    tmp_path: Path, hook: str
) -> None:
    root = tmp_path / "repository"
    fixture_repository(root, succeeds=False, primary_hooks=True)
    # Establish the real hook on the common ancestor, then change independent
    # files on task while primary deletes that otherwise unchanged hook.
    git(root, "switch", "master")
    git(root, "-c", "core.hooksPath=/dev/null", "merge", "--ff-only", "task")
    git(root, "branch", "-d", "task")
    git(root, "switch", "-c", "task")
    (root / "feature.txt").write_text("task feature\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "feat: extend feature")
    task = git(root, "rev-parse", "task")
    git(root, "switch", "master")
    git(root, "rm", ".githooks/" + hook)
    git(
        root,
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "-m",
        "test: remove hook on primary",
    )
    base = git(root, "rev-parse", "master")
    git(root, "switch", "task")
    result = subprocess.run(
        ("scripts/git-flow-merge.sh",),
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert f".githooks/{hook} is missing or not executable" in result.stderr
    assert git(root, "rev-parse", "master") == base
    assert git(root, "rev-parse", "task") == task
    assert not (root / ".git/fixture-gate").exists()
    assert not (root / ".git/MERGE_HEAD").exists()


def test_git_command_pins_validated_hooks_during_configuration_change(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    base = fixture_repository(root, succeeds=False, primary_hooks=False)
    gate = (
        "#!/bin/sh\nprintf 'checked\\n' >> \"$(git rev-parse --git-path fixture-gate)\"\n"
        'git config --get core.hooksPath > "$(git rev-parse --git-path effective-hook-setting)"\nexit 1\n'
    )
    executable(
        root / ("scripts/check-full.sh"),
        gate,
    )
    marker = tmp_path / "configuration-mutated"
    commit_command = (
        "git",
        "-C",
        str(root),
        "-c",
        "core.hooksPath=.githooks",
        "commit",
        "--no-edit",
    )
    source = root / "src/h2h_dev_tools/process.py"
    # Inject at the actual command boundary, preserving native Git for every
    # inventory query. A PATH-wide Python Git shim adds startup cost to dozens
    # of recovery reads and can exhaust their unchanged four-second work budget.
    source.write_text(
        source.read_text()
        + "\n_original_run_command = run_command\n"
        + "def run_command(phase, **kwargs):\n"
        + "    import subprocess\n    from pathlib import Path\n"
        + f"    marker = Path({str(marker)!r})\n"
        + f"    if phase.argv == {commit_command!r} and not marker.exists():\n"
        + f"        subprocess.run(['git', '-C', {str(root)!r}, 'config', '--local', 'core.hooksPath', 'missing-hooks'], check=True)\n"
        + "        marker.write_text('changed immediately before real Git exec\\n')\n"
        + "    return _original_run_command(phase, **kwargs)\n"
    )
    git(root, "add", ".")
    git(root, "commit", "-m", "test: record real hook configuration")
    task = git(root, "rev-parse", "task")
    command = ("scripts/git-flow-merge.sh",)
    result = owned_fixture_command(root, command)
    output = result.stdout + result.stderr
    (tmp_path / "workflow-output.log").write_text(output)
    assert marker.read_text() == "changed immediately before real Git exec\n"
    assert result.returncode not in {0, 124, 125}, result.stdout + result.stderr
    assert (root / ".git/fixture-gate").read_text() == "checked\n"
    assert (root / ".git/effective-hook-setting").read_bytes() == b".githooks\n"
    assert git(root, "config", "--local", "--get", "core.hooksPath") == "missing-hooks"
    assert git(root, "rev-parse", "master") == base
    assert git(root, "rev-parse", "task") == task
    assert not (root / ".git/MERGE_HEAD").exists()

    assert not (root / ".git/h2h-merge-workflow.lock").exists(), output
