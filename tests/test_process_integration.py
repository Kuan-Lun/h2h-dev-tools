"""Execute the installed process contract with real disposable child processes."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from h2h_dev_tools import process


@pytest.mark.parametrize("status", [0, 7])
def test_command_preserves_cwd_environment_output_and_exit_status(
    tmp_path: Path, status: int
) -> None:
    script = (
        "import os,pathlib,sys; "
        "print(pathlib.Path.cwd().name); print(os.environ['TOOL_TEST_VALUE']); "
        "print('diagnostic',file=sys.stderr); sys.exit(int(sys.argv[1]))"
    )
    with (
        (tmp_path / "stdout").open("w+b") as stdout,
        (tmp_path / "stderr").open("w+b") as stderr,
    ):
        command = process.ProcessCommand(
            "contract",
            (sys.executable, "-c", script, str(status)),
            tmp_path,
            {**os.environ, "TOOL_TEST_VALUE": "explicit"},
            stdout,
            stderr,
        )
        with process.controlled_termination_signals() as controller:
            result = process.run_command(
                command,
                hard_deadline=time.monotonic() + 10,
                signal_controller=controller,
            )
        assert result == status
        stdout.seek(0)
        stderr.seek(0)
        assert stdout.read().decode().splitlines() == [tmp_path.name, "explicit"]
        assert stderr.read().decode().splitlines() == ["diagnostic"]


def test_low_level_launch_does_not_pollute_callers_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    deadline = time.monotonic() + 10
    command = process.ProcessCommand(
        "silent worker",
        (sys.executable, "-c", "pass"),
        tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    with process.controlled_termination_signals() as controller:
        with controller.defer():
            owner = process.start_owned_process(
                command, deadline=deadline, signal_controller=controller
            )
        try:
            assert owner.process.wait(timeout=5) == 0
            assert process.wait_for_owned_tree_exit(owner, deadline=deadline)
        finally:
            process.terminate_owned_tree(owner, deadline=deadline)
            assert process.close_owner(owner)
    assert capsys.readouterr().out == ""


def test_short_deadline_still_reserves_time_for_forced_termination(
    tmp_path: Path,
) -> None:
    command = process.ProcessCommand(
        "deadline",
        (sys.executable, "-c", "import time; time.sleep(60)"),
        tmp_path,
    )
    with process.controlled_termination_signals() as controller:
        assert (
            process.run_command(
                command,
                hard_deadline=time.monotonic() + 1,
                termination_reserve_seconds=0.5,
                signal_controller=controller,
            )
            == process.TIMEOUT_EXIT_CODE
        )


def test_missing_executable_fails_as_an_ownership_error(tmp_path: Path) -> None:
    command = process.ProcessCommand("missing", (str(tmp_path / "missing"),), tmp_path)
    if os.name == "nt":
        # Windows launches a start-gated supervisor first; command exec failure
        # is then reported by that owned child after the gate opens.
        assert (
            process.run_command(command, hard_deadline=time.monotonic() + 10)
            == process.TERMINATION_FAILED_EXIT_CODE
        )
    else:
        with pytest.raises(process.ProcessOwnershipError):
            process.start_owned_process(
                command, deadline=time.monotonic() + 5, signal_controller=None
            )


@pytest.mark.skipif(
    os.name == "nt", reason="Uses real POSIX session and process signals"
)
@pytest.mark.parametrize("signal_number", [signal.SIGINT, signal.SIGTERM])
def test_direct_api_defers_signal_until_spawned_process_has_an_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    signal_number: int,
) -> None:
    command = process.ProcessCommand(
        "signal during ownership handoff",
        (sys.executable, "-c", "import time; time.sleep(60)"),
        tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    launch = process._start_posix_process
    children: list[subprocess.Popen[bytes]] = []
    previous_handler = signal.getsignal(signal_number)

    def unprotected_signal(_signal_number: int, _frame: object) -> None:
        raise AssertionError("run_command did not install its signal controller")

    # An implementation missing automatic handlers must fail this test rather
    # than deliver a default SIGTERM to the entire pytest runner.
    signal.signal(signal_number, unprotected_signal)

    def launch_then_signal(phase: process.ProcessCommand) -> subprocess.Popen[bytes]:
        child = launch(phase)
        children.append(child)
        # Interrupt precisely after OS process creation but before returning its
        # handle to start_owned_process/run_command. The API must close this gap.
        os.kill(os.getpid(), signal_number)
        return child

    monkeypatch.setattr(process, "_start_posix_process", launch_then_signal)
    try:
        expected = (
            KeyboardInterrupt
            if signal_number == signal.SIGINT
            else process.RunnerSignalInterrupt
        )
        with pytest.raises(expected) as caught:
            process.run_command(command, hard_deadline=time.monotonic() + 10)
        if isinstance(caught.value, process.RunnerSignalInterrupt):
            assert caught.value.signal_number == signal_number
        assert len(children) == 1
        assert children[0].poll() is not None
        with pytest.raises(ProcessLookupError):
            os.killpg(children[0].pid, 0)
        assert signal.getsignal(signal_number) is unprotected_signal
    finally:
        signal.signal(signal_number, previous_handler)
        # Keep the regression safe even if an implementation leaks the child.
        for child in children:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)
