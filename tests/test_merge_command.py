"""The Git owner works within a consumer's exported package namespace."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from h2h_dev_tools import bundle, merge_command


@pytest.mark.parametrize("failed_operation", ["seek", "read", "close"])
def test_recovery_diagnostic_io_error_preserves_result_and_cleanup_receipt(
    tmp_path: Path, failed_operation: str
) -> None:
    receipt = tmp_path / "cleanup"
    marker = tmp_path / "completed"
    module_root = str(Path(merge_command.__file__).resolve().parents[1])
    script = (
        "import sys\n"
        f"sys.path.insert(0, {module_root!r})\n"
        "from h2h_dev_tools import merge_command\n"
        "original = merge_command.tempfile.TemporaryFile\n"
        "class BrokenReplay:\n"
        "    def __init__(self): self.stream = original()\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *args): self.close()\n"
        "    def fileno(self): return self.stream.fileno()\n"
        "    def seek(self, offset):\n"
        f"        if {failed_operation == 'seek'!r}: raise OSError('injected seek failure')\n"
        "        return self.stream.seek(offset)\n"
        "    def read(self, size):\n"
        f"        if {failed_operation == 'read'!r}: raise OSError('injected read failure')\n"
        "        return self.stream.read(size)\n"
        "    def close(self):\n"
        "        self.stream.close()\n"
        f"        if {failed_operation == 'close'!r}: raise OSError('injected close failure')\n"
        "merge_command.tempfile.TemporaryFile = BrokenReplay\n"
        "raise SystemExit(merge_command.main())\n"
    )
    child = (
        f"from pathlib import Path; Path({str(marker)!r}).touch(); "
        "print('recovery output'); raise SystemExit(7)"
    )
    result = subprocess.run(
        (
            sys.executable,
            "-I",
            "-B",
            "-c",
            script,
            "--cleanup-receipt",
            str(receipt),
            "--recovery",
            str(tmp_path),
            "--",
            sys.executable,
            "-c",
            child,
        ),
        capture_output=True,
        check=False,
        timeout=15,
    )
    assert marker.exists()
    assert result.returncode == 7, result.stderr
    assert receipt.read_text() == "owned_tree_empty\n"


def test_recovery_finishes_before_replaying_bounded_unterminated_output(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "completed"
    receipt = tmp_path / "cleanup"
    child = (
        "import sys\nfrom pathlib import Path\n"
        "sys.stdout.write('o' * (96 * 1024))\nsys.stdout.flush()\n"
        "sys.stderr.write('e' * (96 * 1024))\nsys.stderr.flush()\n"
        f"Path({str(marker)!r}).touch()\n"
    )
    result = subprocess.run(
        (
            sys.executable,
            "-I",
            "-B",
            merge_command.__file__,
            "--cleanup-receipt",
            str(receipt),
            "--recovery",
            str(tmp_path),
            "--",
            sys.executable,
            "-c",
            child,
        ),
        capture_output=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert marker.exists()
    assert receipt.read_text() == "owned_tree_empty\n"
    assert len(result.stderr) < 70_000
    assert b"recovery diagnostics truncated after 64 KiB" in result.stderr


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
