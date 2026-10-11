from __future__ import annotations

import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from h2h_dev_tools.environment import source_requirement

_RESOURCES = Path(__file__).resolve().parents[1] / "src/h2h_dev_tools"


def _wheel(
    directory: Path,
    *,
    filename: str = "source_example",
    name: str = "source-example",
    origin: str = "fixture",
) -> Path:
    wheel = directory / f"{filename}-1.0-py3-none-any.whl"
    metadata = f"{filename}-1.0.dist-info"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            f"{metadata}/METADATA",
            f"Metadata-Version: 2.3\nName: {name}\nVersion: 1.0\nRequires-Python: >=3.14\n",
        )
        archive.writestr(
            f"{metadata}/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(f"{metadata}/RECORD", "")
        archive.writestr("source_example.py", f"ORIGIN = {origin!r}\n")
    return wheel


@pytest.fixture
def environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[build-system]\nrequires=[]\nbuild-backend="fixture_backend"\nbackend-path=["."]\n'
        '[project]\nname="environment-fixture"\nversion="1.0"\ndependencies=[]\n'
        "[project.optional-dependencies]\ndev=[]\n",
        encoding="utf-8",
    )
    (root / "fixture_backend.py").write_text(
        "from pathlib import Path\nimport zipfile\n"
        "def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):\n"
        "    name = 'environment_fixture-1.0-py3-none-any.whl'\n"
        "    prefix = 'environment_fixture-1.0.dist-info/'\n"
        "    with zipfile.ZipFile(Path(wheel_directory) / name, 'w') as archive:\n"
        "        archive.writestr(prefix + 'METADATA', 'Metadata-Version: 2.3\\nName: environment-fixture\\nVersion: 1.0\\n')\n"
        "        archive.writestr(prefix + 'WHEEL', 'Wheel-Version: 1.0\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n')\n"
        "        archive.writestr(prefix + 'RECORD', '')\n"
        "    return name\n"
        "build_editable = build_wheel\n",
        encoding="utf-8",
    )
    original = root / ".venv"
    original.mkdir()
    (original / "preserve-me").write_text("existing development environment")
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("UV_OFFLINE", "1")
    monkeypatch.setenv("UV_PYTHON_DOWNLOADS", "never")
    tools = tmp_path / "bin"
    tools.mkdir()
    npm = tools / "npm"
    npm.write_text("#!/usr/bin/env bash\ntouch .npm-ran\n")
    npm.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{os.environ['PATH']}")
    return root


def _resolve(root: Path, *assignments: str) -> subprocess.CompletedProcess[str]:
    command = [
        "bash",
        str(_RESOURCES / "resolve-sources.sh"),
        str(root / "requirements.in"),
    ]
    for assignment in assignments:
        command.extend(("--source", assignment))
    return subprocess.run(
        command, cwd=root, capture_output=True, text=True, timeout=30, check=False
    )


@pytest.mark.parametrize(
    "source_kind",
    [
        "missing-wheel",
        "missing-archive",
        "missing-file-url",
        "project-without-manifest",
        "wrong-project-name",
        "wrong-wheel-name",
        "wrong-wheel-metadata",
        "incompatible-bound",
        "invalid-wheel",
    ],
)
def test_invalid_source_preserves_existing_environment(
    environment: Path, source_kind: str
) -> None:
    root = environment
    if source_kind == "project-without-manifest":
        source = root / "missing metadata"
        source.mkdir()
    elif source_kind == "wrong-project-name":
        source = root
    elif source_kind == "wrong-wheel-name":
        source = _wheel(root, filename="different_package", name="different-package")
    elif source_kind == "wrong-wheel-metadata":
        source = _wheel(root, name="different-package")
    elif source_kind == "incompatible-bound":
        source = _wheel(root)
        manifest = root / "pyproject.toml"
        manifest.write_text(
            manifest.read_text().replace(
                "dependencies=[]", 'dependencies=["source-example<1"]'
            )
        )
    elif source_kind == "invalid-wheel":
        source = root / "source_example-1.0-py3-none-any.whl"
        source.write_text("not a wheel")
    else:
        source = root / (
            "missing.tar.gz" if source_kind == "missing-archive" else "missing.whl"
        )
    source_value = source.as_uri() if source_kind == "missing-file-url" else str(source)
    result = subprocess.run(
        (
            "bash",
            str(_RESOURCES / "rebuild-env.sh"),
            str(root),
            "",
            "--source",
            f"source-example={source_value}",
        ),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert not (root / ".npm-ran").exists(), "invalid sources must not reach npm"
    assert (
        root / ".venv/preserve-me"
    ).read_text() == "existing development environment"


def test_real_uv_accepts_normalized_name_and_encoded_local_wheel(
    environment: Path,
) -> None:
    directory = environment / "candidate with spaces"
    directory.mkdir()
    wheel = _wheel(directory)
    result = _resolve(environment, f"Source.Example={wheel}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        environment / "requirements.in"
    ).read_text() == f"Source.Example @ {wheel.as_uri()}\n"
    assert (environment / ".venv/preserve-me").exists()


def test_real_uv_rejects_metadata_that_disagrees_with_named_source(
    environment: Path,
) -> None:
    wheel = _wheel(environment, name="different-package")
    result = _resolve(environment, f"source-example={wheel}")
    assert result.returncode != 0
    assert "different-package" in result.stderr
    assert "source-example" in result.stderr


def test_local_project_remains_editable(environment: Path) -> None:
    result = _resolve(environment, f"environment-fixture={environment}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        environment / "requirements.in"
    ).read_text() == f"-e environment-fixture @ {environment.as_uri()}\n"


@pytest.mark.parametrize(
    "assignment",
    [
        "bad name=index",
        "missing-equals",
        "example=",
        "example=index\nother-package",
        "example=wheel\0.whl",
    ],
)
def test_rejects_invalid_or_multiline_assignments(assignment: str) -> None:
    with pytest.raises(ValueError, match="Invalid dependency override"):
        source_requirement(assignment)


def test_validated_sources_install_without_a_preexisting_python_environment(
    environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("UV_OFFLINE")
    monkeypatch.setenv("UV_NO_INDEX", "1")
    wheel = _wheel(environment)
    result = subprocess.run(
        (
            "bash",
            str(_RESOURCES / "rebuild-env.sh"),
            str(environment),
            "",
            "--source",
            f"source-example={wheel}",
        ),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (environment / ".venv/preserve-me").exists()
    assert (environment / ".npm-ran").exists()
    python = (
        environment
        / ".venv"
        / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    installed = subprocess.run(
        (
            str(python),
            "-I",
            "-c",
            "from importlib.metadata import version;print(version('source-example'), version('environment-fixture'))",
        ),
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert installed.stdout == "1.0 1.0\n"


def test_local_uri_preserves_url_reserved_path_characters(tmp_path: Path) -> None:
    wheel = tmp_path / "artifact # with % in name.whl"
    wheel.touch()
    assert source_requirement(f"example={wheel}") == f"example @ {wheel.as_uri()}"


@pytest.mark.parametrize(
    "source",
    [
        "index",
        "https://example.invalid/distribution.tar.gz",
        "git+https://example.invalid/repo.git@ref",
    ],
)
def test_index_and_remote_sources_retain_the_requested_package(
    source: str, tmp_path: Path
) -> None:
    expected = "example" if source == "index" else f"example @ {source}"
    assert source_requirement(f"example={source}") == expected
    assert source_requirement(f"example={source}", source_base=tmp_path) == expected


@pytest.mark.parametrize("entry", ["shared", "local-wrapper"])
@pytest.mark.parametrize("caller_has_wheel", [True, False])
def test_rebuild_uses_caller_artifact_instead_of_repository_decoy(
    environment: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    caller_has_wheel: bool,
) -> None:
    monkeypatch.delenv("UV_OFFLINE")
    monkeypatch.setenv("UV_NO_INDEX", "1")
    caller = tmp_path / "caller checkout"
    caller.mkdir()
    for checkout in (environment, caller):
        subprocess.run(
            ("git", "init", "-b", "master", str(checkout)),
            capture_output=True,
            check=True,
        )
    decoy = _wheel(environment, origin="repository-decoy")
    expected = caller / decoy.name
    if caller_has_wheel:
        assert _wheel(caller, origin="caller-selected") == expected
    callback = environment / "after-build.py"
    callback.write_text(
        "import json\nfrom importlib.metadata import distribution\n"
        "from pathlib import Path\nimport source_example\n"
        "package = distribution('source-example')\n"
        "Path('installed.json').write_text(json.dumps({\n"
        " 'origin': source_example.ORIGIN, 'version': package.version,\n"
        " 'direct_url': json.loads(package.read_text('direct_url.json')),\n"
        " 'cwd': str(Path.cwd())}))\n"
    )
    if entry == "local-wrapper":
        for relative in (
            "scripts/rebuild-env.sh",
            "src/h2h_dev_tools/rebuild-env.sh",
            "src/h2h_dev_tools/resolve-sources.sh",
            "src/h2h_dev_tools/environment.py",
        ):
            target = environment / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(_RESOURCES.parents[1] / relative, target)
        command = ["bash", str(environment / "scripts/rebuild-env.sh")]
    else:
        command = [
            "bash",
            str(_RESOURCES / "rebuild-env.sh"),
            str(environment),
            callback.name,
        ]
    result = subprocess.run(
        [*command, "--source", f"source-example=./{decoy.name}"],
        cwd=caller,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if not caller_has_wheel:
        assert result.returncode != 0, result.stdout + result.stderr
        assert (environment / ".venv/preserve-me").read_text() == (
            "existing development environment"
        )
        assert not (environment / ".npm-ran").exists()
        return
    assert result.returncode == 0, result.stdout + result.stderr
    if entry == "local-wrapper":
        python = (
            environment
            / ".venv"
            / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )
        subprocess.run(
            (str(python), callback.name),
            cwd=environment,
            capture_output=True,
            check=True,
            timeout=10,
        )
    installed = json.loads((environment / "installed.json").read_text())
    assert installed["origin"] == "caller-selected"
    assert installed["version"] == "1.0"
    assert installed["direct_url"]["url"] == expected.as_uri()
    assert installed["cwd"] == str(environment)
    assert (environment / ".npm-ran").exists()
    assert not (caller / ".venv").exists()


@pytest.mark.parametrize("kind", ["wheel", "project", "file-url", "absolute"])
def test_explicit_source_base_only_resolves_relative_local_sources(
    tmp_path: Path, kind: str
) -> None:
    caller = tmp_path / "caller # directory"
    caller.mkdir()
    source = caller / "candidate"
    if kind == "project":
        source.mkdir()
        (source / "pyproject.toml").write_text('[project]\nname="example"\n')
    else:
        source.touch()
    value = {
        "wheel": "./candidate",
        "project": "./candidate",
        "file-url": "file:./candidate",
        "absolute": str(source),
    }[kind]
    prefix = "-e " if kind == "project" else ""
    assert source_requirement(f"example={value}", source_base=caller) == (
        f"{prefix}example @ {source.as_uri()}"
    )
