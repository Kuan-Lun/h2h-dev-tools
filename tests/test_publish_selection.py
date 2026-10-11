from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("requested,expected", [("0.1.0", 0), ("0.1.1", 1), ("", 1)])
def test_dispatch_requires_the_exact_candidate_version(
    tmp_path: Path, requested: str, expected: int
) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
    output = tmp_path / "output"
    environment = os.environ | {
        "EVENT_NAME": "workflow_dispatch",
        "REQUESTED_VERSION": requested,
        "GITHUB_OUTPUT": str(output),
    }
    result = subprocess.run(
        (sys.executable, str(_ROOT / "scripts/select-publish-version.py")),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == expected, result.stderr
    if expected == 0:
        assert output.read_text() == "publish=true\n"
    else:
        assert not output.exists()


def test_workflow_limits_publication_to_default_branch_and_publish_job() -> None:
    text = (_ROOT / ".github/workflows/publish.yml").read_text()
    assert "github.ref ==" in text
    assert "format('refs/heads/{0}', github.event.repository.default_branch)" in text
    validate, publish = text.split("\n  publish:\n", 1)
    assert "id-token: write" not in validate
    assert "environment: pypi" in publish
    assert "id-token: write" in publish
    assert "BEFORE_SHA: ${{ github.event.before }}" in validate
    assert "REQUESTED_VERSION: ${{ inputs.version }}" in validate
