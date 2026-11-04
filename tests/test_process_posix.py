"""Native identity proofs use the original group, independent of numeric reuse."""

from __future__ import annotations

import ctypes
import errno
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY, Mock, call

import pytest

from h2h_dev_tools import _process_posix as native
from h2h_dev_tools import process

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX native ownership")


def _child(*, pidfd: int | None = None) -> native.PinnedProcess:
    child = object.__new__(native.PinnedProcess)
    child.pid = 4312
    child.args = ("owned-test",)
    child.returncode = None
    child._identity_lock = threading.RLock()
    child._blocking_observers = set()
    child._identity_retired = False
    child._leader_reaped = False
    child._pidfd = pidfd
    child._pidfd_acquisition_failed = False
    child._close_error = None
    return child


@pytest.fixture
def exited(monkeypatch: pytest.MonkeyPatch) -> tuple[Mock, Mock]:
    waitid = Mock(
        return_value=SimpleNamespace(si_pid=4312, si_code=os.CLD_EXITED, si_status=7)
    )
    waitpid = Mock(return_value=(4312, 7 << 8))
    monkeypatch.setattr(os, "waitid", waitid)
    monkeypatch.setattr(os, "waitpid", waitpid)
    monkeypatch.setattr(time, "monotonic", Mock(return_value=100.0))
    return waitid, waitpid


@pytest.mark.parametrize("members", [(4312,), (4312, 4313)])
def test_darwin_uses_one_kernel_locked_two_slot_inventory(
    monkeypatch: pytest.MonkeyPatch, members: tuple[int, ...]
) -> None:
    def query(kind: int, group: int, target: Any, size: int) -> int:
        assert (kind, group, size) == (2, 4312, 8)
        for index, pid in enumerate(members):
            target[index] = pid
        return 4 * len(members)

    library = Mock()
    library.proc_listpids.side_effect = query
    monkeypatch.setattr(ctypes, "CDLL", Mock(return_value=library))
    assert native._darwin_group_members(4312) == members
    library.proc_listpids.assert_called_once()


@pytest.mark.parametrize("state_name", ["CLD_STOPPED", "CLD_CONTINUED", "CLD_TRAPPED"])
def test_nonterminal_receipt_retains_identity_until_actual_exit(
    monkeypatch: pytest.MonkeyPatch, state_name: str
) -> None:
    child = _child()
    receipt = SimpleNamespace(
        si_pid=child.pid, si_code=getattr(os, state_name), si_status=signal.SIGSTOP
    )
    query = Mock(return_value=receipt)
    reap = Mock(side_effect=AssertionError("live leader must not be reaped"))
    inventory = Mock(side_effect=AssertionError("live leader cannot retire"))
    monkeypatch.setattr(os, "waitid", query)
    monkeypatch.setattr(os, "waitpid", reap)
    monkeypatch.setattr(native, "_darwin_group_members", inventory)
    assert child.poll() is None
    assert not child.retire_clean_group(deadline=time.monotonic() + 1)
    send = Mock()
    child.signal_group(signal.SIGTERM, send=send)
    send.assert_called_once_with(child.pid, signal.SIGTERM)
    query.return_value = SimpleNamespace(
        si_pid=child.pid, si_code=os.CLD_KILLED, si_status=signal.SIGTERM
    )
    assert child.poll() == -signal.SIGTERM
    query.return_value = receipt
    with pytest.raises(native.IdentityError, match="exit receipt disappeared"):
        child.poll()
    reap.assert_not_called()
    inventory.assert_not_called()


@pytest.mark.parametrize(
    "size,members", [(0, ()), (-1, ()), (3, ()), (12, ()), (4, (0,)), (8, (4312, 4312))]
)
def test_darwin_rejects_missing_malformed_or_duplicate_inventory(
    monkeypatch: pytest.MonkeyPatch, size: int, members: tuple[int, ...]
) -> None:
    def query(kind: int, group: int, target: Any, capacity: int) -> int:
        for index, pid in enumerate(members):
            target[index] = pid
        return size

    library = Mock()
    library.proc_listpids.side_effect = query
    monkeypatch.setattr(ctypes, "CDLL", Mock(return_value=library))
    with pytest.raises(native.IdentityError):
        native._darwin_group_members(4312)


def test_darwin_pins_leader_until_every_other_member_is_gone(
    monkeypatch: pytest.MonkeyPatch, exited: tuple[Mock, Mock]
) -> None:
    child = _child()
    inventory = Mock(side_effect=[(4313, 4312), (4312,)])
    monkeypatch.setattr(native, "_darwin_group_members", inventory)
    assert child.wait(timeout=1) == 7
    assert not child.retire_clean_group(deadline=110)
    exited[1].assert_not_called()
    assert child.retire_clean_group(deadline=110)
    exited[1].assert_called_once_with(4312, os.WNOHANG)
    stale_signal = Mock(side_effect=AssertionError("reused PGID"))
    child.signal_group(signal.SIGKILL, send=stale_signal)
    assert child.retire_clean_group(deadline=110)
    child.close_identity()
    stale_signal.assert_not_called()
    assert inventory.call_count == 2


def test_linux_keeps_kernel_group_authority_after_reaping_leader(
    monkeypatch: pytest.MonkeyPatch, exited: tuple[Mock, Mock]
) -> None:
    child = _child(pidfd=88)
    native_signal = Mock(
        side_effect=[None, None, ProcessLookupError(errno.ESRCH, "empty")]
    )
    close = Mock()
    monkeypatch.setattr(native, "_pidfd_signal", native_signal)
    monkeypatch.setattr(os, "close", close)
    assert not child.retire_clean_group(deadline=110)
    exited[1].assert_called_once()
    exited[0].reset_mock()
    numeric_signal = Mock(side_effect=AssertionError("numeric PGID was reused"))
    child.signal_group(signal.SIGTERM, send=numeric_signal)
    assert child.poll() == 7
    assert child.retire_clean_group(deadline=110)
    exited[0].assert_not_called()
    assert native_signal.call_args_list == [
        call(88, 0),
        call(88, signal.SIGTERM),
        call(88, 0),
    ]
    close.assert_not_called()
    child.close_identity()
    child.close_identity()
    close.assert_called_once_with(88)
    child.signal_group(signal.SIGKILL, send=numeric_signal)
    numeric_signal.assert_not_called()


def test_linux_permission_denial_does_not_prove_group_absence(
    monkeypatch: pytest.MonkeyPatch, exited: tuple[Mock, Mock]
) -> None:
    child = _child(pidfd=88)
    monkeypatch.setattr(
        native,
        "_pidfd_signal",
        Mock(side_effect=PermissionError(errno.EPERM, "hidden")),
    )
    with pytest.raises(native.IdentityError, match="absence is unproven"):
        child.retire_clean_group(deadline=110)
    assert not child._identity_retired
    assert child._pidfd == 88
    with pytest.raises(native.IdentityError, match="unproven group"):
        child.close_identity()


def test_failed_pidfd_close_is_latched_and_never_retries_reused_fd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = _child(pidfd=88)
    child._identity_retired = True
    close = Mock(side_effect=OSError(errno.EIO, "unknown close result"))
    monkeypatch.setattr(os, "close", close)
    for _ in range(2):
        with pytest.raises(native.IdentityError, match="closure is unproven"):
            child.close_identity()
    close.assert_called_once_with(88)


@pytest.mark.parametrize(
    "error",
    [
        None,
        ProcessLookupError(errno.ESRCH, "not a leader"),
        OSError(errno.EINVAL, "old kernel"),
        OSError(errno.ENOSYS, "unsupported syscall"),
    ],
)
def test_linux_capability_probe_is_read_only_and_closes_handle(
    monkeypatch: pytest.MonkeyPatch, error: OSError | None
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    opener = Mock(return_value=88)
    sender = Mock(side_effect=error)
    close = Mock()
    monkeypatch.setattr(native, "_pidfd_open", opener)
    monkeypatch.setattr(native, "_pidfd_signal", sender)
    monkeypatch.setattr(os, "close", close)
    if error is not None and not isinstance(error, ProcessLookupError):
        with pytest.raises(ValueError, match=r"Linux 6\.9\+.*process-group") as caught:
            native._preflight_native_identity()
        assert caught.value.__cause__ is error
    else:
        native._preflight_native_identity()
    opener.assert_called_once_with(os.getpid())
    sender.assert_called_once_with(88, 0)
    close.assert_called_once_with(88)


@pytest.mark.parametrize("operation", ["open", "signal"])
@pytest.mark.parametrize("error_number", [errno.EINVAL, errno.ENOSYS])
def test_linux_capability_failure_rolls_back_scope_before_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, operation: str, error_number: int
) -> None:
    from h2h_dev_tools._process_scope import PosixScope

    kernel_error = OSError(error_number, "capability unavailable")
    scope = Mock(proven_empty=False)
    spawn = Mock(side_effect=AssertionError("Unsupported target must not start"))
    close = Mock()
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(PosixScope, "create", Mock(return_value=scope))
    monkeypatch.setattr(subprocess.Popen, "__init__", spawn)
    monkeypatch.setattr(
        native,
        "_pidfd_open",
        Mock(
            return_value=88, side_effect=kernel_error if operation == "open" else None
        ),
    )
    monkeypatch.setattr(
        native,
        "_pidfd_signal",
        Mock(side_effect=kernel_error if operation == "signal" else None),
    )
    monkeypatch.setattr(os, "close", close)
    command = process.ProcessCommand("unsupported", ("test",), tmp_path)
    with pytest.raises(process.ProcessOwnershipError) as caught:
        process.start_owned_process(command, deadline=110, signal_controller=None)
    cause = caught.value.__cause__
    assert isinstance(cause, ValueError)
    assert "Linux 6.9+ pidfd process-group" in str(cause)
    assert cause.__cause__ is kernel_error
    assert scope.proven_empty
    scope.close.assert_called_once_with(deadline=110)
    spawn.assert_not_called()
    assert close.call_args_list == ([call(88)] if operation == "signal" else [])


def test_darwin_missing_proc_export_rolls_back_scope_before_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from h2h_dev_tools._process_scope import PosixScope

    scope = Mock(proven_empty=False)
    spawn = Mock(side_effect=AssertionError("Unsupported target must not start"))
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(PosixScope, "create", Mock(return_value=scope))
    monkeypatch.setattr(subprocess.Popen, "__init__", spawn)
    monkeypatch.setattr(ctypes, "CDLL", Mock(return_value=SimpleNamespace()))
    command = process.ProcessCommand("unsupported", ("test",), tmp_path)
    with pytest.raises(process.ProcessOwnershipError) as caught:
        process.start_owned_process(command, deadline=110, signal_controller=None)
    cause = caught.value.__cause__
    assert isinstance(cause, ValueError)
    assert "macOS ownership requires native libproc" in str(cause)
    assert isinstance(cause.__cause__, native.IdentityError)
    assert isinstance(cause.__cause__.__cause__, AttributeError)
    assert scope.proven_empty
    scope.close.assert_called_once_with(deadline=110)
    spawn.assert_not_called()


def test_unsupported_native_platform_rejects_before_target_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "unsupported-posix")
    spawn = Mock(side_effect=AssertionError("target must not start"))
    monkeypatch.setattr(subprocess.Popen, "__init__", spawn)
    with pytest.raises(ValueError, match="macOS or Linux"):
        native.PinnedProcess(
            ("test",),
            cwd=tmp_path,
            env=None,
            stdout=None,
            stderr=None,
            start_new_session=True,
        )
    spawn.assert_not_called()


def test_post_spawn_identity_failure_retains_scope_instead_of_startup_rollback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from h2h_dev_tools._process_scope import PosixScope

    scope = Mock(proven_empty=False)
    monkeypatch.setattr(PosixScope, "create", Mock(return_value=scope))
    monkeypatch.setattr(
        process,
        "_start_posix_process",
        Mock(side_effect=native.IdentityError("post-spawn acquisition failed")),
    )
    command = process.ProcessCommand("owned", ("test",), tmp_path)
    with pytest.raises(process.ProcessOwnershipError) as caught:
        process.start_owned_process(command, deadline=110, signal_controller=None)
    assert not caught.value.cleanup_proven
    assert scope.proven_empty is False
    scope.close.assert_not_called()


def test_foreign_reap_cannot_send_signals_to_reused_numeric_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = _child()
    monkeypatch.setattr(os, "waitid", Mock(side_effect=ChildProcessError()))
    send = Mock()
    with pytest.raises(native.IdentityError, match="outside its owner"):
        child.signal_group(signal.SIGKILL, send=send)
    send.assert_not_called()


def test_post_spawn_pidfd_acquisition_failure_defers_signals_to_scope_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def spawned(self: native.PinnedProcess, *args: object, **kwargs: object) -> None:
        self.pid = 4312
        self.args = ("test",)
        self.returncode = None

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(native, "_preflight_native_identity", Mock())
    monkeypatch.setattr(subprocess.Popen, "__init__", spawned)
    acquisition_error = OSError(errno.EMFILE, "fd limit")
    monkeypatch.setattr(native, "_pidfd_open", Mock(side_effect=acquisition_error))
    waitid = Mock(return_value=None)
    waitpid = Mock(side_effect=AssertionError("lost group pin"))
    killpg = Mock()
    monkeypatch.setattr(os, "waitid", waitid)
    monkeypatch.setattr(os, "waitpid", waitpid)
    monkeypatch.setattr(os, "killpg", killpg)
    with pytest.raises(
        native.AcquisitionError, match=r"4312.*cleanup is unproven"
    ) as caught:
        native.PinnedProcess(
            ("test",),
            cwd=tmp_path,
            env=None,
            stdout=None,
            stderr=None,
            start_new_session=True,
        )
    assert caught.value.__cause__ is acquisition_error
    assert caught.value.process.pid == 4312
    assert caught.value.process._pidfd_acquisition_failed
    assert not caught.value.process._leader_reaped
    killpg.assert_not_called()
    waitid.assert_not_called()
    waitpid.assert_not_called()


def test_failed_linux_acquisition_cannot_use_darwin_inventory_or_reap(
    monkeypatch: pytest.MonkeyPatch, exited: tuple[Mock, Mock]
) -> None:
    child = _child()
    child._pidfd_acquisition_failed = True
    inventory = Mock(side_effect=AssertionError("Linux has no libproc proof"))
    monkeypatch.setattr(native, "_darwin_group_members", inventory)
    assert child.wait(timeout=1) == 7
    assert not child.retire_clean_group(deadline=110)
    assert not child.retire_clean_group(deadline=90)
    assert not child._identity_retired
    assert not child._leader_reaped
    inventory.assert_not_called()
    exited[1].assert_not_called()
    with pytest.raises(native.IdentityError, match="unproven group"):
        child.close_identity()


@pytest.mark.parametrize("nested_completes", [False, True])
def test_acquisition_failure_obeys_nested_cleanup_before_sigkill(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, nested_completes: bool
) -> None:
    from h2h_dev_tools._process_scope import PosixScope

    child = _child()
    child._pidfd_acquisition_failed = True
    clock = 100.0
    deadline = 106.0
    closed = False
    signals: list[int] = []
    scope = Mock(
        proven_empty=False, capability=SimpleNamespace(directory=tmp_path / "scope")
    )

    def close_admission(*, deadline: float) -> None:
        nonlocal closed
        assert deadline == 106.0
        closed = True

    def pending(*, deadline: float) -> bool:
        assert deadline == 106.0
        assert closed
        return not nested_completes or clock < 103.0

    def send(group: int, number: int) -> None:
        assert group == child.pid
        assert closed, "Native acquisition failure signalled before admission closed"
        if number == signal.SIGKILL:
            assert not pending(deadline=deadline), "Nested owner killed before cleanup"
        signals.append(number)

    def sleep(seconds: float) -> None:
        nonlocal clock
        clock += seconds

    acquisition_error = native.AcquisitionError(child)
    kernel_error = OSError(errno.EMFILE, "fd limit")
    acquisition_error.__cause__ = kernel_error
    scope.close_admission.side_effect = close_admission
    scope.has_pending.side_effect = pending
    monkeypatch.setattr(PosixScope, "create", Mock(return_value=scope))
    monkeypatch.setattr(
        process, "_start_posix_process", Mock(side_effect=acquisition_error)
    )
    monkeypatch.setattr(time, "monotonic", lambda: clock)
    monkeypatch.setattr(time, "sleep", sleep)
    monkeypatch.setattr(os, "killpg", send)
    monkeypatch.setattr(
        os,
        "waitid",
        Mock(
            return_value=SimpleNamespace(
                si_pid=4312, si_code=os.CLD_EXITED, si_status=0
            )
        ),
    )
    reap = Mock(side_effect=AssertionError("The only retained identity was reaped"))
    monkeypatch.setattr(os, "waitpid", reap)
    command = process.ProcessCommand("owned", ("test",), tmp_path)
    with pytest.raises(process.ProcessOwnershipError) as caught:
        process.start_owned_process(command, deadline=deadline, signal_controller=None)
    failure = caught.value
    assert failure.__cause__ is acquisition_error
    assert acquisition_error.__cause__ is kernel_error
    assert failure.owner is not None
    assert failure.owner.process is child
    assert failure.owner.posix_scope is scope
    assert not failure.cleanup_proven
    assert not scope.proven_empty
    assert clock == deadline
    assert signals == [signal.SIGTERM, *([signal.SIGKILL] if nested_completes else [])]
    scope.close_admission.assert_called_once_with(deadline=deadline)
    scope.close.assert_not_called()
    reap.assert_not_called()


@pytest.mark.parametrize(
    "cleanup_error",
    [
        OSError(errno.EIO, "ledger unavailable"),
        KeyboardInterrupt(),
        process.RunnerSignalInterrupt(signal.SIGTERM),
    ],
)
def test_acquisition_cleanup_failure_preserves_original_cause_and_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cleanup_error: BaseException
) -> None:
    from h2h_dev_tools._process_scope import PosixScope

    child = _child()
    child._pidfd_acquisition_failed = True
    acquisition_error = native.AcquisitionError(child)
    kernel_error = OSError(errno.EMFILE, "fd limit")
    acquisition_error.__cause__ = kernel_error
    scope = Mock(
        proven_empty=False, capability=SimpleNamespace(directory=tmp_path / "scope")
    )
    cleanup = Mock(side_effect=cleanup_error)
    monkeypatch.setattr(PosixScope, "create", Mock(return_value=scope))
    monkeypatch.setattr(
        process, "_start_posix_process", Mock(side_effect=acquisition_error)
    )
    monkeypatch.setattr(process, "terminate_owned_tree", cleanup)
    command = process.ProcessCommand("owned", ("test",), tmp_path)
    with pytest.raises(process.ProcessOwnershipError) as caught:
        process.start_owned_process(command, deadline=110, signal_controller=None)
    failure = caught.value
    assert failure.__cause__ is acquisition_error
    assert acquisition_error.__cause__ is kernel_error
    assert failure.owner is not None
    assert failure.owner.process is child
    assert failure.owner.posix_scope is scope
    assert not failure.cleanup_proven
    assert not scope.proven_empty
    assert any(repr(cleanup_error) in note for note in failure.__notes__)
    assert any(str(scope.capability.directory) in note for note in failure.__notes__)
    cleanup.assert_called_once_with(failure.owner, deadline=110, diagnostic=ANY)
    scope.close.assert_not_called()


def test_pidfd_wrapper_uses_kernel_process_group_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = Mock()
    monkeypatch.setattr(signal, "pidfd_send_signal", sender, raising=False)
    native._pidfd_signal(88, signal.SIGTERM)
    sender.assert_called_once_with(88, signal.SIGTERM, None, 4)


def test_late_retirement_preserves_proof_without_reusing_numeric_identity(
    monkeypatch: pytest.MonkeyPatch, exited: tuple[Mock, Mock]
) -> None:
    child = _child()
    monkeypatch.setattr(native, "_darwin_group_members", Mock(return_value=(4312,)))
    monkeypatch.setattr(time, "monotonic", Mock(side_effect=[100.0, 100.0, 111.0]))
    assert not child.retire_clean_group(deadline=110)
    assert child._identity_retired
    assert child.retire_clean_group(deadline=110)
    exited[1].assert_called_once()


def test_short_command_wait_does_not_spend_the_whole_poll_quantum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = _child()
    clock = 100.0

    def sleep(seconds: float) -> None:
        nonlocal clock
        clock += seconds

    monkeypatch.setattr(time, "monotonic", lambda: clock)
    monkeypatch.setattr(time, "sleep", sleep)
    monkeypatch.setattr(child, "poll", lambda: 7 if clock >= 100.002 else None)
    assert child.wait(timeout=0.01) == 7
    assert clock < 100.01


def test_wait_backoff_remains_inside_the_original_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = _child()
    clock = 100.0

    def sleep(seconds: float) -> None:
        nonlocal clock
        clock += seconds

    monkeypatch.setattr(time, "monotonic", lambda: clock)
    monkeypatch.setattr(time, "sleep", sleep)
    monkeypatch.setattr(child, "poll", lambda: None)
    with pytest.raises(subprocess.TimeoutExpired):
        child.wait(timeout=0.005)
    assert clock == 100.005


def test_unbounded_wait_uses_blocking_observation_without_reaping_or_poll_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = _child()
    receipt = SimpleNamespace(si_pid=child.pid, si_code=os.CLD_EXITED, si_status=7)
    observe = Mock(side_effect=[None, receipt, receipt])
    sleep = Mock(side_effect=AssertionError("ordinary exits must not poll"))
    reap = Mock(side_effect=AssertionError("wait must retain the leader"))
    monkeypatch.setattr(os, "waitid", observe)
    monkeypatch.setattr(os, "waitpid", reap)
    monkeypatch.setattr(time, "sleep", sleep)
    assert child.wait() == 7
    assert observe.call_args_list == [
        call(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT),
        call(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT),
        call(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT),
    ]
    assert not child._blocking_observers
    reap.assert_not_called()
    sleep.assert_not_called()


@pytest.mark.parametrize("state_name", ["CLD_STOPPED", "CLD_CONTINUED", "CLD_TRAPPED"])
def test_unbounded_nonterminal_wakes_back_off_and_resample_current_exit(
    monkeypatch: pytest.MonkeyPatch, state_name: str
) -> None:
    child = _child()
    live = SimpleNamespace(
        si_pid=child.pid, si_code=getattr(os, state_name), si_status=signal.SIGSTOP
    )
    exited = SimpleNamespace(si_pid=child.pid, si_code=os.CLD_EXITED, si_status=7)
    observe = Mock(side_effect=[None, live, live, live, live, exited])
    sleep = Mock()
    monkeypatch.setattr(os, "waitid", observe)
    monkeypatch.setattr(time, "sleep", sleep)
    assert child.wait() == 7
    sleep.assert_called_once_with(0.05)
    assert not child._blocking_observers


@pytest.mark.parametrize(
    "error_type", [KeyboardInterrupt, InterruptedError, ChildProcessError]
)
def test_interrupted_blocking_wait_releases_only_its_own_observation(
    monkeypatch: pytest.MonkeyPatch, error_type: type[BaseException]
) -> None:
    child = _child()
    other_waiter = object()
    child._blocking_observers.add(other_waiter)
    error = error_type()
    monkeypatch.setattr(os, "waitid", Mock(side_effect=[None, error]))
    expected = (
        native.IdentityError if isinstance(error, ChildProcessError) else error_type
    )
    with pytest.raises(expected) as caught:
        child.wait()
    if isinstance(error, ChildProcessError):
        assert caught.value.__cause__ is error
    else:
        assert caught.value is error
    assert child._blocking_observers == {other_waiter}
    assert child.returncode is None


def test_unbounded_wait_reserves_identity_before_reentrant_poll(
    monkeypatch: pytest.MonkeyPatch, exited: tuple[Mock, Mock]
) -> None:
    child = _child()
    poll = child.poll

    def observe() -> int | None:
        assert child._blocking_observers
        assert not child.retire_clean_group(deadline=110)
        return poll()

    monkeypatch.setattr(child, "poll", observe)
    assert child.wait() == 7
    assert not child._blocking_observers
    exited[1].assert_not_called()


@pytest.mark.parametrize("pidfd", [None, 88])
def test_blocking_waiters_pin_pre_call_race_without_blocking_signals(
    monkeypatch: pytest.MonkeyPatch, pidfd: int | None
) -> None:
    child = _child(pidfd=pidfd)
    exited = threading.Event()
    entered = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    local = threading.local()
    errors: list[BaseException] = []
    results: list[int] = []
    receipt = SimpleNamespace(si_pid=child.pid, si_code=os.CLD_EXITED, si_status=7)

    def observe(kind: int, pid: int, options: int) -> Any:
        assert (kind, pid) == (os.P_PID, child.pid)
        if options & os.WNOHANG:
            return receipt if exited.is_set() else None
        # Pause before the kernel call: even an already-exited leader must not
        # be reaped while either waiter still intends to address its numeric PID.
        index = local.index
        entered[index].set()
        assert release[index].wait(3)
        return receipt

    def wait(index: int) -> None:
        local.index = index
        try:
            results.append(child.wait())
        except BaseException as error:
            errors.append(error)

    def native_signal(descriptor: int, number: int) -> None:
        assert descriptor == pidfd
        if number == 0:
            raise ProcessLookupError(errno.ESRCH, "empty original group")
        assert number == signal.SIGTERM

    numeric_signal = Mock()
    reap = Mock(return_value=(child.pid, 7 << 8))
    monkeypatch.setattr(os, "waitid", observe)
    monkeypatch.setattr(os, "waitpid", reap)
    monkeypatch.setattr(native, "_pidfd_signal", native_signal)
    monkeypatch.setattr(native, "_darwin_group_members", lambda pid: (pid,))
    monkeypatch.setattr(os, "close", Mock())
    waiters = [threading.Thread(target=wait, args=(index,)) for index in range(2)]
    for waiter in waiters:
        waiter.start()

    signalled = threading.Event()

    def signal_and_retire() -> None:
        try:
            child.signal_group(signal.SIGTERM, send=numeric_signal)
            assert not child.retire_clean_group(deadline=time.monotonic() + 2)
            signalled.set()
        except BaseException as error:
            errors.append(error)

    sender = threading.Thread(target=signal_and_retire)
    try:
        assert all(event.wait(2) for event in entered)
        exited.set()
        sender.start()
        assert signalled.wait(2)
        reap.assert_not_called()
        release[0].set()
        waiters[0].join(2)
        assert not waiters[0].is_alive()
        assert len(child._blocking_observers) == 1
        assert not child.retire_clean_group(deadline=time.monotonic() + 2)
        reap.assert_not_called()
    finally:
        for event in release:
            event.set()
        for waiter in waiters:
            waiter.join(3)
        if sender.ident is not None:
            sender.join(3)
    assert not errors
    assert results == [7, 7]
    assert not child._blocking_observers
    assert child.retire_clean_group(deadline=time.monotonic() + 2)
    reap.assert_called_once_with(child.pid, os.WNOHANG)
    child.close_identity()
    if pidfd is None:
        numeric_signal.assert_called_once_with(child.pid, signal.SIGTERM)
    else:
        numeric_signal.assert_not_called()
