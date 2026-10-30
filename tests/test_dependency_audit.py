from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
from test_release_policy import commit, write_project
from test_release_policy import repository as repository

from h2h_dev_tools import dependency_audit, version_policy


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
    monkeypatch.setattr(dependency_audit, "_npm_command", lambda: ("npm",))
    assert (
        dependency_audit._audit_node("fixture", ">=1")["latest_satisfies"] is expected
    )
    assert calls[-1] == ("npm", "view", "fixture@>=1", "version", "--json")


def _use_cp1252_default_text_io(monkeypatch: pytest.MonkeyPatch) -> None:
    read_text, write_text = Path.read_text, Path.write_text

    def read(
        path: Path,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> str:
        return read_text(
            path, encoding=encoding or "cp1252", errors=errors, newline=newline
        )

    def write(
        path: Path,
        data: str,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        return write_text(
            path, data, encoding=encoding or "cp1252", errors=errors, newline=newline
        )

    # Real filesystem I/O under a controlled default codec; explicit encodings
    # still work normally. Assertions inspect bytes and the actual validator,
    # not the implementation's write_text keyword arguments.
    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(Path, "write_text", write)


@pytest.mark.parametrize("note", ["café", "中文審查"])
def test_audit_receipt_is_utf8_independent_of_locale(
    repository: Path, monkeypatch: pytest.MonkeyPatch, note: str
) -> None:
    write_project(repository, "0.1.0")
    with monkeypatch.context() as locale_patch:
        _use_cp1252_default_text_io(locale_patch)
        locale_patch.setattr(sys, "argv", ["audit-dependencies", "--review-note", note])
        assert dependency_audit.main() == 0
    raw = (repository / ".release/dependency-audit.json").read_bytes()
    assert json.loads(raw)["review"]["note"] == note
    assert note.encode("utf-8") in raw
    raw.decode("utf-8")
    tree = commit(repository, "test: retain generated Unicode audit")
    document = tomllib.loads(
        (repository / "pyproject.toml").read_text(encoding="utf-8")
    )
    version_policy._validate_audit(tree, document, "0.1.0")


def test_audit_package_json_uses_utf8(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_project(repository, "0.1.0")
    package = {"description": "丁", "devDependencies": {}}
    (repository / "package.json").write_bytes(
        json.dumps(package, ensure_ascii=False).encode("utf-8")
    )
    with monkeypatch.context() as locale_patch:
        _use_cp1252_default_text_io(locale_patch)
        locale_patch.setattr(
            sys, "argv", ["audit-dependencies", "--review-note", "Checked fixture"]
        )
        assert dependency_audit.main() == 0
    tree = commit(repository, "test: retain UTF-8 package audit")
    document = tomllib.loads(
        (repository / "pyproject.toml").read_text(encoding="utf-8")
    )
    version_policy._validate_audit(tree, document, "0.1.0")
