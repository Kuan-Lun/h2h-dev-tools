from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from h2h_dev_tools import offline_wheel


def _repository(root: Path) -> tuple[Path, Path]:
    repository = root / "consumer checkout"
    repository.mkdir()
    (repository / ".venv").symlink_to(Path(sys.prefix), target_is_directory=True)
    callback = repository / "smoke callback.py"
    callback.write_text("raise SystemExit(0)\n")
    return repository, callback


@pytest.mark.parametrize("callback_status", [0, 7])
def test_real_offline_candidate_install_and_callback_cleanup(
    tmp_path: Path, callback_status: int
) -> None:
    repository, callback = _repository(tmp_path)
    (repository / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
        '[project]\nname = "offline-wheel-fixture"\nversion = "1.0.0"\n'
        'dependencies = ["packaging>=26"]\n'
        '[tool.hatch.build.targets.wheel]\npackages = ["src/offline_fixture"]\n'
    )
    package = repository / "src/offline_fixture"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('VALUE = "candidate wheel"\n')
    result_file = tmp_path / "callback evidence.json"
    callback.write_text(
        "import json, pathlib, sys\n"
        "from importlib.metadata import distribution\n"
        "import offline_fixture, packaging\n"
        "prefix = pathlib.Path(sys.prefix).resolve()\n"
        "assert sys.flags.isolated and not sys.flags.optimize\n"
        "assert pathlib.Path(offline_fixture.__file__).resolve().is_relative_to(prefix)\n"
        "assert not pathlib.Path(packaging.__file__).resolve().is_relative_to(prefix)\n"
        "assert offline_fixture.VALUE == 'candidate wheel'\n"
        "assert distribution('offline-wheel-fixture').version == '1.0.0'\n"
        "assert sys.argv[2] == '--literal=$value with spaces'\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps({'prefix': str(prefix), 'cwd': str(pathlib.Path.cwd())}))\n"
        f"raise SystemExit({callback_status})\n"
    )
    arguments = [str(result_file), "--literal=$value with spaces"]
    if callback_status:
        with pytest.raises(subprocess.CalledProcessError) as error:
            offline_wheel.run_offline_smoke(repository, callback, arguments)
        assert error.value.returncode == callback_status
    else:
        offline_wheel.run_offline_smoke(repository, callback, arguments)
    observed = json.loads(result_file.read_text())
    assert not Path(observed["cwd"]).is_relative_to(repository)
    assert not Path(observed["cwd"]).exists()
    assert not Path(observed["prefix"]).exists()
    assert not (repository / "dist").exists()


@pytest.mark.parametrize("wheel_count", [0, 2])
def test_ambiguous_build_never_runs_callback_and_removes_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wheel_count: int
) -> None:
    repository, callback = _repository(tmp_path)
    artifact_roots: list[Path] = []
    monkeypatch.setattr(offline_wheel, "_site", lambda *_args, **_kwargs: tmp_path)

    def build(command: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        assert command[1:3] == ["-m", "build"]
        output = Path(command[command.index("--outdir") + 1])
        artifact_roots.append(output.parent)
        assert options["env"]["UV_OFFLINE"] == "1"
        assert Path(options["env"]["UV_CACHE_DIR"]).parent == output.parent
        output.mkdir()
        for index in range(wheel_count):
            (output / f"candidate-{index}.whl").write_bytes(b"invalid fake wheel")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", build)
    with pytest.raises(ValueError, match="exactly one regular wheel"):
        offline_wheel.run_offline_smoke(repository, callback)
    assert artifact_roots and not artifact_roots[0].exists()


@pytest.mark.parametrize("failed_stage", ["build", "venv", "install"])
def test_failed_preparation_never_calls_oracle_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_stage: str
) -> None:
    repository, callback = _repository(tmp_path)
    artifact_roots: list[Path] = []
    monkeypatch.setattr(offline_wheel, "_site", lambda *_args, **_kwargs: tmp_path)

    def run(command: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        assert str(callback) not in command
        stage = (
            "build"
            if command[1] == "-m"
            else "venv"
            if command[1] == "venv"
            else "install"
        )
        cache = Path(options["env"]["UV_CACHE_DIR"])
        artifact_roots.append(cache.parent)
        assert options["env"]["UV_OFFLINE"] == "1"
        if stage == failed_stage:
            raise subprocess.CalledProcessError(5, command)
        if stage == "build":
            output = Path(command[command.index("--outdir") + 1])
            output.mkdir()
            (output / "candidate.whl").write_bytes(b"candidate")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        offline_wheel.run_offline_smoke(repository, callback)
    assert artifact_roots and all(not path.exists() for path in artifact_roots)


def test_cli_forwards_callback_arguments_without_shell_interpretation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[tuple[Path, Path, list[str]]] = []

    def run(repository: Path, callback: Path, arguments: list[str]) -> None:
        observed.append((repository, callback, arguments))

    monkeypatch.setattr(offline_wheel, "run_offline_smoke", run)
    assert (
        offline_wheel.main(
            [
                "--repository",
                str(tmp_path),
                "--smoke-script",
                "scripts/check.py",
                "--",
                "--domain-flag",
                "$value with spaces",
            ]
        )
        == 0
    )
    assert observed == [
        (tmp_path, Path("scripts/check.py"), ["--domain-flag", "$value with spaces"])
    ]


def test_callback_cannot_escape_repository(tmp_path: Path) -> None:
    repository, _callback = _repository(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("raise AssertionError('must not execute')\n")
    with pytest.raises(ValueError, match="inside the repository"):
        offline_wheel.run_offline_smoke(repository, outside)
