from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SELECTORS = (
    "tests/test_release_policy.py::test_compatible_tool_release_has_one_dedicated_version_transition",
    "tests/test_publish_version.py::test_publish_version_uses_the_entire_multi_commit_push",
    "tests/test_merge_workflow.py::test_documentation_merge_does_not_run_or_verify_review",
)


def _checkout(destination: Path) -> None:
    destination.mkdir()
    for directory in ("src", "scripts", ".githooks"):
        shutil.copytree(
            _ROOT / directory,
            destination / directory,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
    for filename in ("pyproject.toml", "AGENTS.md"):
        shutil.copy2(_ROOT / filename, destination / filename)
    tests = destination / "tests"
    tests.mkdir()
    for selector in _SELECTORS:
        relative = Path(selector.split("::", 1)[0])
        shutil.copy2(_ROOT / relative, destination / relative)
    configuration = _ROOT / "tests/conftest.py"
    if configuration.exists():
        shutil.copy2(configuration, tests / configuration.name)


@pytest.mark.skipif(os.name == "nt", reason="Exercises the POSIX merge workflow")
@pytest.mark.parametrize("authority", ("global", "system", "count", "parameters"))
def test_disposable_git_fixtures_ignore_host_signing_configuration(
    tmp_path: Path, authority: str
) -> None:
    checkout = tmp_path / "checkout"
    _checkout(checkout)
    marker = tmp_path / "unexpected-signing"
    signer = tmp_path / "fake-gpg"
    signer.write_text(
        f"#!{sys.executable}\n"
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('host signing configuration was used')\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    signer.chmod(0o755)
    # This adversarial child starts from an explicit Git configuration; only the
    # selected authority is poisoned. The surrounding pytest/gate is untouched.
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("GIT_", "PYTEST_"))
    }
    environment.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
    )
    settings = (("commit.gpgsign", "true"), ("gpg.program", str(signer)))
    if authority in {"global", "system"}:
        configuration = tmp_path / f"{authority}.gitconfig"
        for key, value in settings:
            subprocess.run(
                ("git", "config", "--file", str(configuration), key, value),
                check=True,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
            )
        environment[f"GIT_CONFIG_{authority.upper()}"] = str(configuration)
        if authority == "system":
            environment.pop("GIT_CONFIG_NOSYSTEM")
    elif authority == "count":
        environment["GIT_CONFIG_COUNT"] = str(len(settings))
        for index, (key, value) in enumerate(settings):
            environment[f"GIT_CONFIG_KEY_{index}"] = key
            environment[f"GIT_CONFIG_VALUE_{index}"] = value
    else:
        environment["GIT_CONFIG_PARAMETERS"] = " ".join(
            "'" + f"{key}={value}".replace("'", "'\\''") + "'"
            for key, value in settings
        )
    before = dict(os.environ)

    result = subprocess.run(
        (
            sys.executable,
            "-B",
            "scripts/run-pytest.py",
            "-p",
            "no:cacheprovider",
            "-q",
            *_SELECTORS,
        ),
        cwd=checkout,
        env=environment,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )

    assert dict(os.environ) == before
    assert not marker.exists(), result.stdout + result.stderr
    assert result.returncode == 0, result.stdout + result.stderr
    assert "3 passed" in result.stdout


def test_configuration_isolation_preserves_local_authority_and_explicit_overrides(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    configuration = (_ROOT / "tests/conftest.py").read_text(encoding="utf-8")
    (checkout / "conftest.py").write_text(
        configuration + "\n\ndef pytest_sessionfinish():\n"
        "    assert os.environ['GIT_CONFIG_GLOBAL'] == 'host-global'\n"
        "    assert os.environ['GIT_CONFIG_PARAMETERS'] == 'host-parameters'\n"
        "    assert os.environ['GIT_CONFIG_COUNT'] == '1'\n",
        encoding="utf-8",
    )
    (checkout / "test_boundary.py").write_text(
        "import os, subprocess\n"
        "def test_boundary(monkeypatch, tmp_path):\n"
        "    assert os.environ['GIT_DIR'] == 'candidate.git'\n"
        "    assert os.environ['GIT_INDEX_FILE'] == 'candidate.index'\n"
        "    assert os.environ['GIT_WORK_TREE'] == 'candidate-worktree'\n"
        "    assert os.environ['GIT_CONFIG_NOT_AN_AUTHORITY'] == 'preserved'\n"
        "    assert not any(key in os.environ for key in (\n"
        "        'GIT_CONFIG', 'GIT_CONFIG_COUNT', 'GIT_CONFIG_PARAMETERS',\n"
        "        'GIT_CONFIG_KEY_0', 'GIT_CONFIG_VALUE_0'))\n"
        "    configuration = tmp_path / 'explicit.gitconfig'\n"
        "    configuration.write_text('[fixture]\\nvalue = explicit\\n')\n"
        "    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(configuration))\n"
        "    result = subprocess.run(\n"
        "        ['git', 'config', '--global', '--get', 'fixture.value'],\n"
        "        capture_output=True, text=True, check=True, timeout=10)\n"
        "    assert result.stdout.strip() == 'explicit'\n",
        encoding="utf-8",
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("GIT_", "PYTEST_"))
    }
    environment.update(
        GIT_DIR="candidate.git",
        GIT_INDEX_FILE="candidate.index",
        GIT_WORK_TREE="candidate-worktree",
        GIT_CONFIG="host-config",
        GIT_CONFIG_GLOBAL="host-global",
        GIT_CONFIG_PARAMETERS="host-parameters",
        GIT_CONFIG_COUNT="1",
        GIT_CONFIG_KEY_0="host.key",
        GIT_CONFIG_VALUE_0="host-value",
        GIT_CONFIG_NOT_AN_AUTHORITY="preserved",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
    )

    result = subprocess.run(
        (sys.executable, "-B", "-m", "pytest", "-p", "no:cacheprovider", "-q"),
        cwd=checkout,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
