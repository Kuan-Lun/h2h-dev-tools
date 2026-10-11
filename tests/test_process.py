"""Shared process ownership: deadlines, signals, start gates and empty-tree proof."""

from __future__ import annotations

import ctypes
import io
import signal
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast
from unittest.mock import ANY, Mock, call

import pytest

from h2h_dev_tools import process as implementation

runner: ModuleType = implementation
COMMAND = implementation.ProcessCommand(
    "regression command", (sys.executable, "-c", "pass"), Path.cwd(), {}
)


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
def test_timeout_checks_owned_tree_termination(
    monkeypatch: pytest.MonkeyPatch,
    terminated: bool,
    with_windows_job: bool,
) -> None:
    process = Mock()
    process.wait.side_effect = subprocess.TimeoutExpired(cmd="pytest", timeout=3.0)
    job = Mock() if with_windows_job else None
    owner = runner.OwnedProcess(process, job)
    start = Mock(return_value=owner)
    terminate = Mock(return_value=terminated)
    monotonic = Mock(side_effect=(100.0, 101.0, 102.0))
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
    terminate.assert_called_once_with(owner, deadline=110.0)
    if job is not None:
        job.close.assert_called_once_with()


@pytest.mark.parametrize("with_windows_job", [False, True])
def test_phase_wait_subtracts_ownership_overhead_from_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch,
    with_windows_job: bool,
) -> None:
    process = Mock()
    process.wait.return_value = 0
    job = Mock() if with_windows_job else None
    owner = runner.OwnedProcess(process, job)
    start = Mock(return_value=owner)
    monotonic = Mock(side_effect=(100.0, 103.0, 103.5, 104.0))
    wait_for_tree = Mock(return_value=True)
    monkeypatch.setattr(runner, "start_owned_process", start)
    monkeypatch.setattr(runner, "wait_for_owned_tree_exit", wait_for_tree)
    monkeypatch.setattr(runner.time, "monotonic", monotonic)

    assert runner.run_command(COMMAND, hard_deadline=110.0) == 0
    process.wait.assert_called_once_with(timeout=1.0)
    start.assert_called_once_with(COMMAND, deadline=110.0, signal_controller=ANY)
    wait_for_tree.assert_called_once_with(owner, deadline=103.75)
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


def test_bounded_interrupt_cleanup_never_extends_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = Mock()
    process.wait.side_effect = KeyboardInterrupt
    owner = runner.OwnedProcess(process)
    terminate = Mock(return_value=True)
    monkeypatch.setattr(runner, "start_owned_process", Mock(return_value=owner))
    monkeypatch.setattr(runner, "terminate_owned_tree", terminate)
    monkeypatch.setattr(
        runner.time,
        "monotonic",
        Mock(side_effect=(100.0, 101.0, 109.0)),
    )

    with pytest.raises(KeyboardInterrupt):
        runner.run_command(
            COMMAND,
            hard_deadline=110.0,
            signal_controller=runner.SignalController(),
        )

    terminate.assert_called_once_with(owner, deadline=110.0)


def test_windows_signal_during_timeout_cleanup_waits_for_empty_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = runner.SignalController()
    process = Mock()
    process.wait.side_effect = subprocess.TimeoutExpired("pytest", 3.0)
    owner = runner.OwnedProcess(process)

    def terminate(_owner: object, *, deadline: float) -> bool:
        assert deadline == 110.0
        controller.receive(signal.SIGINT)
        controller.receive(21)
        return True

    monkeypatch.setattr(runner, "start_owned_process", Mock(return_value=owner))
    monkeypatch.setattr(runner, "terminate_owned_tree", terminate)
    monkeypatch.setattr(
        runner.time,
        "monotonic",
        Mock(side_effect=(100.0, 101.0)),
    )

    with pytest.raises(KeyboardInterrupt):
        runner.run_command(
            COMMAND,
            hard_deadline=110.0,
            signal_controller=controller,
        )


def test_windows_failed_cleanup_cannot_publish_interrupt_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = runner.SignalController()
    process = Mock()
    process.wait.side_effect = subprocess.TimeoutExpired("pytest", 3.0)
    owner = runner.OwnedProcess(process)

    def terminate(_owner: object, *, deadline: float) -> bool:
        assert deadline == 110.0
        controller.receive(signal.SIGINT)
        return False

    monkeypatch.setattr(runner, "start_owned_process", Mock(return_value=owner))
    monkeypatch.setattr(runner, "terminate_owned_tree", terminate)
    monkeypatch.setattr(
        runner.time,
        "monotonic",
        Mock(side_effect=(100.0, 101.0)),
    )

    with pytest.raises(runner.ProcessOwnershipError):
        runner.run_command(
            COMMAND,
            hard_deadline=110.0,
            signal_controller=controller,
        )


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
        Mock(side_effect=(100.0, 101.0, 102.0)),
    )

    with pytest.raises(runner.ProcessOwnershipError):
        runner.run_command(COMMAND, hard_deadline=110.0)

    terminate.assert_called_once_with(owner, deadline=110.0)


@pytest.mark.parametrize("terminated", [False, True])
@pytest.mark.parametrize("closed", [False, True])
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
    taskkill.assert_called_once_with(process, deadline=110.0)
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
        Mock(side_effect=(100.0, 101.0, 102.0)),
    )

    with pytest.raises(runner.ProcessOwnershipError):
        runner.run_command(COMMAND, hard_deadline=110.0)


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
        Mock(side_effect=(100.0, 101.0, 102.0)),
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
        call.terminate(owner, deadline=expected_deadline),
        call.close(),
    ]


@pytest.mark.parametrize("cleanup_error", [False, OSError("termination failed")])
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

    terminate.assert_called_once_with(owner, deadline=106.0)
    job.close.assert_called_once_with()


@pytest.mark.parametrize(
    "diagnostic_error",
    [BrokenPipeError, OSError, ValueError, RuntimeError],
)
@pytest.mark.parametrize("interrupted", [False, True])
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
    diagnostic_stream = Mock()
    diagnostic_stream.write.side_effect = diagnostic_error("diagnostic unavailable")
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

    terminate.assert_called_once_with(owner, deadline=106.0 if interrupted else 110.0)
    job.close.assert_called_once_with()


@pytest.mark.parametrize("standard_output", [False, True])
@pytest.mark.parametrize("binary_capture", [False, True])
def test_diagnostics_preserve_memory_only_capture(
    monkeypatch: pytest.MonkeyPatch, standard_output: bool, binary_capture: bool
) -> None:
    binary = io.BytesIO()
    stream = (
        io.TextIOWrapper(binary, encoding="utf-8") if binary_capture else io.StringIO()
    )
    with monkeypatch.context() as capture:
        capture.setattr(runner.sys, "stdout" if standard_output else "stderr", stream)
        runner.report_diagnostic("清理已完成 ✓", standard_output=standard_output)
    if binary_capture:
        assert binary.getvalue().decode("utf-8") == "清理已完成 ✓\n"
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


@pytest.mark.parametrize("blocking", [False, True])
@pytest.mark.parametrize("pipe_full", [False, True])
@pytest.mark.parametrize("encoding", ["utf-8", "utf-16-le"])
def test_windows_pipe_diagnostics_restore_flags_without_buffered_fallback(
    monkeypatch: pytest.MonkeyPatch, blocking: bool, pipe_full: bool, encoding: str
) -> None:
    sink = Mock(encoding=encoding, errors="strict")
    sink.fileno.return_value = 42
    payload = "清理已完成 ✓\n".encode(encoding)
    written: list[bytes] = []

    def write(descriptor: int, value: bytes | memoryview) -> int:
        assert descriptor == 42
        if pipe_full:
            raise BlockingIOError("pipe reader is not consuming")
        # A short write must preserve the remaining Unicode bytes exactly.
        chunk = bytes(value[:3])
        written.append(chunk)
        return len(chunk)

    native = SimpleNamespace(
        name="nt",
        fstat=Mock(return_value=SimpleNamespace(st_mode=stat.S_IFIFO)),
        get_blocking=Mock(return_value=blocking),
        set_blocking=Mock(),
        write=Mock(side_effect=write),
    )
    monkeypatch.setattr(runner, "os", native)
    monkeypatch.setattr(runner.sys, "stderr", sink)

    runner.report_diagnostic("清理已完成 ✓")

    assert native.set_blocking.call_args_list == [call(42, False), call(42, blocking)]
    assert b"".join(written) == (b"" if pipe_full else payload)
    native.write.assert_called()
    sink.write.assert_not_called()
    sink.flush.assert_not_called()


class _NativeDiagnosticCapture(io.TextIOWrapper):
    def fileno(self) -> int:
        return 42


@pytest.mark.parametrize("mode", [stat.S_IFREG, stat.S_IFCHR])
def test_windows_regular_and_console_diagnostics_keep_native_text_encoding(
    monkeypatch: pytest.MonkeyPatch, mode: int
) -> None:
    buffer = io.BytesIO()
    sink = _NativeDiagnosticCapture(buffer, encoding="utf-16-le")
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

    assert buffer.getvalue().decode("utf-16-le") == "清理已完成 ✓\n"
    native.get_blocking.assert_not_called()
    native.set_blocking.assert_not_called()
    native.write.assert_not_called()
    sink.close()


@pytest.mark.parametrize("taskkill_status", [0, 1])
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
    diagnostic_stream = Mock()
    diagnostic_stream.write.side_effect = BrokenPipeError("diagnostic pipe closed")
    monkeypatch.setattr(runner.sys, "stderr", diagnostic_stream)
    monkeypatch.setattr(runner, "start_owned_process", Mock(return_value=owner))
    monkeypatch.setattr(runner.subprocess, "run", taskkill)
    monkeypatch.setattr(runner, "wait_for_owned_tree_exit", wait_tree)
    monkeypatch.setattr(runner.time, "monotonic", Mock(return_value=100.0))

    if taskkill_status:
        with pytest.raises(runner.ProcessOwnershipError):
            runner.run_command(COMMAND, hard_deadline=110.0)
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
