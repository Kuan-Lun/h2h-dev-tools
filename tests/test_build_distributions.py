from __future__ import annotations

import csv
import importlib.metadata
import os
import runpy
import shutil
import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest


@pytest.mark.parametrize("missing_payload", [False, True])
def test_real_console_smoke_uses_installed_wheel_despite_pythonpath(
    tmp_path: Path, missing_payload: bool
) -> None:
    root = Path(__file__).resolve().parents[1]
    checkout = tmp_path / "candidate"
    checkout.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copyfile(root / name, checkout / name)
    shutil.copytree(
        root / "src", checkout / "src", ignore=shutil.ignore_patterns("__pycache__")
    )
    (checkout / "scripts").mkdir()
    script = checkout / "scripts/build-and-verify-distributions.py"
    shutil.copyfile(root / "scripts/build-and-verify-distributions.py", script)
    if missing_payload:
        project = checkout / "pyproject.toml"
        project.write_text(
            project.read_text().replace(
                "[tool.hatch.build.targets.wheel]\n",
                "[tool.hatch.build.targets.wheel]\n"
                'exclude = ["src/h2h_dev_tools/commit-message.sh"]\n',
            )
        )
        # The complete checkout would hide a missing installed shell resource.
        shadow = root / "src"
    else:
        shadow = tmp_path / "unrelated source"
        (shadow / "h2h_dev_tools").mkdir(parents=True)
        (shadow / "h2h_dev_tools/__init__.py").write_text(
            "raise RuntimeError('console imported unrelated PYTHONPATH source')\n"
        )
    result = subprocess.run(
        [sys.executable, "-B", str(script)],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(shadow)},
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
    )
    if missing_payload:
        assert result.returncode != 0, result.stdout
        assert "commit-message.sh" in result.stderr
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Verified sdist-derived" in result.stdout


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_offline_runtime_inventory_copies_only_selected_distributions(
    tmp_path: Path, platform: str
) -> None:
    root = Path(__file__).resolve().parents[1]
    script = runpy.run_path(str(root / "scripts/build-and-verify-distributions.py"))
    site = tmp_path / "site"
    script["provide_runtime_dependencies"](site, platform)
    names = {"packaging", "distlib"} if platform == "win32" else {"packaging"}
    expected = {}
    for name in names:
        distribution = importlib.metadata.distribution(name)
        record = distribution.read_text("RECORD")
        assert record is not None
        for row in csv.reader(StringIO(record, newline="")):
            assert len(row) == 3
            relative = Path(row[0])
            expected[relative.as_posix()] = Path(
                str(distribution.locate_file(relative))
            ).read_bytes()
    actual = {
        path.relative_to(site).as_posix(): path.read_bytes()
        for path in site.rglob("*")
        if path.is_file()
    }
    assert actual == expected
    assert {
        item.metadata["Name"].lower()
        for item in importlib.metadata.distributions(path=[str(site)])
    } == names


@pytest.mark.parametrize(
    "member", ["../outside.py", "packaging/../../outside.py", "unrelated/source.py"]
)
def test_offline_inventory_rejects_members_outside_distribution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, member: str
) -> None:
    root = Path(__file__).resolve().parents[1]
    script = runpy.run_path(str(root / "scripts/build-and-verify-distributions.py"))
    dependency = tmp_path / "dependency"
    metadata = dependency / "packaging-1.0.dist-info"
    metadata.mkdir(parents=True)
    (metadata / "METADATA").write_text("Name: packaging\nVersion: 1.0\n")
    (metadata / "RECORD").write_text(f"{member},,\n")
    # Distribution.files excludes missing RECORD entries before returning them.
    # Keep the malicious source present so the copier must reject its boundary.
    (dependency / "packaging").mkdir()
    source = (dependency / member).resolve()
    assert source.is_relative_to(tmp_path)
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"must not be copied")
    distribution = importlib.metadata.PathDistribution(metadata)
    monkeypatch.setattr(importlib.metadata, "distribution", lambda name: distribution)
    site = tmp_path / "site"
    with pytest.raises(ValueError, match="unexpected packaging distribution member"):
        script["provide_runtime_dependencies"](site, "linux")
    assert not site.exists()


def _write_record(dependency: Path, members: list[str]) -> None:
    output = StringIO(newline="")
    csv.writer(output).writerows((member, "", "") for member in members)
    (dependency / "packaging-1.0.dist-info/RECORD").write_text(
        output.getvalue(), encoding="utf-8", newline=""
    )


@pytest.fixture
def installed_packaging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    dependency = tmp_path / "dependency"
    contents = {
        "packaging/__init__.py": "",
        "packaging/py.typed": "",
        "packaging/data,quoted.txt": "CSV quoted member\n",
        "packaging-1.0.dist-info/METADATA": "Name: packaging\nVersion: 1.0\n",
        "packaging-1.0.dist-info/WHEEL": "Wheel-Version: 1.0\n",
        "packaging-1.0.dist-info/licenses/LICENSE": "test license\n",
    }
    for member, content in contents.items():
        target = dependency / member
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    _write_record(dependency, [*contents, "packaging-1.0.dist-info/RECORD"])
    distribution = importlib.metadata.PathDistribution(
        dependency / "packaging-1.0.dist-info"
    )
    monkeypatch.setattr(importlib.metadata, "distribution", lambda name: distribution)
    return dependency


def _provide_packaging(site: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    script = runpy.run_path(str(root / "scripts/build-and-verify-distributions.py"))
    script["provide_runtime_dependencies"](site, "linux")


def test_offline_inventory_preserves_complete_csv_inventory(
    tmp_path: Path, installed_packaging: Path
) -> None:
    expected = {
        path.relative_to(installed_packaging).as_posix(): path.read_bytes()
        for path in installed_packaging.rglob("*")
        if path.is_file()
    }
    site = tmp_path / "site"
    _provide_packaging(site)
    assert {
        path.relative_to(site).as_posix(): path.read_bytes()
        for path in site.rglob("*")
        if path.is_file()
    } == expected
    assert (
        '"packaging/data,quoted.txt"'
        in (site / "packaging-1.0.dist-info/RECORD").read_text()
    )


@pytest.mark.parametrize(
    "member", ["packaging/py.typed", "packaging-1.0.dist-info/licenses/LICENSE"]
)
def test_offline_inventory_rejects_missing_recorded_file_before_copy(
    tmp_path: Path, installed_packaging: Path, member: str
) -> None:
    (installed_packaging / member).unlink()
    site = tmp_path / "site"
    with pytest.raises(ValueError, match="missing packaging distribution file"):
        _provide_packaging(site)
    assert not site.exists()


@pytest.mark.parametrize(
    "record",
    [
        None,
        "",
        "\n",
        "packaging/py.typed\n",
        "packaging/py.typed,,,\n",
        ",,\n",
        '"unterminated,,\n',
    ],
)
def test_offline_inventory_rejects_missing_or_malformed_record(
    tmp_path: Path, installed_packaging: Path, record: str | None
) -> None:
    path = installed_packaging / "packaging-1.0.dist-info/RECORD"
    if record is None:
        path.unlink()
    else:
        path.write_text(record)
    site = tmp_path / "site"
    with pytest.raises(ValueError, match="installed-file inventory"):
        _provide_packaging(site)
    assert not site.exists()


@pytest.mark.parametrize(
    "member",
    [
        "packaging/./py.typed",
        "packaging//py.typed",
        "packaging/../packaging/py.typed",
        "packaging-2.0.dist-info/METADATA",
        "/packaging/py.typed",
        "packaging/py.typed",
    ],
)
def test_offline_inventory_rejects_late_unsafe_or_duplicate_member_before_copy(
    tmp_path: Path, installed_packaging: Path, member: str
) -> None:
    _write_record(installed_packaging, ["packaging/py.typed", member])
    site = tmp_path / "site"
    with pytest.raises(ValueError, match="unexpected packaging distribution member"):
        _provide_packaging(site)
    assert not site.exists()


@pytest.mark.parametrize("member", ["__init__.py", "METADATA", "WHEEL", "RECORD"])
def test_offline_inventory_requires_package_and_wheel_metadata(
    tmp_path: Path, installed_packaging: Path, member: str
) -> None:
    record = installed_packaging / "packaging-1.0.dist-info/RECORD"
    rows = list(csv.reader(StringIO(record.read_text(), newline="")))
    _write_record(
        installed_packaging,
        [row[0] for row in rows if Path(row[0]).name != member],
    )
    site = tmp_path / "site"
    with pytest.raises(
        ValueError, match="incomplete packaging installed-file inventory"
    ):
        _provide_packaging(site)
    assert not site.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink boundary fixture")
@pytest.mark.parametrize("boundary", ["source-file", "source-directory", "destination"])
def test_offline_inventory_rejects_symlink_escape_before_copy(
    tmp_path: Path, installed_packaging: Path, boundary: str
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "py.typed").write_bytes(b"outside must not change")
    site = tmp_path / "site"
    if boundary == "source-file":
        member = installed_packaging / "packaging/py.typed"
        member.unlink()
        member.symlink_to(outside / "py.typed")
    elif boundary == "source-directory":
        shutil.rmtree(installed_packaging / "packaging")
        (installed_packaging / "packaging").symlink_to(
            outside, target_is_directory=True
        )
    else:
        site.mkdir()
        (site / "packaging").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="distribution member escapes"):
        _provide_packaging(site)
    assert {path.name: path.read_bytes() for path in outside.iterdir()} == {
        "py.typed": b"outside must not change"
    }
    assert not (site / "packaging-1.0.dist-info").exists()
    if boundary != "destination":
        assert not site.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink root fixture")
def test_offline_inventory_accepts_symlinked_installation_root(
    tmp_path: Path, installed_packaging: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = tmp_path / "linked site"
    link.symlink_to(installed_packaging, target_is_directory=True)
    distribution = importlib.metadata.PathDistribution(link / "packaging-1.0.dist-info")
    monkeypatch.setattr(importlib.metadata, "distribution", lambda name: distribution)
    site = tmp_path / "site"
    _provide_packaging(site)
    assert (site / "packaging-1.0.dist-info/RECORD").read_bytes() == (
        installed_packaging / "packaging-1.0.dist-info/RECORD"
    ).read_bytes()
