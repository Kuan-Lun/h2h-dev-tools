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


def test_windows_supervisor_cannot_run_python_startup_hooks_before_ownership(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "startup-ran"
    (tmp_path / "sitecustomize.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "Path(os.environ['STARTUP_MARKER']).touch()\n",
        encoding="utf-8",
    )
    # Even with site disabled, PYTHONPATH must not inject modules imported by
    # the supervisor itself before the start gate is reached.
    (tmp_path / "ctypes.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "Path(os.environ['STARTUP_MARKER']).touch()\n"
        "raise RuntimeError('unowned environment module ran')\n",
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "PYTHONPATH": str(tmp_path),
        "STARTUP_MARKER": str(marker),
    }
    phase = process.ProcessCommand(
        "startup isolation", (sys.executable, "-c", "pass"), tmp_path, environment
    )
    command, launch_environment = process._windows_supervisor_launch(phase)
    # An unopened gate exits without running the command. This also works on
    # POSIX: the Windows supervisor refuses that platform, after Python startup.
    result = subprocess.run(
        command,
        cwd=tmp_path,
        env=launch_environment,
        input=b"",
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == process.TERMINATION_FAILED_EXIT_CODE, result.stderr
    assert not marker.exists(), "Python startup ran before Job Object assignment"

    # Isolation belongs to the supervisor, not the target: preserve the exact
    # original Python environment for the command admitted after assignment.
    assert launch_environment["PYTHONPATH"] == str(tmp_path)
    assert process.run_command(phase, hard_deadline=time.monotonic() + 10) == 0
    assert marker.exists()


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
