from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from h2h_dev_tools import dependency_audit


def test_python_inventory_includes_build_runtime_optional_and_groups() -> None:
    document = {
        "build-system": {"requires": ["build>=1"]},
        "project": {
            "dependencies": ["runtime>=2"],
            "optional-dependencies": {"dev": ["dev>=3"]},
        },
        "dependency-groups": {"type": ["types>=4"]},
    }
    assert dependency_audit._python_requirements(document) == [
        ("build", "build>=1"),
        ("runtime", "runtime>=2"),
        ("optional:dev", "dev>=3"),
        ("group:type", "types>=4"),
    ]


def test_python_audit_keeps_latest_outside_bound_and_upstream_review_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload: dict[str, Any] = {
        "info": {
            "version": "3.0",
            "package_url": "https://pypi.org/project/fixture/",
            "project_urls": {"Release-notes": "https://example.invalid/releases"},
        },
        "releases": {"3.0": [{"requires_python": ">=3.14", "yanked": False}]},
    }
    monkeypatch.setattr(dependency_audit, "_pypi_json", lambda _name: payload)
    result = dependency_audit._audit_python("runtime", "fixture>=1,<2")
    assert result["latest"] == "3.0"
    assert result["latest_satisfies"] is False
    assert result["python_3_14"] is True
    assert result["release_notes"] == "https://example.invalid/releases"
    assert result["registry_url"] == "https://pypi.org/project/fixture/"
    result = dependency_audit._audit_python(
        "runtime", "fixture @ https://example.invalid/fixture.whl"
    )
    assert result["latest"] is None
    assert "manual upstream review" in result["note"]


@pytest.mark.parametrize(
    "files,expected",
    [
        ([], False),
        ([{"requires_python": ">=3.15"}], False),
        ([{"requires_python": ">=3.14", "yanked": True}], False),
        ([{"requires_python": ">=3.14", "yanked": False}], True),
    ],
)
def test_python_support_uses_non_yanked_latest_release_files(
    files: list[dict[str, Any]], expected: bool
) -> None:
    assert dependency_audit._supports_python_314(files) is expected


@pytest.mark.parametrize("matching,expected", [("1.0", False), (["1.0", "2.0"], True)])
def test_npm_range_compatibility_is_queried_separately(
    monkeypatch: pytest.MonkeyPatch, matching: str | list[str], expected: bool
) -> None:
    responses = iter(["2.0", matching])
    calls: list[tuple[str, ...]] = []

    def run(
        command: tuple[str, ...], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(next(responses))
        )

    monkeypatch.setattr(subprocess, "run", run)
    assert (
        dependency_audit._audit_node("fixture", ">=1")["latest_satisfies"] is expected
    )
    assert calls[-1] == ("npm", "view", "fixture@>=1", "version", "--json")
