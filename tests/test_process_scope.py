"""Admission and identity tests without launching any managed command."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

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


def test_closed_parent_rejects_before_allocating_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = scope_module.PosixScope.create(deadline=time.monotonic() + 2)
    monkeypatch.setenv(scope_module.ENVIRONMENT_KEY, parent.capability.encode())
    parent.close_admission(deadline=time.monotonic() + 2)
    allocations: list[bool] = []
    real_allocate = tempfile.mkdtemp

    def allocate(*args: object, **kwargs: object) -> str:
        allocations.append(True)
        # The closed path must never allocate. Raising also avoids creating test
        # residue when the old ordering calls this unexpectedly.
        raise AssertionError("Closed admission allocated a scope")

    try:
        with monkeypatch.context() as boundary:
            boundary.setattr(tempfile, "mkdtemp", allocate)
            with pytest.raises(scope_module.ScopeAdmissionClosed):
                scope_module.PosixScope.create(deadline=time.monotonic() + 2)
        assert not allocations
    finally:
        monkeypatch.setattr(tempfile, "mkdtemp", real_allocate)
        _close_unlaunched(parent)


@pytest.mark.parametrize("rollback_fails", [False, True])
def test_initialization_failure_owns_metadata_until_verified_rollback(
    monkeypatch: pytest.MonkeyPatch, rollback_fails: bool
) -> None:
    parent = scope_module.PosixScope.create(deadline=time.monotonic() + 2)
    monkeypatch.setenv(scope_module.ENVIRONMENT_KEY, parent.capability.encode())
    real_mkdir, real_unlink = Path.mkdir, Path.unlink
    created: list[Path] = []
    failure = OSError("Local initialization failed")

    def failed_mkdir(path: Path, *args: object, **kwargs: object) -> None:
        if path.name == "pending" and path.parent != parent.capability.directory:
            created.append(path.parent)
            # A completed responsibility must exist even before initialization.
            assert len(tuple((parent.capability.directory / "pending").iterdir())) == 1
            raise failure
        raise AssertionError(f"Unexpected mkdir in initialization probe: {path}")

    def failed_unlink(path: Path, *, missing_ok: bool = False) -> None:
        if rollback_fails and path.name == "token" and path.parent in created:
            raise OSError("Local rollback failed")
        real_unlink(path, missing_ok=missing_ok)

    try:
        with monkeypatch.context() as boundary:
            boundary.setattr(Path, "mkdir", failed_mkdir)
            boundary.setattr(Path, "unlink", failed_unlink)
            with pytest.raises(scope_module.ScopeError) as raised:
                scope_module.PosixScope.create(deadline=time.monotonic() + 2)
        assert raised.value.__cause__ is failure
        assert len(created) == 1
        assert created[0].exists() is rollback_fails
        assert parent.has_pending(deadline=time.monotonic() + 2) is rollback_fails
    finally:
        # No Popen exists in this test. Recover only these recorded synthetic
        # allocation/registration facts after checking the failure evidence.
        monkeypatch.setattr(Path, "mkdir", real_mkdir)
        monkeypatch.setattr(Path, "unlink", real_unlink)
        for directory in created:
            for name in ("lock", "token"):
                (directory / name).unlink(missing_ok=True)
            if directory.exists():
                directory.rmdir()
        for obligation in (parent.capability.directory / "pending").iterdir():
            obligation.unlink()
        _close_unlaunched(parent)


def test_local_scope_removal_failure_retains_parent_responsibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = scope_module.PosixScope.create(deadline=time.monotonic() + 2)
    monkeypatch.setenv(scope_module.ENVIRONMENT_KEY, parent.capability.encode())
    child = scope_module.PosixScope.create(deadline=time.monotonic() + 2)
    real_unlink = Path.unlink
    failure = OSError("Scope token removal failed")

    def failed_unlink(path: Path, *, missing_ok: bool = False) -> None:
        if path == child.capability.directory / "token":
            raise failure
        real_unlink(path, missing_ok=missing_ok)

    try:
        with monkeypatch.context() as boundary:
            boundary.setattr(Path, "unlink", failed_unlink)
            with pytest.raises(scope_module.ScopeError) as raised:
                _close_unlaunched(child)
        assert raised.value.__cause__ is failure
        assert parent.has_pending(deadline=time.monotonic() + 2)
        assert child.capability.directory.exists()
    finally:
        _close_unlaunched(child)
        _close_unlaunched(parent)


def test_partial_registration_failure_is_not_erased(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    parent = scope_module.PosixScope.create(deadline=time.monotonic() + 2)
    monkeypatch.setenv(scope_module.ENVIRONMENT_KEY, parent.capability.encode())
    real_open = Path.open
    partial_entries: list[Path] = []
    failure = OSError("Uncertain partial registration")

    class PartialWriter:
        def __init__(self, destination: Any) -> None:
            self.destination = destination

        def write(self, value: str) -> None:
            self.destination.write(value[:12])
            self.destination.flush()
            raise failure

    @contextmanager
    def partial_open(path: Path, *args: Any, **kwargs: Any) -> Iterator[Any]:
        with real_open(path, *args, **kwargs) as destination:
            if path.parent == parent.capability.directory / "pending":
                partial_entries.append(path)
                yield PartialWriter(destination)
            else:
                yield destination

    try:
        with monkeypatch.context() as boundary:
            boundary.setattr(Path, "open", partial_open)
            with pytest.raises(scope_module.ScopeError) as raised:
                scope_module.PosixScope.create(deadline=time.monotonic() + 2)
        assert raised.value.__cause__ is failure
        assert len(partial_entries) == 1
        assert partial_entries[0].read_text() == '{"directory"'
        assert parent.has_pending(deadline=time.monotonic() + 2)
        assert list(tmp_path.glob("h2h-process-scope-*")) == [
            parent.capability.directory
        ]
    finally:
        # No child is ever spawned. Preserve the runtime error above; discard
        # only the partial entry deliberately written by this isolated fixture.
        for entry in partial_entries:
            entry.unlink()
        _close_unlaunched(parent)


_QUEUED_ADMISSION = r"""
import json, os, runpy, sys, tempfile, time
from pathlib import Path
package, parent, state = map(Path, sys.argv[1:4])
runpy.run_path(str(package / 'source_loader.py'))['load_package'](package, 'h2h_dev_tools')
from h2h_dev_tools._process_scope import PosixScope, ScopeError
real_open, real_allocate = Path.open, tempfile.mkdtemp
allocated = False

def observe_open(path, *args, **kwargs):
    result = real_open(path, *args, **kwargs)
    if path == parent / 'lock':
        (state / 'queued-opened').touch()
    return result

def observe_allocate(*args, **kwargs):
    global allocated
    allocated = True
    return real_allocate(*args, **kwargs)

Path.open, tempfile.mkdtemp = observe_open, observe_allocate
created = False
try:
    scope = PosixScope.create(deadline=time.monotonic() + 3)
except ScopeError:
    pass
else:
    created = True
    scope.proven_empty = True
    scope.close(deadline=time.monotonic() + 2)
(state / 'queued-result.json').write_text(json.dumps(dict(created=created, allocated=allocated)))
"""


def test_queued_admission_on_removed_scope_rejects_before_allocation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    parent = scope_module.PosixScope.create(deadline=time.monotonic() + 2)
    real_unlink = Path.unlink
    children: list[subprocess.Popen[bytes]] = []
    deadline = time.monotonic() + 5

    def observe_unlink(path: Path, *, missing_ok: bool = False) -> None:
        real_unlink(path, missing_ok=missing_ok)
        if path == parent.capability.directory / "closed":
            child = subprocess.Popen(
                (
                    sys.executable,
                    "-I",
                    "-B",
                    "-c",
                    _QUEUED_ADMISSION,
                    str(Path(scope_module.__file__).resolve().parent),
                    str(parent.capability.directory),
                    str(tmp_path),
                ),
                env=os.environ
                | {scope_module.ENVIRONMENT_KEY: parent.capability.encode()},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            children.append(child)
            while not (tmp_path / "queued-opened").exists():
                assert child.poll() is None
                assert time.monotonic() < deadline - 1
                time.sleep(0.001)

    try:
        with monkeypatch.context() as boundary:
            boundary.setattr(Path, "unlink", observe_unlink)
            parent.proven_empty = True
            parent.close(deadline=deadline)
        assert len(children) == 1
        _, stderr = children[0].communicate(
            timeout=max(0.01, deadline - time.monotonic())
        )
        assert children[0].returncode == 0, stderr
        assert json.loads((tmp_path / "queued-result.json").read_text()) == {
            "created": False,
            "allocated": False,
        }
        assert not list(tmp_path.glob("h2h-process-scope-*"))
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=max(0.01, deadline - time.monotonic()))
