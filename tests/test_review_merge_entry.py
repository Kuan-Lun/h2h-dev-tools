from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ENTRY = (
    Path(__file__).resolve().parents[1] / "src/h2h_dev_tools/review-merge-candidate.sh"
)


@pytest.mark.parametrize("timeout", [None, "3600"])
def test_merge_review_passes_explicit_deadline_to_reviewer(
    tmp_path: Path, timeout: str | None
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "check_change_scope.py").write_text("print('full')\n")
    (scripts / "review-code.py").write_text(
        "import json, sys\nprint(json.dumps(sys.argv[1:]))\n"
    )
    local_bin = tmp_path / ".venv/bin"
    local_bin.mkdir(parents=True)
    (local_bin / "python").symlink_to(sys.executable)
    environment = os.environ.copy()
    environment.pop("H2H_CODE_REVIEW_TIMEOUT_SECONDS", None)
    if timeout is not None:
        environment["H2H_CODE_REVIEW_TIMEOUT_SECONDS"] = timeout
    result = subprocess.run(
        ("bash", str(_ENTRY), str(tmp_path)),
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == [
        "run",
        "--index",
        "--timeout-seconds",
        timeout or "900",
    ]
