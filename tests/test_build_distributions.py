from __future__ import annotations

import importlib.metadata
import os
import runpy
import shutil
import subprocess
import sys
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
        assert distribution.files is not None
        for relative in distribution.files:
            expected[str(relative)] = Path(
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
