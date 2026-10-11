"""Keep Git hook authority out of disposable test repositories."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from h2h_dev_tools import git_environment

ROOT = Path(__file__).resolve().parents[1]


def _git(repository: Path, environment: dict[str, str], *arguments: str) -> str:
    return subprocess.run(
        ("git", "-C", str(repository), *arguments),
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _environment() -> tuple[dict[str, str], list[str]]:
    names = subprocess.run(
        ("git", "rev-parse", "--local-env-vars"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    environment = {key: value for key, value in os.environ.items() if key not in names}
    environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
    return environment, names


def _executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def test_only_git_declared_local_names_are_removed_from_an_environment_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    original = {
        "GIT_INDEX_FILE": "candidate-index",
        "GIT_FUTURE_LOCAL": "future authority",
        "GIT_CONFIG_GLOBAL": "global preferences",
        "H2H_TEST_SENTINEL": "preserved",
    }
    observed_environment: dict[str, str] = {}

    def query_git(
        *arguments: object, env: dict[str, str], **options: object
    ) -> subprocess.CompletedProcess[bytes]:
        observed_environment.update(env)
        return subprocess.CompletedProcess((), 0, b"GIT_INDEX_FILE\nGIT_FUTURE_LOCAL\n")

    query = Mock(side_effect=query_git)
    monkeypatch.setattr(subprocess, "run", query)

    cleaned = git_environment.without_local_git_environment(
        original, repository=tmp_path
    )

    assert cleaned == {
        "GIT_CONFIG_GLOBAL": "global preferences",
        "H2H_TEST_SENTINEL": "preserved",
    }
    assert original["GIT_INDEX_FILE"] == "candidate-index"
    assert original["GIT_FUTURE_LOCAL"] == "future authority"
    assert observed_environment == original
    query.assert_called_once()
    assert query.call_args.args == (
        ("git", "-C", str(tmp_path), "rev-parse", "--local-env-vars"),
    )
    assert query.call_args.kwargs["timeout"] == (
        git_environment.GIT_ENVIRONMENT_QUERY_TIMEOUT_SECONDS
    )


@pytest.mark.parametrize(
    "output",
    [
        b"",
        b"GIT_DIR",
        b"GIT_DIR\nGIT_DIR\n",
        b"GIT_DIR\n\n",
        b"not a name\n",
        b"\xff\n",
    ],
)
def test_malformed_git_inventory_never_launches_the_test_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, output: bytes
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        Mock(return_value=subprocess.CompletedProcess((), 0, output)),
    )
    execute = Mock()
    monkeypatch.setattr(os, "execvpe", execute)
    monkeypatch.setattr(
        sys, "argv", ["git-environment", "--repository", str(tmp_path), "--", "pytest"]
    )

    with pytest.raises(SystemExit) as error:
        git_environment.main()

    assert error.value.code == 1
    execute.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    [
        OSError("missing Git"),
        subprocess.CalledProcessError(1, "git"),
        subprocess.TimeoutExpired("git", 15),
    ],
)
def test_git_query_failure_does_not_return_an_unsanitized_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: Exception
) -> None:
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=failure))
    original = {"GIT_INDEX_FILE": "candidate-index"}

    with pytest.raises(git_environment.GitEnvironmentError):
        git_environment.without_local_git_environment(original, repository=tmp_path)

    assert original == {"GIT_INDEX_FILE": "candidate-index"}


@pytest.mark.skipif(os.name != "posix", reason="POSIX exec keeps the same owned PID")
def test_test_child_replaces_launcher_and_preserves_its_exit_status(
    tmp_path: Path,
) -> None:
    environment, _ = _environment()
    marker = tmp_path / "child-pid"
    child = subprocess.Popen(
        (
            sys.executable,
            str(ROOT / "src/h2h_dev_tools/git_environment.py"),
            "--repository",
            str(ROOT),
            "--",
            sys.executable,
            "-c",
            "import os\nfrom pathlib import Path\n"
            f"Path({str(marker)!r}).write_text(str(os.getpid()))\n"
            "raise SystemExit(7)\n",
        ),
        env=environment,
    )
    try:
        assert child.wait(timeout=10) == 7
        assert marker.read_text() == str(child.pid)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


@pytest.mark.skipif(os.name != "posix", reason="Exercises the Bash full-gate entry")
def test_full_gate_isolates_pytest_from_a_real_linked_worktree_hook(
    tmp_path: Path,
) -> None:
    environment, names = _environment()
    environment["H2H_TEST_ENVIRONMENT_SENTINEL"] = "preserved"
    main = tmp_path / "main"
    main.mkdir()
    _git(main, environment, "init", "-b", "master")
    _git(main, environment, "config", "user.name", "Candidate Owner")
    _git(main, environment, "config", "user.email", "owner@example.invalid")
    (main / "candidate.txt").write_text("original\n")
    _git(main, environment, "add", ".")
    _git(main, environment, "commit", "-m", "test: initialize")
    candidate = tmp_path / "candidate"
    _git(main, environment, "worktree", "add", "-b", "candidate", str(candidate))
    scripts = candidate / "scripts"
    scripts.mkdir()
    shutil.copy2(ROOT / "scripts/check-full.sh", scripts / "check-full.sh")
    source = candidate / "src/h2h_dev_tools"
    source.mkdir(parents=True)
    helper = ROOT / "src/h2h_dev_tools/git_environment.py"
    shutil.copy2(helper, source / helper.name)
    python = candidate / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    _executable(python, f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    recorder = tmp_path / "record.py"
    recorder.write_text(
        "import json, os, sys\nfrom pathlib import Path\n"
        f"names = {names!r}\n"
        f"Path({str(tmp_path)!r}, sys.argv[1] + '.json').write_text("
        "json.dumps({name: os.environ[name] for name in names if name in os.environ}))\n"
    )
    _executable(
        scripts / "check-fast.sh",
        f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(recorder))} fast\n",
    )
    (scripts / "build-and-verify-distributions.py").write_text(
        "import runpy, sys\n"
        "sys.argv[1:] = ['build']\n"
        f"runpy.run_path({str(recorder)!r}, run_name='__main__')\n"
    )
    (candidate / "test_isolation.py").write_text(
        "import os, subprocess\nfrom pathlib import Path\n"
        "def test_fixture_owns_its_git_metadata(tmp_path):\n"
        f"    assert not set({names!r}).intersection(os.environ)\n"
        "    assert os.environ['H2H_TEST_ENVIRONMENT_SENTINEL'] == 'preserved'\n"
        "    assert os.environ['GIT_CONFIG_GLOBAL'] == os.devnull\n"
        "    def git(*args):\n"
        "        return subprocess.run(('git', '-C', str(tmp_path), *args), check=True, capture_output=True, text=True).stdout.strip()\n"
        "    git('init', '-b', 'fixture')\n"
        "    git('config', 'user.name', 'Fixture Owner')\n"
        "    git('config', 'user.email', 'fixture@example.invalid')\n"
        "    (tmp_path / 'isolated.txt').write_text('disposable fixture')\n"
        "    git('add', '.')\n"
        "    git('commit', '-m', 'test: isolated fixture')\n"
        "    assert Path(git('rev-parse', '--show-toplevel')).resolve() == tmp_path.resolve()\n"
    )
    (candidate / "candidate.txt").write_text("staged candidate\n")
    _git(candidate, environment, "add", "candidate.txt")
    original_tree = _git(candidate, environment, "write-tree")
    original_entries = _git(candidate, environment, "ls-files", "--stage", "-z")
    original_config = (main / ".git/config").read_bytes()
    original_head = _git(candidate, environment, "rev-parse", "HEAD")
    hook = main / ".git/hooks/pre-commit"
    _executable(
        hook,
        "#!/bin/sh\nset -eu\n"
        f"{shlex.quote(sys.executable)} {shlex.quote(str(recorder))} before\n"
        "scripts/check-full.sh\n"
        f"{shlex.quote(sys.executable)} {shlex.quote(str(recorder))} after\n"
        "exit 1\n",
    )
    result = subprocess.run(
        ("git", "-C", str(candidate), "commit", "-m", "test: candidate gate"),
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1
    assert (tmp_path / "after.json").exists(), result.stdout + result.stderr
    before = json.loads((tmp_path / "before.json").read_text())
    assert Path(before["GIT_DIR"]).is_absolute()
    assert Path(before["GIT_INDEX_FILE"]).is_absolute()
    for stage in ("fast", "build", "after"):
        assert json.loads((tmp_path / f"{stage}.json").read_text()) == before
    # Git commit legitimately refreshes index stat data. Its exact staged tree
    # and entries must remain unchanged by the nested fixture's Git mutations.
    assert _git(candidate, environment, "write-tree") == original_tree
    assert _git(candidate, environment, "ls-files", "--stage", "-z") == original_entries
    assert (main / ".git/config").read_bytes() == original_config
    assert _git(candidate, environment, "rev-parse", "HEAD") == original_head
