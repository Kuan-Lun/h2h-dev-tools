from __future__ import annotations

import os
import re

import pytest


@pytest.fixture(autouse=True)
def isolated_git_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disposable repositories own their config; keep Git-local authority intact."""
    for name in tuple(os.environ):
        if name in {
            "GIT_CONFIG",
            "GIT_CONFIG_PARAMETERS",
            "GIT_CONFIG_COUNT",
        } or re.fullmatch(r"GIT_CONFIG_(?:KEY|VALUE)_\d+", name):
            monkeypatch.delenv(name)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
