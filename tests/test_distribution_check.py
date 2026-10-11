from __future__ import annotations

import importlib
import sys
from importlib.metadata import Distribution
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from h2h_dev_tools import distribution_check


def test_requirements_honor_inactive_markers_and_explicit_prerelease_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "METADATA").write_text(
        'Name: fixture\nVersion: 1.0.0\nRequires-Dist: absent; extra == "dev"\n'
        "Requires-Dist: active>=1.0\n"
    )
    package = Distribution.at(tmp_path)
    monkeypatch.setattr(
        distribution_check,
        "distribution",
        lambda _name: SimpleNamespace(version="1.1rc1"),
    )
    assert distribution_check.check_runtime_requirements(package, prereleases=True) == {
        "active": "1.1rc1"
    }
    with pytest.raises(RuntimeError, match="does not satisfy"):
        distribution_check.check_runtime_requirements(package, prereleases=False)


@pytest.mark.parametrize(
    "failure", [None, "record", "editable", "shadow", "outside-prefix"]
)
def test_origin_checks_metadata_record_and_import_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    prefix = tmp_path / "smoke"
    prefix.mkdir()
    module = prefix / "fixture/__init__.py"
    module.parent.mkdir()
    module.write_text("")
    package = SimpleNamespace(
        locate_file=lambda name: prefix / name,
        files=[] if failure == "record" else [Path("fixture/__init__.py")],
        read_text=lambda _name: (
            '{"dir_info":{"editable":true}}' if failure == "editable" else None
        ),
    )
    monkeypatch.setattr(
        distribution_check, "distribution", lambda _name: cast(Distribution, package)
    )
    origin = tmp_path / "shadow.py" if failure == "shadow" else module
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda _name: SimpleNamespace(__file__=str(origin)),
    )
    monkeypatch.setattr(
        sys,
        "prefix",
        str(tmp_path / "other" if failure == "outside-prefix" else prefix),
    )
    if failure:
        with pytest.raises(RuntimeError):
            distribution_check.check_wheel_origin("fixture", "fixture", local=True)
    else:
        assert distribution_check.check_wheel_origin(
            "fixture", "fixture", local=True
        ) == str(module)
