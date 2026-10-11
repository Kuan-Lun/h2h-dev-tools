"""Admission and identity tests without launching any managed command."""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import pytest

from h2h_dev_tools import _process_scope as scope_module

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX admission ledger")


@pytest.fixture(autouse=True)
def isolated_scope(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(scope_module.ENVIRONMENT_KEY, raising=False)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))


def _close_unlaunched(scope: scope_module.PosixScope) -> None:
    # These unit scopes never launch a process. Real lifecycle proof is tested
    # separately using run_command and actual POSIX groups.
    scope.proven_empty = True
    scope.close(deadline=time.monotonic() + 1)


def test_obligations_are_independent_and_admission_closes_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = scope_module.PosixScope.create(deadline=time.monotonic() + 1)
    monkeypatch.setenv(scope_module.ENVIRONMENT_KEY, parent.capability.encode())
    first = scope_module.PosixScope.create(deadline=time.monotonic() + 1)
    second = scope_module.PosixScope.create(deadline=time.monotonic() + 1)
    parent.close_admission(deadline=time.monotonic() + 1)
    with pytest.raises(scope_module.ScopeError, match="closed admission"):
        scope_module.PosixScope.create(deadline=time.monotonic() + 1)
    assert parent.has_pending(deadline=time.monotonic() + 1)
    with pytest.raises(scope_module.ScopeError, match="Unproven"):
        first.close(deadline=time.monotonic() + 1)
    _close_unlaunched(first)
    assert parent.has_pending(deadline=time.monotonic() + 1)
    _close_unlaunched(second)
    assert not parent.has_pending(deadline=time.monotonic() + 1)
    _close_unlaunched(parent)


@pytest.mark.parametrize(
    "value", ["", "null", "[]", "{}", '{"directory":"relative","token":"a"}']
)
def test_malformed_inherited_scope_never_becomes_standalone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str
) -> None:
    monkeypatch.setenv(scope_module.ENVIRONMENT_KEY, value)
    with pytest.raises(scope_module.ScopeError, match="Invalid inherited"):
        scope_module.PosixScope.create(deadline=time.monotonic() + 1)
    assert not list(tmp_path.iterdir())


def test_closed_or_stale_inherited_scope_cannot_start_new_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = scope_module.PosixScope.create(deadline=time.monotonic() + 1)
    monkeypatch.setenv(scope_module.ENVIRONMENT_KEY, parent.capability.encode())
    _close_unlaunched(parent)
    with pytest.raises(scope_module.ScopeError, match="unavailable"):
        scope_module.PosixScope.create(deadline=time.monotonic() + 1)


def test_scope_and_obligation_identity_mismatch_retains_responsibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = scope_module.PosixScope.create(deadline=time.monotonic() + 1)
    monkeypatch.setenv(scope_module.ENVIRONMENT_KEY, parent.capability.encode())
    token_file = parent.capability.directory / "token"
    token_file.write_text("0" * 64)
    with pytest.raises(scope_module.ScopeError, match="identity changed"):
        scope_module.PosixScope.create(deadline=time.monotonic() + 1)
    token_file.write_text(parent.capability.token)
    child = scope_module.PosixScope.create(deadline=time.monotonic() + 1)
    obligation = parent.capability.directory / "pending" / child.capability.token
    original = obligation.read_text()
    obligation.write_text("another child")
    with pytest.raises(scope_module.ScopeError, match="identity changed"):
        _close_unlaunched(child)
    assert parent.has_pending(deadline=time.monotonic() + 1)
    obligation.write_text(original)
    _close_unlaunched(child)
    _close_unlaunched(parent)


def test_scope_lock_contention_obeys_cleanup_deadline() -> None:
    import fcntl

    scope = scope_module.PosixScope.create(deadline=time.monotonic() + 1)
    with (scope.capability.directory / "lock").open("rb") as competing:
        fcntl.flock(competing.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = time.monotonic()
        with pytest.raises(scope_module.ScopeError, match="lock deadline"):
            scope.close_admission(deadline=started + 0.04)
        assert time.monotonic() - started < 0.5
    _close_unlaunched(scope)


def test_child_environment_cannot_replace_its_new_scope() -> None:
    scope = scope_module.PosixScope.create(deadline=time.monotonic() + 1)
    original = {scope_module.ENVIRONMENT_KEY: "stale", "KEEP": "present"}
    assert scope.environment(original) == {
        scope_module.ENVIRONMENT_KEY: scope.capability.encode(),
        "KEEP": "present",
    }
    assert original[scope_module.ENVIRONMENT_KEY] == "stale"
    _close_unlaunched(scope)
