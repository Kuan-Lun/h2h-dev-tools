from __future__ import annotations

import os
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
