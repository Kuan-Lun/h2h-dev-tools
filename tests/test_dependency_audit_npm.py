from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from h2h_dev_tools import dependency_audit


@pytest.mark.parametrize("global_install", [False, True])
def test_windows_npm_layout_runs_without_shell_or_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, global_install: bool
) -> None:
    node = shutil.which("node")
    assert node is not None, "the repository Node development dependency is required"
    directory = tmp_path / "Node installation 空白"
    prefix = tmp_path / "User prefix 空白"
    entries = directory / "node_modules" / "npm" / "bin"
    entries.mkdir(parents=True)
    launcher = directory / "npm.cmd"
    launcher.write_text("@exit /b 99\n", encoding="ascii")
    (entries / "npm-prefix.js").write_text(
        f"console.log({json.dumps(str(prefix))});\n", encoding="utf-8"
    )
    selected = prefix / "node_modules" / "npm" / "bin" if global_install else entries
    selected.mkdir(parents=True, exist_ok=True)
    (entries / "npm-cli.js").write_text("process.exit(98);\n", encoding="ascii")
    specification = "@fixture/name@>=1 <3 || >=4 & literal%VALUE%"
    (selected / "npm-cli.js").write_text(
        "const expected = "
        + json.dumps(["view", specification, "version", "--json"])
        + ";\nif (JSON.stringify(process.argv.slice(2)) !== JSON.stringify(expected)) "
        "process.exit(97);\nconsole.log(JSON.stringify(['1.0.0', '2.0.0']));\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        shutil, "which", lambda name: str(launcher) if name == "npm" else node
    )
    assert dependency_audit._npm_view_version(specification) == ["1.0.0", "2.0.0"]


def test_native_launcher_is_resolved_without_a_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: "/tools/npm")
    assert dependency_audit._npm_command() == ("/tools/npm",)


def test_missing_npm_fails_before_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(FileNotFoundError, match="npm on PATH"):
        dependency_audit._npm_command()


@pytest.mark.parametrize("missing", ["node", "cli", "prefix"])
def test_incomplete_windows_installation_fails_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    entries = tmp_path / "node_modules" / "npm" / "bin"
    entries.mkdir(parents=True)
    for name in ("cli", "prefix"):
        if missing != name:
            (entries / f"npm-{name}.js").write_text("", encoding="ascii")
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: (
            str(tmp_path / "npm.cmd")
            if name == "npm"
            else None
            if missing == "node"
            else "node"
        ),
    )
    with pytest.raises(FileNotFoundError):
        dependency_audit._npm_command()


def test_local_node_and_prefix_failure_are_not_silently_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = tmp_path / "node.exe"
    node.write_bytes(b"fixture")
    entries = tmp_path / "node_modules" / "npm" / "bin"
    entries.mkdir(parents=True)
    for name in ("cli", "prefix"):
        (entries / f"npm-{name}.js").write_text("", encoding="ascii")
    monkeypatch.setattr(shutil, "which", lambda _name: str(tmp_path / "npm.cmd"))

    def fail(
        command: tuple[str, ...], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        assert command == (str(node), str(entries / "npm-prefix.js"))
        raise subprocess.CalledProcessError(7, command)

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError) as error:
        dependency_audit._npm_command()
    assert error.value.returncode == 7
