"""The Git owner works within a consumer's exported package namespace."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from h2h_dev_tools import bundle


def test_exported_git_owner_passes_strict_typing_without_installed_toolkit(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.h2h-dev-tools]\ncomponents = ["git-flow"]\n'
        '[tool.mypy]\npython_version = "3.14"\nstrict = true\n'
        'files = ["scripts/_shared"]\n'
    )
    bundle.sync_bundle(tmp_path)
    # Consumers expose _shared as their own package, while the launcher must also
    # remain directly executable after the shell copies its source closure.
    (tmp_path / "scripts/_shared/__init__.py").write_text("")
    result = subprocess.run(
        (
            sys.executable,
            "-I",
            "-B",
            "-m",
            "mypy",
            "--no-site-packages",
            f"--cache-dir={os.devnull}",
            "--config-file=pyproject.toml",
        ),
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
