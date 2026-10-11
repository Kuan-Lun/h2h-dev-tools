from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from typing import Any

import pytest

from h2h_dev_tools.dependency_manifest import dependency_manifest


def test_manifest_retains_v1_canonical_contract() -> None:
    document: dict[str, Any] = {
        "build-system": {"requires": ["hatchling>=1"]},
        "dependency-groups": {"檢查": ["ruff>=1"]},
        "project": {
            "version": "0.1.0",
            "dependencies": ["runtime>=2"],
            "optional-dependencies": {"dev": ["pytest>=8"]},
        },
    }
    package = {"devDependencies": {"markdownlint-cli2": ">=1"}}
    expected = (
        '{"build":["hatchling>=1"],"dependency-groups":{"檢查":["ruff>=1"]},'
        '"node-dev":{"markdownlint-cli2":">=1"},"optional":{"dev":["pytest>=8"]},'
        '"runtime":["runtime>=2"]}'
    ).encode()
    assert dependency_manifest(document, package) == expected
    changed = deepcopy(document)
    changed["project"]["version"] = "0.9.0"
    assert dependency_manifest(changed, package) == expected
    assert dependency_manifest(dict(reversed(document.items())), package) == expected


@pytest.mark.parametrize(
    ("document", "package"),
    [
        ({"build-system": {"requires": ["new>=1"]}}, {}),
        ({"project": {"dependencies": ["new>=1"]}}, {}),
        ({"project": {"optional-dependencies": {"dev": ["new>=1"]}}}, {}),
        ({"dependency-groups": {"test": ["new>=1"]}}, {}),
        ({}, {"devDependencies": {"tool": ">=1"}}),
    ],
)
def test_every_dependency_section_changes_manifest_hash(
    document: dict[str, Any], package: dict[str, Any]
) -> None:
    assert (
        sha256(dependency_manifest(document, package)).digest()
        != sha256(dependency_manifest({}, {})).digest()
    )
