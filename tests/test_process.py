"""Shared process ownership: deadlines, signals, start gates and empty-tree proof."""

from __future__ import annotations

import ctypes
import errno
import io
import os
import signal
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast
from unittest.mock import ANY, Mock, call

import pytest

from h2h_dev_tools import _process_diagnostic as diagnostic
from h2h_dev_tools import process as implementation

runner: ModuleType = implementation
COMMAND = implementation.ProcessCommand(
    "regression command", (sys.executable, "-c", "pass"), Path.cwd(), {}
)


@pytest.mark.parametrize("terminated", [False, True])
@pytest.mark.parametrize("closed", [False, True])
@pytest.mark.usefixtures("memory_diagnostics")
def test_scope_probe_error_retains_cause_and_complete_cleanup_proof(
    monkeypatch: pytest.MonkeyPatch, terminated: bool, closed: bool
) -> None:
    from h2h_dev_tools._process_scope import ScopeError

    scope_error = ScopeError("Process scope lock deadline")
    scope = Mock()
    scope.close_admission.side_effect = scope_error
    child = Mock(pid=4312, returncode=0)
    child.wait.return_value = 0
    owner = runner.OwnedProcess(child, posix_scope=scope)
    terminate = Mock(return_value=terminated)
    close = Mock(return_value=closed)
    monkeypatch.setattr(runner, "start_owned_process", Mock(return_value=owner))
    monkeypatch.setattr(runner, "terminate_owned_tree", terminate)
    monkeypatch.setattr(runner, "close_owner", close)
    monkeypatch.setattr(runner.time, "monotonic", Mock(return_value=100.0))
    monkeypatch.setattr(runner.os, "killpg", Mock(), raising=False)

    with pytest.raises(runner.ProcessOwnershipError) as caught:
        runner.run_command(COMMAND, hard_deadline=110.0)

    assert caught.value.__cause__ is scope_error
    assert caught.value.cleanup_proven is (terminated and closed)
    scope.close_admission.assert_called_once_with(deadline=110.0)
    terminate.assert_called_once_with(owner, deadline=110.0, diagnostic=ANY)
    close.assert_called_once_with(owner, deadline=110.0, diagnostic=ANY)


@pytest.mark.parametrize("operation", ["wait", "terminate"])
def test_unverifiable_scope_is_typed_unproven_without_losing_cause(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    from h2h_dev_tools._process_scope import ScopeError

    scope_error = ScopeError("Process scope identity changed")
    scope = Mock(proven_empty=False)
    scope.close_admission.side_effect = scope_error
    owner = runner.OwnedProcess(Mock(pid=4312), posix_scope=scope)
    monkeypatch.setattr(runner.os, "killpg", Mock(), raising=False)
    with pytest.raises(runner.ProcessOwnershipError) as caught:
        if operation == "wait":
            runner.wait_for_owned_tree_exit(owner, deadline=110.0, poll_deadline=100.25)
        else:
            runner.terminate_owned_tree(owner, deadline=110.0)
    assert caught.value.__cause__ is scope_error
    assert caught.value.cleanup_proven is False
    assert scope.proven_empty is False
    scope.close_admission.assert_called_once_with(deadline=110.0)


@pytest.mark.parametrize("poll_deadline", [100.25, 120.0])
def test_short_group_observation_never_replaces_or_extends_cleanup_deadline(
    monkeypatch: pytest.MonkeyPatch, poll_deadline: float
) -> None:
    clock = 100.0

    def sleep(seconds: float) -> None:
        nonlocal clock
        clock += seconds

    child = Mock(pid=4312)
    scope = Mock()
    owner = runner.OwnedProcess(child, posix_scope=scope)
    monkeypatch.setattr(runner.os, "killpg", Mock(), raising=False)
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock)
    monkeypatch.setattr(runner.time, "sleep", sleep)
    assert not runner.wait_for_owned_tree_exit(
        owner, deadline=110.0, poll_deadline=poll_deadline
    )
    assert clock == min(110.0, poll_deadline)
    scope.close_admission.assert_called_once_with(deadline=110.0)
    scope.has_pending.assert_not_called()


@pytest.fixture(autouse=True)
def require_explicit_process_launch_double(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Unit tests must replace process launches; real children belong in integration."""

    popen = Mock(
        side_effect=AssertionError("Unit test attempted a real process launch")
    )
    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    yield
    # Also catch callers that convert the guard's exception into an expected error.
    popen.assert_not_called()


@pytest.fixture
def memory_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep owner-unit output real without introducing a second process owner.

    Tests of the pipe writer opt into their own transport boundary or real
    process fixture; these lifecycle units use ordinary in-memory text streams.
    """
    # Pytest restores global streams between fixture setup and the test call.
    # Bind this unit's streams without replacing pytest's own capture objects.
    streams = SimpleNamespace(**vars(sys))
    streams.stdout = io.StringIO()
    streams.stderr = io.StringIO()
    monkeypatch.setattr(runner, "sys", streams)


def test_start_phase_inherits_live_output_and_starts_posix_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = Mock()
    popen = Mock(return_value=process)
    monkeypatch.setattr(runner.subprocess, "Popen", popen)

    assert runner._start_posix_process(COMMAND) is process

    _, keywords = popen.call_args
    assert keywords["start_new_session"] is True
    assert keywords["stdout"] is None
    assert keywords["stderr"] is None


@pytest.mark.parametrize("startup_error", [IndexError(), TypeError(), ValueError()])
@pytest.mark.parametrize("rollback_fails", [False, True])
def test_posix_startup_argument_failure_rolls_back_only_proven_scope(
    monkeypatch: pytest.MonkeyPatch,
    startup_error: Exception,
    rollback_fails: bool,
) -> None:
    from h2h_dev_tools import _process_scope

    monkeypatch.setattr(runner, "os", SimpleNamespace(name="posix"))
    scope = Mock(proven_empty=False)
    rollback_error = _process_scope.ScopeError("obligation identity changed")
    if rollback_fails:
        scope.close.side_effect = rollback_error
    monkeypatch.setattr(_process_scope.PosixScope, "create", Mock(return_value=scope))
    monkeypatch.setattr(runner, "_start_posix_process", Mock(side_effect=startup_error))

    with pytest.raises(runner.ProcessOwnershipError) as caught:
        runner.start_owned_process(COMMAND, deadline=100.0, signal_controller=None)

    assert caught.value.cleanup_proven is False
    assert caught.value.__cause__ is (
        rollback_error if rollback_fails else startup_error
    )
    assert scope.proven_empty is True
    scope.close.assert_called_once_with(deadline=100.0)


@pytest.mark.parametrize(
    "startup_error",
    [
        RuntimeError("unknown startup failure"),
        KeyboardInterrupt(),
        implementation.RunnerSignalInterrupt(15),
    ],
)
def test_posix_unknown_startup_failure_retains_obligation(
    monkeypatch: pytest.MonkeyPatch,
    startup_error: BaseException,
) -> None:
    from h2h_dev_tools import _process_scope

    monkeypatch.setattr(runner, "os", SimpleNamespace(name="posix"))
    scope = Mock(proven_empty=False)
    monkeypatch.setattr(_process_scope.PosixScope, "create", Mock(return_value=scope))
    monkeypatch.setattr(runner, "_start_posix_process", Mock(side_effect=startup_error))

    with pytest.raises(type(startup_error)) as caught:
        runner.start_owned_process(COMMAND, deadline=100.0, signal_controller=None)

    assert caught.value is startup_error
    assert scope.proven_empty is False
    scope.close.assert_not_called()


def test_windows_start_requires_a_real_new_process_group_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    popen = Mock()
    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    monkeypatch.delattr(runner.subprocess, "CREATE_NEW_PROCESS_GROUP", raising=False)

    with pytest.raises(runner.ProcessOwnershipError):
        runner._start_windows_process(
            COMMAND,
            deadline=100.0,
            signal_controller=None,
        )
    popen.assert_not_called()


def test_windows_start_uses_new_process_group_and_inherits_live_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdin = Mock()
    process = Mock(stdin=stdin)
    stdin.write.return_value = 1
    job = Mock()
    popen = Mock(return_value=process)
    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    monkeypatch.setattr(
        runner.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False
    )
    monkeypatch.setattr(runner.WindowsJob, "create", Mock(return_value=job))
    monkeypatch.setattr(
        runner,
        "_windows_supervisor_launch",
        Mock(return_value=(("python",), {})),
    )

    owner = runner._start_windows_process(
        COMMAND,
        deadline=100.0,
        signal_controller=None,
    )

    _, keywords = popen.call_args
    assert owner.process is process
    assert owner.windows_job is job
    assert keywords["creationflags"] == 512
    assert "start_new_session" not in keywords
    assert keywords["stdout"] is None
    assert keywords["stderr"] is None
    job.assign.assert_called_once_with(process)
    stdin.write.assert_called_once_with(runner._WINDOWS_START_TOKEN)


@pytest.mark.parametrize("with_windows_job", [False, True])
@pytest.mark.parametrize("terminated", [True, False])
@pytest.mark.usefixtures("memory_diagnostics")
def test_timeout_checks_owned_tree_termination(
    monkeypatch: pytest.MonkeyPatch,
    terminated: bool,
    with_windows_job: bool,
) -> None:
    process = Mock()
    job = Mock() if with_windows_job else None
    owner = implementation.OwnedProcess(process, job)
    monotonic = Mock(return_value=100.0)

    def started(*_args: object, **_kwargs: object) -> implementation.OwnedProcess:
        monotonic.return_value = 101.0
        return owner

    def expired(*, timeout: float) -> int:
        assert timeout == 3.0
        monotonic.return_value = 104.0
        raise subprocess.TimeoutExpired(cmd="pytest", timeout=timeout)

    process.wait.side_effect = expired
    start = Mock(side_effect=started)
    terminate = Mock(return_value=terminated)
    monkeypatch.setattr(runner, "start_owned_process", start)
    monkeypatch.setattr(runner, "terminate_owned_tree", terminate)
    monkeypatch.setattr(runner.time, "monotonic", monotonic)

    if terminated:
        assert (
            runner.run_command(COMMAND, hard_deadline=110.0) == runner.TIMEOUT_EXIT_CODE
        )
    else:
        with pytest.raises(runner.ProcessOwnershipError):
            runner.run_command(COMMAND, hard_deadline=110.0)
    process.wait.assert_called_once_with(timeout=3.0)
    start.assert_called_once_with(COMMAND, deadline=110.0, signal_controller=ANY)
    terminate.assert_called_once_with(owner, deadline=110.0, diagnostic=ANY)
    if job is not None:
        job.close.assert_called_once_with()


@pytest.mark.parametrize("with_windows_job", [False, True])
@pytest.mark.usefixtures("memory_diagnostics")
def test_phase_wait_subtracts_ownership_overhead_from_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch,
    with_windows_job: bool,
) -> None:
    process = Mock()
    job = Mock() if with_windows_job else None
    owner = implementation.OwnedProcess(process, job)
    monotonic = Mock(return_value=100.0)

    def started(*_args: object, **_kwargs: object) -> implementation.OwnedProcess:
        monotonic.return_value = 103.0
        return owner

    def waited(*, timeout: float) -> int:
        assert timeout == 1.0
        monotonic.return_value = 103.5
        return 0

    def empty(*_args: object, **_kwargs: object) -> bool:
        monotonic.return_value = 104.0
        return True

    process.wait.side_effect = waited
    start = Mock(side_effect=started)
    wait_for_tree = Mock(side_effect=empty)
    monkeypatch.setattr(runner, "start_owned_process", start)
    monkeypatch.setattr(runner, "wait_for_owned_tree_exit", wait_for_tree)
    monkeypatch.setattr(runner.time, "monotonic", monotonic)

    assert runner.run_command(COMMAND, hard_deadline=110.0) == 0
    process.wait.assert_called_once_with(timeout=1.0)
    start.assert_called_once_with(COMMAND, deadline=110.0, signal_controller=ANY)
    wait_for_tree.assert_called_once_with(owner, deadline=110.0, poll_deadline=103.75)
    if job is not None:
        job.close.assert_called_once_with()


def test_posix_termination_kills_surviving_group_after_leader_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = Mock(pid=4312)
    process.poll.return_value = 0
    kill_process_group = Mock()
    wait_for_group = Mock(side_effect=(False, True))
    monkeypatch.setattr(runner, "os", SimpleNamespace(killpg=kill_process_group))
    monkeypatch.setattr(runner, "signal", SimpleNamespace(SIGTERM=15, SIGKILL=9))
    monkeypatch.setattr(runner, "_wait_for_posix_process_group_exit", wait_for_group)

    assert runner._terminate_posix_process_group(process, deadline=1000.0)

    assert kill_process_group.call_args_list == [
        call(4312, runner.signal.SIGTERM),
        call(4312, runner.signal.SIGKILL),
    ]
    assert wait_for_group.call_count == 2


def test_posix_group_probe_does_not_trust_an_exited_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = Mock(pid=4312)
    process.poll.return_value = 0
    process.wait.return_value = 0
    kill_process_group = Mock(side_effect=(None, ProcessLookupError()))
    monotonic = Mock(side_effect=(1.0, 2.0))
    sleep = Mock()
    monkeypatch.setattr(runner.time, "monotonic", monotonic)
    monkeypatch.setattr(runner.time, "sleep", sleep)

    assert runner._wait_for_posix_process_group_exit(
        process,
        kill_process_group=kill_process_group,
        deadline=10.0,
    )
    assert kill_process_group.call_args_list == [call(4312, 0), call(4312, 0)]
    sleep.assert_called_once_with(runner.TERMINATION_POLL_SECONDS)
    process.wait.assert_called_once_with(timeout=8.0)


def test_posix_termination_reports_a_group_that_survives_sigkill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = Mock(pid=4312)
    kill_process_group = Mock()
    wait_for_group = Mock(side_effect=(False, False))
    monkeypatch.setattr(runner, "os", SimpleNamespace(killpg=kill_process_group))
    monkeypatch.setattr(runner, "signal", SimpleNamespace(SIGTERM=15, SIGKILL=9))
    monkeypatch.setattr(runner, "_wait_for_posix_process_group_exit", wait_for_group)

    assert not runner._terminate_posix_process_group(process, deadline=1000.0)


@pytest.mark.parametrize("edge", [15, 9], ids=["term", "kill"])
@pytest.mark.parametrize(
    "outcome", ["resolved", "permission", "other-error", "pending"]
)
def test_posix_signal_eperm_requires_bounded_complete_cleanup_proof(
    monkeypatch: pytest.MonkeyPatch, edge: int, outcome: str
) -> None:
    process = Mock(pid=4312)
    process.poll.return_value = 0
    process.wait.return_value = 0
    scope = Mock()
    clock = 100.0
    deadline = 106.0
    signaled = False
    probes = 0
    calls: list[int] = []

    def sleep(seconds: float) -> None:
        nonlocal clock
        clock += seconds

    def pending(*, deadline: float) -> bool:
        assert deadline <= 106.0
        return signaled and outcome == "pending"

    def kill_process_group(group: int, number: int) -> None:
        nonlocal signaled, probes
        assert group == process.pid
        calls.append(number)
        if number == edge:
            signaled = True
            if outcome == "other-error":
                raise OSError(errno.EIO, "Unknown signaling failure")
            raise PermissionError(errno.EPERM, "No absence evidence")
        if number == 0 and signaled:
            probes += 1
            if outcome == "permission" or (outcome == "resolved" and probes == 1):
                raise PermissionError(errno.EPERM, "Still no absence evidence")
            raise ProcessLookupError(errno.ESRCH, "Group absent")

    scope.has_pending.side_effect = pending
    monkeypatch.setattr(runner, "os", SimpleNamespace(killpg=kill_process_group))
    monkeypatch.setattr(runner, "signal", SimpleNamespace(SIGTERM=15, SIGKILL=9))
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock)
    monkeypatch.setattr(runner.time, "sleep", sleep)

    assert runner._terminate_posix_process_group(
        process, deadline=deadline, scope=scope
    ) is (outcome == "resolved")
    scope.close_admission.assert_called_once_with(deadline=deadline)
    assert signaled and calls.count(edge) == 1
    assert clock <= deadline
    if outcome == "resolved":
        assert probes == 2
        assert clock < deadline
        process.wait.assert_called_once()
    else:
        process.wait.assert_not_called()
        if outcome == "other-error":
            assert probes == 0 and clock < deadline
        else:
            assert probes >= 2 and clock == deadline


def test_windows_taskkill_success_is_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = Mock(pid=4312)
    taskkill = Mock(
        return_value=subprocess.CompletedProcess(
            args=(),
            returncode=0,
            stdout="SUCCESS",
            stderr="",
        )
    )
    monotonic = Mock(return_value=100.0)
    monkeypatch.setattr(runner.subprocess, "run", taskkill)
    monkeypatch.setattr(runner.time, "monotonic", monotonic)

    assert runner._taskkill_windows_tree(process, deadline=110.0)
    taskkill.assert_called_once_with(
        ("taskkill", "/PID", "4312", "/T", "/F"),
        check=False,
        capture_output=True,
        text=True,
        timeout=runner.TERMINATION_GRACE_SECONDS,
    )


@pytest.mark.usefixtures("memory_diagnostics")
def test_windows_taskkill_nonzero_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = Mock(pid=4312)
    taskkill = Mock(
        return_value=subprocess.CompletedProcess(
            args=(),
            returncode=1,
            stdout="",
            stderr="process tree not found",
        )
    )
    monkeypatch.setattr(runner.subprocess, "run", taskkill)
    monkeypatch.setattr(runner.time, "monotonic", Mock(return_value=100.0))

    assert not runner._taskkill_windows_tree(process, deadline=110.0)
    process.wait.assert_not_called()


def test_windows_job_create_builds_real_ctypes_structures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel32 = Mock()
    kernel32.CreateJobObjectW.return_value = 77
    kernel32.SetInformationJobObject.return_value = True
    kernel32.CloseHandle.return_value = True
    win_dll = Mock(return_value=kernel32)
    monkeypatch.setattr(runner, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(runner.ctypes, "WinDLL", win_dll, raising=False)

    job = runner.WindowsJob.create()

    assert job._handle == 77
    win_dll.assert_called_once_with("kernel32", use_last_error=True)
    kernel32.SetInformationJobObject.assert_called_once()
    information_pointer = kernel32.SetInformationJobObject.call_args.args[2]
    assert information_pointer is not None


def test_windows_start_assigns_job_before_opening_start_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    stdin = Mock()
    process = Mock(stdin=stdin)
    job = Mock()
    job.assign.side_effect = lambda _process: events.append("assign")

    def open_start_gate(_token: bytes) -> int:
        events.append("start")
        return 1

    stdin.write.side_effect = open_start_gate

    def popen(*_args: object, **_kwargs: object) -> object:
        events.append("popen")
        return process

    def create_job() -> object:
        events.append("job")
        return job

    monkeypatch.setattr(
        runner.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False
    )
    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    monkeypatch.setattr(runner.WindowsJob, "create", create_job)
    monkeypatch.setattr(
        runner,
        "_windows_supervisor_launch",
        Mock(return_value=(("python",), {})),
    )

    owner = runner._start_windows_process(
        COMMAND,
        deadline=100.0,
        signal_controller=None,
    )

    assert owner.process is process
    assert owner.windows_job is job
    assert events == ["job", "popen", "assign", "start"]
    stdin.close.assert_called_once_with()


def test_windows_assignment_failure_never_opens_start_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdin = Mock()
    process = Mock(stdin=stdin)
    process.poll.side_effect = (None, 0)
    process.wait.return_value = 1
    job = Mock()
    job.assign.side_effect = OSError("assignment failed")
    monkeypatch.setattr(runner.time, "monotonic", Mock(return_value=90.0))
    monkeypatch.setattr(
        runner.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False
    )
    monkeypatch.setattr(runner.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(runner.WindowsJob, "create", Mock(return_value=job))
    monkeypatch.setattr(
        runner,
        "_windows_supervisor_launch",
        Mock(return_value=(("python",), {})),
    )

    with pytest.raises(runner.ProcessOwnershipError):
        runner._start_windows_process(
            COMMAND,
            deadline=100.0,
            signal_controller=None,
        )

    stdin.write.assert_not_called()
    process.kill.assert_called_once_with()
    process.wait.assert_called_once_with(timeout=10.0)
    job.close.assert_called_once_with()


def test_windows_establishment_cleanup_failure_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdin = Mock()
    process = Mock(stdin=stdin)
    process.poll.return_value = None
    process.kill.side_effect = OSError("kill failed")
    process.wait.side_effect = subprocess.TimeoutExpired("supervisor", 1.0)
    job = Mock()
    job.assign.side_effect = KeyboardInterrupt
    monkeypatch.setattr(runner.time, "monotonic", Mock(return_value=100.0))
    monkeypatch.setattr(
        runner.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False
    )
    monkeypatch.setattr(runner.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(runner.WindowsJob, "create", Mock(return_value=job))
    monkeypatch.setattr(
        runner,
        "_windows_supervisor_launch",
        Mock(return_value=(("python",), {})),
    )

    with pytest.raises(runner.ProcessOwnershipError):
        runner._start_windows_process(
            COMMAND,
            deadline=101.0,
            signal_controller=runner.SignalController(),
        )

    stdin.write.assert_not_called()
    job.close.assert_called_once_with()


def test_windows_signal_during_establishment_cleanup_waits_for_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = runner.SignalController()
    stdin = Mock()
    process = Mock(stdin=stdin)
    job = Mock()
    job.assign.side_effect = OSError("assignment failed")

    def cleanup(*_args: object, **_kwargs: object) -> bool:
        controller.receive(signal.SIGINT)
        controller.receive(21)
        return True

    monkeypatch.setattr(
        runner.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False
    )
    monkeypatch.setattr(runner.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(runner.WindowsJob, "create", Mock(return_value=job))
    monkeypatch.setattr(
        runner,
        "_windows_supervisor_launch",
        Mock(return_value=(("python",), {})),
    )
    cleanup_mock = Mock(side_effect=cleanup)
    monkeypatch.setattr(runner, "_cleanup_windows_supervisor", cleanup_mock)

    with pytest.raises(KeyboardInterrupt):
        runner._start_windows_process(
            COMMAND,
            deadline=100.0,
            signal_controller=controller,
        )

    cleanup_mock.assert_called_once()
    stdin.write.assert_not_called()


def test_windows_venv_launch_uses_base_interpreter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    base = tmp_path / "python.exe"
    base.touch()
    venv = tmp_path / ".venv" / "Scripts" / "python.exe"
    monkeypatch.setattr(runner.sys, "executable", str(venv))
    monkeypatch.setattr(runner.sys, "_base_executable", str(base), raising=False)
    monkeypatch.setenv("__PYVENV_LAUNCHER__", "must-not-leak")

    command, environment = runner._windows_supervisor_launch(COMMAND)

    assert command[0] == str(base)
    assert "__PYVENV_LAUNCHER__" not in environment


def test_windows_signal_handlers_cover_sigint_and_sigbreak_and_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sigint = 2
    sigbreak = 21
    old_sigint = object()
    old_sigbreak = object()
    getsignal = Mock(side_effect=(old_sigint, old_sigbreak))
    install = Mock()
    monkeypatch.setattr(runner, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        runner,
        "signal",
        SimpleNamespace(
            SIGINT=sigint,
            SIGBREAK=sigbreak,
            getsignal=getsignal,
            signal=install,
        ),
    )

    with runner.controlled_termination_signals() as controller:
        assert controller is not None
        assert install.call_count == 2
        installed_handler = install.call_args_list[0].args[1]
        assert install.call_args_list[1].args[1] is installed_handler

    assert getsignal.call_args_list == [call(sigint), call(sigbreak)]
    assert install.call_args_list[2:] == [
        call(sigbreak, old_sigbreak),
        call(sigint, old_sigint),
    ]


def test_signal_controller_defers_first_signal_and_ignores_repeats() -> None:
    controller = runner.SignalController()

    with pytest.raises(KeyboardInterrupt):
        with controller.defer():
            controller.receive(signal.SIGINT)
            controller.receive(21)
            assert controller.pending_signal == signal.SIGINT
            assert not controller.interruption_started

    assert controller.pending_signal is None
    assert controller.interruption_started
    controller.receive(21)


@pytest.mark.usefixtures("memory_diagnostics")
def test_bounded_interrupt_cleanup_never_extends_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = Mock()
    monotonic = Mock(return_value=100.0)

    def interrupted(*, timeout: float) -> int:
        assert timeout == 4.0
        monotonic.return_value = 109.0
        raise KeyboardInterrupt

    process.wait.side_effect = interrupted
    owner = runner.OwnedProcess(process)
    terminate = Mock(return_value=True)
    monkeypatch.setattr(runner, "start_owned_process", Mock(return_value=owner))
    monkeypatch.setattr(runner, "terminate_owned_tree", terminate)
    monkeypatch.setattr(runner.time, "monotonic", monotonic)

    with pytest.raises(KeyboardInterrupt):
        runner.run_command(
            COMMAND,
            hard_deadline=110.0,
            signal_controller=runner.SignalController(),
        )

    terminate.assert_called_once_with(owner, deadline=110.0, diagnostic=ANY)


@pytest.mark.usefixtures("memory_diagnostics")
def test_windows_signal_during_timeout_cleanup_waits_for_empty_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = runner.SignalController()
    process = Mock()
    process.wait.side_effect = subprocess.TimeoutExpired("pytest", 3.0)
    owner = runner.OwnedProcess(process)

    def terminate(
        _owner: object, *, deadline: float, diagnostic: Callable[[str], None]
    ) -> bool:
        assert callable(diagnostic)
        assert deadline == 110.0
        controller.receive(signal.SIGINT)
        controller.receive(21)
        return True

    monkeypatch.setattr(runner, "start_owned_process", Mock(return_value=owner))
    monkeypatch.setattr(runner, "terminate_owned_tree", terminate)
    monkeypatch.setattr(
        runner.time,
        "monotonic",
        Mock(return_value=100.0),
    )

    with pytest.raises(KeyboardInterrupt):
        runner.run_command(
            COMMAND,
            hard_deadline=110.0,
            signal_controller=controller,
        )


@pytest.mark.usefixtures("memory_diagnostics")
def test_windows_failed_cleanup_cannot_publish_interrupt_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = runner.SignalController()
    process = Mock()
    process.wait.side_effect = subprocess.TimeoutExpired("pytest", 3.0)
    owner = runner.OwnedProcess(process)

    def terminate(
        _owner: object, *, deadline: float, diagnostic: Callable[[str], None]
    ) -> bool:
        assert callable(diagnostic)
        assert deadline == 110.0
        controller.receive(signal.SIGINT)
        return False

    monkeypatch.setattr(runner, "start_owned_process", Mock(return_value=owner))
    monkeypatch.setattr(runner, "terminate_owned_tree", terminate)
    monkeypatch.setattr(
        runner.time,
        "monotonic",
        Mock(return_value=100.0),
    )

    with pytest.raises(runner.ProcessOwnershipError):
        runner.run_command(
            COMMAND,
            hard_deadline=110.0,
            signal_controller=controller,
        )


@pytest.mark.usefixtures("memory_diagnostics")
def test_windows_normal_leader_survivor_is_cleaned_and_fails_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = Mock(returncode=0)
    process.wait.return_value = 0
    owner = runner.OwnedProcess(process)
    wait_tree = Mock(return_value=False)
    terminate = Mock(return_value=True)
    monkeypatch.setattr(runner, "start_owned_process", Mock(return_value=owner))
    monkeypatch.setattr(runner, "wait_for_owned_tree_exit", wait_tree)
    monkeypatch.setattr(runner, "terminate_owned_tree", terminate)
    monkeypatch.setattr(
        runner.time,
        "monotonic",
        Mock(return_value=100.0),
    )

    with pytest.raises(runner.ProcessOwnershipError):
        runner.run_command(COMMAND, hard_deadline=110.0)

    terminate.assert_called_once_with(owner, deadline=110.0, diagnostic=ANY)


@pytest.mark.parametrize("terminated", [False, True])
@pytest.mark.parametrize("closed", [False, True])
@pytest.mark.usefixtures("memory_diagnostics")
def test_ownership_failure_reports_cleanup_only_after_empty_tree_and_owner_close(
    monkeypatch: pytest.MonkeyPatch, terminated: bool, closed: bool
) -> None:
    child = Mock(returncode=1)
    child.wait.return_value = 1
    owner = runner.OwnedProcess(child)
    monkeypatch.setattr(runner, "start_owned_process", Mock(return_value=owner))
    monkeypatch.setattr(runner, "wait_for_owned_tree_exit", Mock(return_value=False))
    monkeypatch.setattr(runner, "terminate_owned_tree", Mock(return_value=terminated))
    monkeypatch.setattr(runner, "close_owner", Mock(return_value=closed))
    monkeypatch.setattr(runner.time, "monotonic", Mock(return_value=100.0))
    with pytest.raises(runner.ProcessOwnershipError) as failure:
        runner.run_command(COMMAND, hard_deadline=110.0)
    assert failure.value.cleanup_proven is (terminated and closed)


@pytest.mark.usefixtures("memory_diagnostics")
def test_windows_job_termination_falls_back_to_taskkill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = Mock(pid=4312)
    job = Mock()
    job.terminate.side_effect = OSError("job termination failed")
    owner = runner.OwnedProcess(process, job)
    taskkill = Mock(return_value=True)
    wait_tree = Mock(return_value=True)
    monkeypatch.setattr(runner, "_taskkill_windows_tree", taskkill)
    monkeypatch.setattr(runner, "wait_for_owned_tree_exit", wait_tree)
    monkeypatch.setattr(runner.time, "monotonic", Mock(return_value=100.0))

    assert runner._terminate_windows_tree(owner, deadline=110.0)
    taskkill.assert_called_once_with(process, deadline=110.0, diagnostic=None)
    wait_tree.assert_called_once_with(owner, deadline=110.0)


def test_windows_job_query_failure_cannot_issue_empty_receipt() -> None:
    process = Mock()
    job = Mock()
    job.wait_empty.side_effect = OSError("query failed")
    owner = runner.OwnedProcess(process, job)

    assert not runner.wait_for_owned_tree_exit(owner, deadline=110.0)


def test_windows_termination_does_not_start_after_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = Mock()
    owner = runner.OwnedProcess(Mock(), job)
    monkeypatch.setattr(runner.time, "monotonic", Mock(return_value=110.0))

    assert not runner._terminate_windows_tree(owner, deadline=110.0)
    job.terminate.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    (OSError("taskkill unavailable"), subprocess.TimeoutExpired("taskkill", 2.0)),
)
def test_windows_taskkill_exception_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    monkeypatch.setattr(runner.subprocess, "run", Mock(side_effect=failure))
    monkeypatch.setattr(runner.time, "monotonic", Mock(return_value=100.0))

    assert not runner._taskkill_windows_tree(Mock(pid=4312), deadline=110.0)


def test_windows_taskkill_uses_only_bounded_remaining_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Mock(return_value=subprocess.CompletedProcess((), 0, "", ""))
    monkeypatch.setattr(runner.subprocess, "run", run)
    monkeypatch.setattr(runner.time, "monotonic", Mock(return_value=109.5))

    assert runner._taskkill_windows_tree(Mock(pid=4312), deadline=110.0)
    assert run.call_args_list == [
        call(
            ("taskkill", "/PID", "4312", "/T", "/F"),
            check=False,
            capture_output=True,
            text=True,
            timeout=0.5,
        )
    ]


def test_windows_job_wait_uses_active_process_count_until_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounting_type = type(
        "Accounting",
        (ctypes.Structure,),
        {"_fields_": [("ActiveProcesses", ctypes.c_ulong)]},
    )
    kernel32 = Mock()
    active = iter((1, 0))

    def query(
        _handle: object,
        _kind: object,
        information_pointer: object,
        _size: object,
        _unused: object,
    ) -> bool:
        information: Any = ctypes.cast(
            cast(Any, information_pointer),
            ctypes.POINTER(accounting_type),
        ).contents
        information.ActiveProcesses = next(active)
        return True

    kernel32.QueryInformationJobObject.side_effect = query
    job = runner.WindowsJob(kernel32, 77, accounting_type)
    monkeypatch.setattr(
        runner.time,
        "monotonic",
        Mock(side_effect=(100.0, 100.1)),
    )
    monkeypatch.setattr(runner.time, "sleep", Mock())

    assert job.wait_empty(deadline=110.0)
    assert kernel32.QueryInformationJobObject.call_count == 2


@pytest.mark.usefixtures("memory_diagnostics")
def test_windows_job_close_failure_fails_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = Mock(returncode=0)
    process.wait.return_value = 0
    job = Mock()
    job.close.side_effect = OSError("close failed")
    owner = runner.OwnedProcess(process, job)
    monkeypatch.setattr(runner, "start_owned_process", Mock(return_value=owner))
    monkeypatch.setattr(runner, "wait_for_owned_tree_exit", Mock(return_value=True))
    monkeypatch.setattr(
        runner.time,
        "monotonic",
        Mock(return_value=100.0),
    )

    with pytest.raises(runner.ProcessOwnershipError):
        runner.run_command(COMMAND, hard_deadline=110.0)


@pytest.mark.usefixtures("memory_diagnostics")
def test_windows_signal_during_job_close_is_delivered_after_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = runner.SignalController()
    process = Mock(returncode=0)
    process.wait.return_value = 0
    job = Mock()
    job.close.side_effect = lambda: controller.receive(signal.SIGINT)
    owner = runner.OwnedProcess(process, job)
    monkeypatch.setattr(runner, "start_owned_process", Mock(return_value=owner))
    monkeypatch.setattr(runner, "wait_for_owned_tree_exit", Mock(return_value=True))
    monkeypatch.setattr(
        runner.time,
        "monotonic",
        Mock(return_value=100.0),
    )

    with pytest.raises(KeyboardInterrupt):
        runner.run_command(
            COMMAND,
            hard_deadline=110.0,
            signal_controller=controller,
        )
    job.close.assert_called_once_with()


@pytest.mark.parametrize("hard_deadline", [None, 110.0])
@pytest.mark.parametrize("failed_wait", ["leader", "tree"])
@pytest.mark.usefixtures("memory_diagnostics")
def test_wait_error_still_terminates_and_closes_owner(
    monkeypatch: pytest.MonkeyPatch,
    hard_deadline: float | None,
    failed_wait: str,
) -> None:
    process = Mock()
    process.wait.return_value = 0
    wait_tree = Mock(return_value=True)
    wait_error = OSError("wait failed")
    if failed_wait == "leader":
        process.wait.side_effect = wait_error
    else:
        wait_tree.side_effect = wait_error
    job = Mock()
    owner = runner.OwnedProcess(process, job)
    lifecycle = Mock()
    terminate = Mock(return_value=True)
    lifecycle.attach_mock(terminate, "terminate")
    lifecycle.attach_mock(job.close, "close")
    monkeypatch.setattr(runner, "start_owned_process", Mock(return_value=owner))
    monkeypatch.setattr(runner, "wait_for_owned_tree_exit", wait_tree)
    monkeypatch.setattr(runner, "terminate_owned_tree", terminate)
    monkeypatch.setattr(runner.time, "monotonic", Mock(return_value=100.0))

    with pytest.raises(OSError, match="wait failed") as caught:
        runner.run_command(COMMAND, hard_deadline=hard_deadline)

    assert caught.value is wait_error
    # A returned leader already established its bounded tree-probe deadline;
    # a wait exception starts only the short cleanup reserve.
    expected_deadline = (
        hard_deadline if failed_wait == "tree" and hard_deadline else 106.0
    )
    assert lifecycle.mock_calls == [
        call.terminate(owner, deadline=expected_deadline, diagnostic=ANY),
        call.close(),
    ]


@pytest.mark.parametrize("cleanup_error", [False, OSError("termination failed")])
@pytest.mark.usefixtures("memory_diagnostics")
def test_failed_termination_still_closes_job_and_fails_ownership(
    monkeypatch: pytest.MonkeyPatch,
    cleanup_error: bool | OSError,
) -> None:
    process = Mock()
    process.wait.side_effect = OSError("wait failed")
    job = Mock()
    owner = runner.OwnedProcess(process, job)
    terminate = (
        Mock(side_effect=cleanup_error)
        if isinstance(cleanup_error, OSError)
        else Mock(return_value=cleanup_error)
    )
    monkeypatch.setattr(runner, "start_owned_process", Mock(return_value=owner))
    monkeypatch.setattr(runner, "terminate_owned_tree", terminate)
    monkeypatch.setattr(runner.time, "monotonic", Mock(return_value=100.0))

    with pytest.raises(runner.ProcessOwnershipError):
        runner.run_command(COMMAND, hard_deadline=110.0)

    terminate.assert_called_once_with(owner, deadline=106.0, diagnostic=ANY)
    job.close.assert_called_once_with()


@pytest.mark.parametrize(
    "diagnostic_error",
    [BrokenPipeError, OSError, ValueError, RuntimeError],
)
@pytest.mark.parametrize("interrupted", [False, True])
@pytest.mark.usefixtures("memory_diagnostics")
def test_diagnostic_failure_cannot_skip_teardown_or_job_close(
    monkeypatch: pytest.MonkeyPatch,
    diagnostic_error: type[Exception],
    interrupted: bool,
) -> None:
    process = Mock()
    process.wait.side_effect = (
        KeyboardInterrupt()
        if interrupted
        else subprocess.TimeoutExpired("command", 4.0)
    )
    job = Mock()
    owner = runner.OwnedProcess(process, job)
    terminate = Mock(return_value=True)
    diagnostic_stream = io.StringIO()
    failed_write = Mock(side_effect=diagnostic_error("diagnostic unavailable"))
    monkeypatch.setattr(diagnostic_stream, "write", failed_write)
    monkeypatch.setattr(runner.sys, "stderr", diagnostic_stream)
    monkeypatch.setattr(runner, "start_owned_process", Mock(return_value=owner))
    monkeypatch.setattr(runner, "terminate_owned_tree", terminate)
    monkeypatch.setattr(runner.time, "monotonic", Mock(return_value=100.0))

    if interrupted:
        with pytest.raises(KeyboardInterrupt):
            runner.run_command(COMMAND, hard_deadline=110.0)
    else:
        assert (
            runner.run_command(COMMAND, hard_deadline=110.0) == runner.TIMEOUT_EXIT_CODE
        )

    terminate.assert_called_once_with(
        owner, deadline=106.0 if interrupted else 110.0, diagnostic=ANY
    )
    job.close.assert_called_once_with()
    failed_write.assert_called()


@pytest.mark.parametrize("standard_output", [False, True])
@pytest.mark.parametrize(
    ("binary_capture", "newline"),
    [(False, "\n"), (True, None), (True, "\n"), (True, "\r\n")],
    ids=["string", "native", "lf", "crlf"],
)
def test_diagnostics_preserve_memory_only_capture(
    monkeypatch: pytest.MonkeyPatch,
    standard_output: bool,
    binary_capture: bool,
    newline: str | None,
) -> None:
    binary = io.BytesIO()
    stream = (
        io.TextIOWrapper(binary, encoding="utf-8", newline=newline)
        if binary_capture
        else io.StringIO()
    )
    with monkeypatch.context() as capture:
        capture.setattr(runner.sys, "stdout" if standard_output else "stderr", stream)
        runner.report_diagnostic("清理已完成 ✓", standard_output=standard_output)
    if binary_capture:
        # Native text streams retain their configured newline translation.
        terminator = os.linesep if newline is None else newline
        assert binary.getvalue() == f"清理已完成 ✓{terminator}".encode()
    else:
        assert isinstance(stream, io.StringIO)
        assert stream.getvalue() == "清理已完成 ✓\n"
    stream.close()


@pytest.mark.parametrize("fileno_error", [None, io.UnsupportedOperation, ValueError])
def test_diagnostics_never_write_to_an_unknown_no_descriptor_sink(
    monkeypatch: pytest.MonkeyPatch, fileno_error: type[Exception] | None
) -> None:
    sink = SimpleNamespace(write=Mock(), flush=Mock())
    if fileno_error is not None:
        sink.fileno = Mock(side_effect=fileno_error("no descriptor"))
    monkeypatch.setattr(runner.sys, "stderr", sink)

    runner.report_diagnostic("Must not block on an unknown stream")

    sink.write.assert_not_called()
    sink.flush.assert_not_called()


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16-le"])
def test_windows_pipe_diagnostics_preserve_payload_without_mutating_shared_flags(
    monkeypatch: pytest.MonkeyPatch, encoding: str
) -> None:
    sink = Mock(encoding=encoding, errors="strict")
    sink.fileno.return_value = 42
    payload = "清理已完成 ✓\n".encode(encoding)
    native = SimpleNamespace(
        name="nt",
        fstat=Mock(return_value=SimpleNamespace(st_mode=stat.S_IFIFO)),
        get_blocking=Mock(),
        set_blocking=Mock(),
        write=Mock(),
    )
    transport = Mock()
    monkeypatch.setattr(diagnostic, "write_diagnostic", transport)
    monkeypatch.setattr(runner, "os", native)
    monkeypatch.setattr(runner.sys, "stderr", sink)
    monkeypatch.setattr(runner.time, "monotonic", Mock(return_value=100.0))
    controller = runner.SignalController()
    runner.report_diagnostic(
        "清理已完成 ✓", deadline=101.0, signal_controller=controller
    )

    transport.assert_called_once()
    assert transport.call_args.args == (42, payload)
    assert 100.0 < transport.call_args.kwargs["deadline"] <= 101.0
    assert transport.call_args.kwargs["signal_controller"] is controller
    native.get_blocking.assert_not_called()
    native.set_blocking.assert_not_called()
    native.write.assert_not_called()
    sink.write.assert_not_called()
    sink.flush.assert_not_called()


class _NativeDiagnosticCapture(io.TextIOWrapper):
    def fileno(self) -> int:
        return 42


@pytest.mark.parametrize("mode", [stat.S_IFREG, stat.S_IFCHR])
@pytest.mark.parametrize("newline", [None, "\n", "\r\n"], ids=["native", "lf", "crlf"])
def test_windows_regular_and_console_diagnostics_keep_native_text_encoding(
    monkeypatch: pytest.MonkeyPatch, mode: int, newline: str | None
) -> None:
    buffer = io.BytesIO()
    sink = _NativeDiagnosticCapture(buffer, encoding="utf-16-le", newline=newline)
    native = SimpleNamespace(
        name="nt",
        fstat=Mock(return_value=SimpleNamespace(st_mode=mode)),
        get_blocking=Mock(),
        set_blocking=Mock(),
        write=Mock(),
    )
    monkeypatch.setattr(runner, "os", native)
    with monkeypatch.context() as capture:
        capture.setattr(runner.sys, "stderr", sink)
        runner.report_diagnostic("清理已完成 ✓")

    terminator = os.linesep if newline is None else newline
    assert buffer.getvalue() == f"清理已完成 ✓{terminator}".encode("utf-16-le")
    native.get_blocking.assert_not_called()
    native.set_blocking.assert_not_called()
    native.write.assert_not_called()
    sink.close()


@pytest.mark.parametrize("taskkill_status", [0, 1])
@pytest.mark.usefixtures("memory_diagnostics")
def test_broken_diagnostics_cannot_skip_windows_taskkill_fallback_or_job_close(
    monkeypatch: pytest.MonkeyPatch,
    taskkill_status: int,
) -> None:
    process = Mock(pid=4312)
    process.wait.side_effect = subprocess.TimeoutExpired("command", 4.0)
    job = Mock()
    job.terminate.side_effect = OSError("job termination failed")
    owner = runner.OwnedProcess(process, job)
    taskkill = Mock(
        return_value=subprocess.CompletedProcess((), taskkill_status, "", "failed")
    )
    wait_tree = Mock(return_value=True)
    diagnostic_stream = io.StringIO()
    failed_write = Mock(side_effect=BrokenPipeError("diagnostic pipe closed"))
    monkeypatch.setattr(diagnostic_stream, "write", failed_write)
    monkeypatch.setattr(runner.sys, "stderr", diagnostic_stream)
    monkeypatch.setattr(runner, "start_owned_process", Mock(return_value=owner))
    monkeypatch.setattr(runner.subprocess, "run", taskkill)
    monkeypatch.setattr(runner, "wait_for_owned_tree_exit", wait_tree)
    monkeypatch.setattr(runner.time, "monotonic", Mock(return_value=100.0))

    if taskkill_status:
        with pytest.raises(runner.ProcessOwnershipError) as failure:
            runner.run_command(COMMAND, hard_deadline=110.0)
        assert "job termination failed" in failure.value.__notes__
        wait_tree.assert_not_called()
    else:
        assert (
            runner.run_command(COMMAND, hard_deadline=110.0) == runner.TIMEOUT_EXIT_CODE
        )
        wait_tree.assert_called_once_with(owner, deadline=110.0)

    taskkill.assert_called_once_with(
        ("taskkill", "/PID", "4312", "/T", "/F"),
        check=False,
        capture_output=True,
        text=True,
        timeout=runner.TERMINATION_GRACE_SECONDS,
    )
    job.close.assert_called_once_with()
    if taskkill_status:
        failed_write.assert_not_called()
    else:
        failed_write.assert_called()


@pytest.mark.usefixtures("memory_diagnostics")
def test_explicit_controller_does_not_replace_callers_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = Mock()
    process.wait.return_value = 0
    owner = runner.OwnedProcess(process)
    controller = runner.SignalController()
    start = Mock(return_value=owner)
    automatic_controller = Mock(side_effect=AssertionError("must reuse controller"))
    monkeypatch.setattr(runner, "controlled_termination_signals", automatic_controller)
    monkeypatch.setattr(runner, "start_owned_process", start)
    monkeypatch.setattr(runner, "wait_for_owned_tree_exit", Mock(return_value=True))
    monkeypatch.setattr(runner.time, "monotonic", Mock(return_value=100.0))

    assert (
        runner.run_command(COMMAND, hard_deadline=110.0, signal_controller=controller)
        == 0
    )

    automatic_controller.assert_not_called()
    assert start.call_args.kwargs["signal_controller"] is controller


@pytest.mark.parametrize("assigned", [False, True])
@pytest.mark.parametrize(
    "wait_error", [OSError("wait failed"), RuntimeError("wait bug")]
)
def test_startup_cleanup_wait_error_always_closes_job(
    monkeypatch: pytest.MonkeyPatch,
    assigned: bool,
    wait_error: Exception,
) -> None:
    process = Mock()
    process.poll.return_value = None
    process.wait.side_effect = wait_error
    job = Mock()
    job.wait_empty.return_value = True
    monkeypatch.setattr(runner.time, "monotonic", Mock(return_value=100.0))

    if isinstance(wait_error, OSError):
        assert not runner._cleanup_windows_supervisor(
            process, job, assigned=assigned, deadline=110.0
        )
    else:
        with pytest.raises(RuntimeError, match="wait bug"):
            runner._cleanup_windows_supervisor(
                process, job, assigned=assigned, deadline=110.0
            )

    process.wait.assert_called_once_with(timeout=10.0)
    job.close.assert_called_once_with()
    if assigned:
        job.terminate.assert_called_once_with()
        job.wait_empty.assert_called_once_with(deadline=110.0)
        process.kill.assert_not_called()
    else:
        process.kill.assert_called_once_with()
        job.terminate.assert_not_called()


@pytest.mark.parametrize("windows", [False, True])
@pytest.mark.parametrize("custom_sink", [False, True])
@pytest.mark.parametrize("failure", [False, True])
def test_owner_close_uses_phase_deadline_and_caller_diagnostic_sink(
    monkeypatch: pytest.MonkeyPatch, windows: bool, custom_sink: bool, failure: bool
) -> None:
    resource = Mock()
    if failure:
        resource.close.side_effect = OSError("resource close failed")
    owner = runner.OwnedProcess(
        Mock(),
        resource if windows else None,
        None if windows else resource,
    )
    default_sink = Mock()
    messages: list[str] = []
    monkeypatch.setattr(runner, "report_diagnostic", default_sink)
    assert runner.close_owner(
        owner, deadline=123.0, diagnostic=messages.append if custom_sink else None
    ) is (not failure)
    if windows:
        resource.close.assert_called_once_with()
    else:
        resource.close.assert_called_once_with(deadline=123.0)
    if failure and custom_sink:
        assert messages == ["resource close failed"]
        default_sink.assert_not_called()
    elif failure:
        assert not messages
        default_sink.assert_called_once_with("resource close failed", deadline=123.0)
    else:
        assert not messages
        default_sink.assert_not_called()


def test_diagnostic_transport_ownership_failure_is_not_optional_output_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink = Mock(encoding="utf-8", errors="strict")
    sink.fileno.return_value = 42
    error = runner.ProcessOwnershipError("helper cleanup unproven")
    transport = Mock(side_effect=error)
    monkeypatch.setattr(diagnostic, "write_diagnostic", transport)
    monkeypatch.setattr(runner.sys, "stderr", sink)
    monkeypatch.setattr(runner, "_diagnostic_requires_writer", lambda _fd: True)
    with pytest.raises(runner.ProcessOwnershipError) as caught:
        runner.report_diagnostic(
            "cleanup failed", signal_controller=runner.SignalController()
        )
    assert caught.value is error
    transport.assert_called_once()
    sink.write.assert_not_called()
    sink.flush.assert_not_called()


@pytest.mark.parametrize("taskkill_status", [0, 1])
@pytest.mark.parametrize(
    "interrupt",
    [None, int(signal.SIGINT), int(getattr(signal, "SIGBREAK", signal.SIGTERM))],
    ids=["no-signal", "sigint", "platform-termination"],
)
@pytest.mark.usefixtures("memory_diagnostics")
def test_windows_fallback_diagnostics_wait_for_owner_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    taskkill_status: int,
    interrupt: int | None,
) -> None:
    child = Mock(pid=4312)
    child.wait.side_effect = subprocess.TimeoutExpired("command", 4.0)
    job = Mock()
    job.terminate.side_effect = OSError("job termination failed")
    owner = runner.OwnedProcess(child, job)
    wait_tree = Mock(return_value=True)
    diagnostic_events: list[tuple[int, bool, bytes]] = []
    sink = _NativeDiagnosticCapture(io.BytesIO(), encoding="utf-8")
    monkeypatch.setattr(runner.sys, "stderr", sink)
    monkeypatch.setattr(runner, "_diagnostic_requires_writer", Mock(return_value=True))
    monkeypatch.setattr(runner, "start_owned_process", Mock(return_value=owner))
    monkeypatch.setattr(runner, "wait_for_owned_tree_exit", wait_tree)
    monkeypatch.setattr(runner.time, "monotonic", Mock(return_value=100.0))

    def deliver_signal() -> None:
        if interrupt is not None:
            handler = signal.getsignal(interrupt)
            assert callable(handler)
            handler(interrupt, None)

    def taskkill(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        # The primary controller must keep this signal pending during cleanup.
        deliver_signal()
        return subprocess.CompletedProcess((), taskkill_status, "", "fallback failed")

    def write_diagnostic(
        descriptor: int,
        payload: bytes,
        *,
        deadline: float,
        signal_controller: implementation.SignalController,
    ) -> None:
        assert descriptor == 42 and deadline <= 110.0
        diagnostic_events.append(
            (job.close.call_count, signal_controller is controller, payload)
        )
        # Model arrival while the optional pipe writer is outside its own defer.
        deliver_signal()

    monkeypatch.setattr(runner.subprocess, "run", taskkill)
    monkeypatch.setattr(diagnostic, "write_diagnostic", write_diagnostic)
    caught: BaseException | None = None
    result: int | None = None
    try:
        with runner.controlled_termination_signals() as controller:
            try:
                result = runner.run_command(
                    COMMAND, hard_deadline=110.0, signal_controller=controller
                )
            except (
                KeyboardInterrupt,
                runner.RunnerSignalInterrupt,
                runner.ProcessOwnershipError,
            ) as error:
                caught = error
    finally:
        sink.close()

    job.close.assert_called_once_with()
    if taskkill_status:
        assert isinstance(caught, implementation.ProcessOwnershipError)
        assert not caught.cleanup_proven
        notes = "\n".join(getattr(caught, "__notes__", []))
        assert "taskkill failed with exit 1: fallback failed" in notes
        assert "job termination failed" in notes
        assert diagnostic_events == []
        wait_tree.assert_not_called()
    else:
        wait_tree.assert_called_once_with(owner, deadline=110.0)
        assert diagnostic_events
        assert all(closed == 1 and same for closed, same, _ in diagnostic_events)
        assert b"job termination failed" in diagnostic_events[0][2]
        if interrupt is None:
            assert caught is None and result == runner.TIMEOUT_EXIT_CODE
        elif interrupt == signal.SIGINT:
            assert isinstance(caught, KeyboardInterrupt)
        else:
            assert isinstance(caught, implementation.RunnerSignalInterrupt)
            assert caught.signal_number == interrupt


@pytest.mark.parametrize("closed", [False, True])
@pytest.mark.parametrize("previous_cleanup_proven", [False, True])
@pytest.mark.usefixtures("memory_diagnostics")
def test_cleanup_notes_preserve_original_ownership_error_and_cause(
    monkeypatch: pytest.MonkeyPatch, closed: bool, previous_cleanup_proven: bool
) -> None:
    child = Mock()
    child.wait.side_effect = subprocess.TimeoutExpired("command", 4.0)
    owner = runner.OwnedProcess(child)
    original = implementation.ProcessOwnershipError("native cleanup failed")
    original.cleanup_proven = previous_cleanup_proven
    cause = OSError("native Job failure")
    original.__cause__ = cause

    def terminate(
        actual: implementation.OwnedProcess,
        *,
        deadline: float,
        diagnostic: Callable[[str], None],
    ) -> bool:
        assert actual is owner and deadline == 110.0
        diagnostic("saved termination detail")
        raise original

    monkeypatch.setattr(runner, "start_owned_process", Mock(return_value=owner))
    monkeypatch.setattr(runner, "terminate_owned_tree", terminate)
    close = Mock(return_value=closed)
    monkeypatch.setattr(runner, "close_owner", close)
    monkeypatch.setattr(runner.time, "monotonic", Mock(return_value=100.0))
    with pytest.raises(implementation.ProcessOwnershipError) as caught:
        runner.run_command(COMMAND, hard_deadline=110.0)
    assert caught.value is original and caught.value.__cause__ is cause
    assert not caught.value.cleanup_proven
    assert "saved termination detail" in original.__notes__
    close.assert_called_once()
