"""Execute the installed process contract with real disposable child processes."""

from __future__ import annotations

import errno
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from h2h_dev_tools import process


def _source_bootstrap(package: Path) -> str:
    return (
        "import runpy\nfrom pathlib import Path\n"
        f"package = Path({str(package)!r})\n"
        "runpy.run_path(str(package / 'source_loader.py'))['load_package'](\n"
        "    package, 'h2h_dev_tools')\n"
    )


@pytest.mark.parametrize("standard_output", [False, True])
@pytest.mark.parametrize(
    "poisoned_source_package", ["process.py", "_process_scope.py"], indirect=True
)
def test_full_diagnostic_pipe_preserves_completion_and_blocking_mode(
    tmp_path: Path, standard_output: bool, poisoned_source_package: Path
) -> None:
    """Also runs natively in Windows CI, where nonblocking support is pipe-only."""
    marker = tmp_path / "completed"
    descriptor = 1 if standard_output else 2
    script = _source_bootstrap(poisoned_source_package) + (
        "import os, sys\n"
        "from h2h_dev_tools.process import report_diagnostic\n"
        f"descriptor = {descriptor}\n"
        "os.set_blocking(descriptor, False)\n"
        "try:\n"
        "    while os.write(descriptor, b'x' * 8192): pass\n"
        "except BlockingIOError:\n    pass\n"
        "finally:\n    os.set_blocking(descriptor, True)\n"
        f"report_diagnostic('must not wait', standard_output={standard_output!r})\n"
        f"Path({str(marker)!r}).write_text(str(os.get_blocking(descriptor)))\n"
        "raise SystemExit(7)\n"
    )
    read_descriptor, write_descriptor = os.pipe()
    try:
        child = subprocess.Popen(
            (sys.executable, "-I", "-B", "-c", script),
            stdout=write_descriptor if standard_output else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL if standard_output else write_descriptor,
        )
        try:
            assert child.wait(timeout=5) == 7
            assert marker.read_text() == "True"
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
    finally:
        os.close(read_descriptor)
        os.close(write_descriptor)


@pytest.mark.skipif(
    os.name != "posix", reason="Exercises real POSIX owner cancellation"
)
@pytest.mark.parametrize("standard_output", [False, True])
@pytest.mark.parametrize("trigger", ["cancel", "timeout"])
@pytest.mark.parametrize(
    "poisoned_source_package", ["process.py", "_process_scope.py"], indirect=True
)
def test_full_diagnostic_pipe_cannot_delay_owned_cleanup(
    tmp_path: Path, standard_output: bool, trigger: str, poisoned_source_package: Path
) -> None:
    ready = tmp_path / "ready"
    completed = tmp_path / "completed"
    child = (
        "import os, signal, time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"descriptor = {1 if standard_output else 2}\n"
        "os.set_blocking(descriptor, False)\n"
        "try:\n"
        "    while True: os.write(descriptor, b'x' * 8192)\n"
        "except BlockingIOError:\n    pass\n"
        "finally:\n    os.set_blocking(descriptor, True)\n"
        f"Path({str(ready)!r}).write_text(str(os.getpgrp()))\n"
        "time.sleep(60)\n"
    )
    script = _source_bootstrap(poisoned_source_package) + (
        "import sys, time\n"
        "from h2h_dev_tools import process\n"
        f"phase = process.ProcessCommand('backpressure', (sys.executable, '-c', {child!r}), Path.cwd())\n"
        "try:\n"
        "    status = process.run_command(phase, termination_reserve_seconds=1, "
        f"hard_deadline={'time.monotonic() + 3' if trigger == 'timeout' else 'None'})\n"
        "except process.RunnerSignalInterrupt as error:\n"
        "    status = 128 + error.signal_number\n"
        f"Path({str(completed)!r}).write_text(str(status))\n"
        "raise SystemExit(status)\n"
    )
    read_descriptor, write_descriptor = os.pipe()
    group: int | None = None
    try:
        owner = subprocess.Popen(
            (sys.executable, "-I", "-B", "-c", script),
            stdout=write_descriptor if standard_output else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL if standard_output else write_descriptor,
            cwd=tmp_path,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 5
            while not ready.exists():
                assert owner.poll() is None
                assert time.monotonic() < deadline, "The child did not fill its pipe"
                time.sleep(0.01)
            group = int(ready.read_text())
            if trigger == "cancel":
                owner.send_signal(signal.SIGTERM)
            expected = 143 if trigger == "cancel" else process.TIMEOUT_EXIT_CODE
            assert owner.wait(timeout=5) == expected
            assert completed.read_text() == str(expected)
            with pytest.raises(ProcessLookupError):
                os.killpg(group, 0)
            assert os.get_blocking(write_descriptor)
        finally:
            for identifier in (group, owner.pid):
                if identifier is not None:
                    try:
                        os.killpg(identifier, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            owner.wait(timeout=5)
    finally:
        os.close(read_descriptor)
        os.close(write_descriptor)


@pytest.mark.parametrize("standard_output", [False, True])
@pytest.mark.parametrize("status", [0, 143])
@pytest.mark.parametrize("failure", ["closed-pipe", "closed-fd"])
@pytest.mark.parametrize(
    "poisoned_source_package", ["process.py", "_process_scope.py"], indirect=True
)
def test_failed_diagnostic_descriptor_preserves_interpreter_exit_status(
    tmp_path: Path,
    standard_output: bool,
    status: int,
    failure: str,
    poisoned_source_package: Path,
) -> None:
    marker = tmp_path / "completed"
    script = _source_bootstrap(poisoned_source_package) + (
        "import os\n"
        "from h2h_dev_tools.process import report_diagnostic\n"
        + (
            f"os.close({1 if standard_output else 2})\n"
            if failure == "closed-fd"
            else ""
        )
        + f"report_diagnostic('diagnostic', standard_output={standard_output!r})\n"
        f"Path({str(marker)!r}).touch()\n"
        f"raise SystemExit({status})\n"
    )
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    with os.fdopen(write_fd, "wb", buffering=0) as closed_pipe:
        result = subprocess.run(
            (sys.executable, "-I", "-B", "-c", script),
            stdout=closed_pipe if standard_output else subprocess.PIPE,
            stderr=subprocess.PIPE if standard_output else closed_pipe,
            check=False,
            timeout=10,
        )
    assert marker.exists(), "A diagnostic failure interrupted the command"
    assert result.returncode == status, result.stderr or result.stdout


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
        owner: process.OwnedProcess | None = None
        try:
            with controller.defer():
                owner = process.start_owned_process(
                    command, deadline=deadline, signal_controller=controller
                )
            assert owner.process.wait(timeout=5) == 0
            assert process.wait_for_owned_tree_exit(owner, deadline=deadline)
        finally:
            with controller.defer():
                if owner is not None:
                    process.terminate_owned_tree(owner, deadline=deadline)
                    assert process.close_owner(owner, deadline=deadline)
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


@pytest.mark.skipif(os.name != "posix", reason="Real POSIX parent ledger contention")
@pytest.mark.parametrize("status", [0, 7])
@pytest.mark.parametrize("bounded", [False, True])
def test_normal_exit_closes_parent_obligation_with_remaining_cleanup_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int, bounded: bool
) -> None:
    import threading

    from h2h_dev_tools import _process_scope as scope_module

    parent = scope_module.PosixScope.create(deadline=time.monotonic() + 3)
    monkeypatch.setenv(scope_module.ENVIRONMENT_KEY, parent.capability.encode())
    original_wait = process.wait_for_owned_tree_exit
    owners: list[process.OwnedProcess] = []
    locked = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def contend() -> None:
        try:
            with parent.capability.locked(deadline=time.monotonic() + 3):
                locked.set()
                # Longer than the normal-exit quick poll, within the original
                # cleanup budget. This is real flock contention, not a clock mock.
                release.wait(timeout=0.6)
        except BaseException as error:
            errors.append(error)
            locked.set()

    contender = threading.Thread(target=contend)

    def wait_then_contend(owner: process.OwnedProcess, *, deadline: float) -> bool:
        owners.append(owner)
        assert original_wait(owner, deadline=deadline)
        contender.start()
        assert locked.wait(timeout=3)
        assert not errors
        return True

    monkeypatch.setattr(process, "wait_for_owned_tree_exit", wait_then_contend)
    command = process.ProcessCommand(
        "parent ledger contention",
        (sys.executable, "-c", f"raise SystemExit({status})"),
        tmp_path,
    )
    started = time.monotonic()
    try:
        assert (
            process.run_command(
                command,
                hard_deadline=started + 8 if bounded else None,
                termination_reserve_seconds=2,
            )
            == status
        )
        assert 0.5 <= time.monotonic() - started < 8
        assert len(owners) == 1
        scope = owners[0].posix_scope
        assert scope is not None
        assert not scope.capability.directory.exists()
        assert not parent.has_pending(deadline=time.monotonic() + 1)
    finally:
        release.set()
        if contender.ident is not None:
            contender.join(timeout=3)
            assert not contender.is_alive()
        assert not errors
        # The OLD negative retains its proven-empty scope. After removing only
        # our synthetic lock, independently prove and release that obligation.
        for owner in owners:
            assert owner.process.poll() == status
            with pytest.raises(ProcessLookupError):
                os.killpg(owner.process.pid, 0)
            scope = owner.posix_scope
            assert scope is not None
            if scope.capability.directory.exists():
                assert process.terminate_owned_tree(
                    owner, deadline=time.monotonic() + 2
                )
                scope.close(deadline=time.monotonic() + 2)
        assert not parent.has_pending(deadline=time.monotonic() + 1)
        # This parent is a synthetic ledger: it never launches its own group.
        parent.proven_empty = True
        parent.close(deadline=time.monotonic() + 1)
        assert not parent.capability.directory.exists()


@pytest.mark.skipif(os.name != "posix", reason="Real POSIX parent ledger contention")
def test_owner_close_deadline_retains_responsibility_until_lock_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fcntl

    from h2h_dev_tools import _process_scope as scope_module

    deadline = time.monotonic() + 3
    parent: scope_module.PosixScope | None = None
    owner: process.OwnedProcess | None = None
    with process.controlled_termination_signals() as controller:
        try:
            with controller.defer():
                parent = scope_module.PosixScope.create(deadline=deadline)
                monkeypatch.setenv(
                    scope_module.ENVIRONMENT_KEY, parent.capability.encode()
                )
                owner = process.start_owned_process(
                    process.ProcessCommand(
                        "bounded close", (sys.executable, "-c", "pass"), tmp_path
                    ),
                    deadline=deadline,
                    signal_controller=controller,
                )
            scope = owner.posix_scope
            assert scope is not None
            assert owner.process.wait(timeout=3) == 0
            messages: list[str] = []
            assert not process.close_owner(
                owner, deadline=time.monotonic() + 1, diagnostic=messages.append
            )
            assert "Unproven process scope retained" in messages.pop()
            assert parent.has_pending(deadline=time.monotonic() + 1)
            assert process.wait_for_owned_tree_exit(
                owner, deadline=time.monotonic() + 1
            )
            with (parent.capability.directory / "lock").open("rb") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                started = time.monotonic()
                assert not process.close_owner(
                    owner, deadline=started + 0.04, diagnostic=messages.append
                )
                assert time.monotonic() - started < 0.5
                assert "Process scope lock deadline" in messages.pop()
                assert scope.proven_empty
                assert scope.capability.directory.is_dir()
                assert (
                    parent.capability.directory / "pending" / scope.capability.token
                ).is_file()
            assert process.close_owner(owner, deadline=time.monotonic() + 1)
            assert not scope.capability.directory.exists()
            assert not parent.has_pending(deadline=time.monotonic() + 1)
            assert not messages
        finally:
            with controller.defer():
                if owner is not None:
                    scope = owner.posix_scope
                    assert scope is not None
                    if scope.capability.directory.exists():
                        assert process.terminate_owned_tree(owner, deadline=deadline)
                        assert process.close_owner(owner, deadline=deadline)
                    assert owner.process.poll() is not None
                    with pytest.raises(ProcessLookupError):
                        os.killpg(owner.process.pid, 0)
                if parent is not None:
                    assert not parent.has_pending(deadline=deadline)
                    # This exact fixture ledger never launches its own process group.
                    parent.proven_empty = True
                    parent.close(deadline=deadline)
                    assert not parent.capability.directory.exists()


def _observe_exited_unreaped(child: subprocess.Popen[bytes], deadline: float) -> None:
    status = None
    while status is None:
        status = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
        assert time.monotonic() < deadline, "Native child did not exit"
        if status is None:
            time.sleep(0.001)
    assert status.si_pid == child.pid
    assert status.si_code == os.CLD_EXITED and status.si_status == 0
    assert child.returncode is None


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="XNU reports EPERM when a process group is exited but unreaped",
)
@pytest.mark.parametrize("edge", ["term", "kill"])
def test_native_signal_eperm_waits_for_reap_and_complete_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, edge: str
) -> None:
    ready, release = tmp_path / "ready", tmp_path / "release"
    child = (
        "import signal,sys,time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "Path(sys.argv[1]).touch()\n"
        "while not Path(sys.argv[2]).exists(): time.sleep(.001)\n"
        if edge == "kill"
        else "pass"
    )
    deadline = time.monotonic() + 6
    real_killpg = os.killpg
    observations: list[tuple[int, int]] = []
    with process.controlled_termination_signals() as controller:
        owner: process.OwnedProcess | None = None
        try:
            with controller.defer():
                owner = process.start_owned_process(
                    process.ProcessCommand(
                        "native signal exit barrier",
                        (
                            sys.executable,
                            "-I",
                            "-S",
                            "-c",
                            child,
                            str(ready),
                            str(release),
                        ),
                        tmp_path,
                    ),
                    deadline=deadline,
                    signal_controller=controller,
                )
            scope = owner.posix_scope
            assert scope is not None
            if edge == "term":
                _observe_exited_unreaped(owner.process, deadline)
            else:
                while not ready.exists():
                    assert time.monotonic() < deadline
                    time.sleep(0.001)

            def observe(group: int, number: int) -> None:
                assert group == owner.process.pid
                if edge == "kill" and number == signal.SIGKILL:
                    # The last real group probe observed a live child. Make it
                    # exit here, then preserve its actual wait status so the
                    # following real SIGKILL observes XNU's native EPERM race.
                    release.touch()
                    _observe_exited_unreaped(owner.process, deadline)
                try:
                    real_killpg(group, number)
                except OSError as error:
                    assert error.errno is not None
                    observations.append((number, error.errno))
                    raise
                else:
                    observations.append((number, 0))

            with monkeypatch.context() as boundary:
                boundary.setattr(os, "killpg", observe)
                assert process.terminate_owned_tree(owner, deadline=deadline)
            expected_signal = signal.SIGTERM if edge == "term" else signal.SIGKILL
            assert (expected_signal, errno.EPERM) in observations
            assert observations[-1] == (0, errno.ESRCH)
            assert owner.process.returncode == 0
            assert scope.proven_empty
            assert not scope.has_pending(deadline=deadline)
            with pytest.raises(ChildProcessError):
                os.waitpid(owner.process.pid, os.WNOHANG)
        finally:
            with controller.defer():
                # The OLD regression control also cleans its real exited child,
                # using the same absolute deadline and the existing absence oracle.
                if owner is not None:
                    scope = owner.posix_scope
                    assert scope is not None
                    release.touch()
                    if not scope.proven_empty:
                        assert process.wait_for_owned_tree_exit(
                            owner, deadline=deadline
                        )
                    assert process.close_owner(owner, deadline=deadline)
        assert not scope.capability.directory.exists()
        if scope.parent is not None:
            with scope.parent.locked(deadline=deadline):
                assert not (
                    scope.parent.directory / "pending" / scope.capability.token
                ).exists()


@pytest.mark.skipif(os.name != "posix", reason="Real signal at owner handoff")
@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize("boundary", ["launch", "cleanup"])
@pytest.mark.parametrize(
    "fixture",
    [
        "silent",
        "close",
        pytest.param(
            "native-term",
            marks=pytest.mark.skipif(sys.platform != "darwin", reason="XNU EPERM"),
        ),
        pytest.param(
            "native-kill",
            marks=pytest.mark.skipif(sys.platform != "darwin", reason="XNU EPERM"),
        ),
    ],
)
def test_owner_fixtures_clean_up_when_launch_handoff_is_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    signum: int,
    fixture: str,
    boundary: str,
) -> None:
    from h2h_dev_tools import _process_scope as scope_module

    original = process.start_owned_process
    close = process.close_owner
    create = scope_module.PosixScope.create
    close_scope = scope_module.PosixScope.close
    owners: list[tuple[process.OwnedProcess, float]] = []
    closed: list[tuple[process.OwnedProcess, float, bool]] = []
    scopes: list[tuple[scope_module.PosixScope, float]] = []
    pending: list[int | None] = []
    controllers: list[process.SignalController | None] = []
    parents_closed: list[tuple[scope_module.PosixScope, float]] = []

    def send_signal(controller: process.SignalController | None) -> None:
        os.kill(os.getpid(), signum)
        assert controller is not None and controller.defer_depth > 0
        pending.append(controller.pending_signal)

    def observe_scope(*, deadline: float) -> scope_module.PosixScope:
        scope = create(deadline=deadline)
        scopes.append((scope, deadline))
        return scope

    def interrupt(
        phase: process.ProcessCommand,
        *,
        deadline: float,
        signal_controller: process.SignalController | None,
    ) -> process.OwnedProcess:
        owner = original(phase, deadline=deadline, signal_controller=signal_controller)
        owners.append((owner, deadline))
        controllers.append(signal_controller)
        if boundary == "launch":
            send_signal(signal_controller)
        return owner

    def record_close(
        owner: process.OwnedProcess,
        *,
        deadline: float,
        diagnostic: Callable[[str], None] | None = None,
    ) -> bool:
        if boundary == "cleanup" and fixture != "close":
            send_signal(controllers[0])
        result = close(owner, deadline=deadline, diagnostic=diagnostic)
        closed.append((owner, deadline, result))
        return result

    def record_parent_close(scope: scope_module.PosixScope, *, deadline: float) -> None:
        if scopes and scope is scopes[0][0]:
            if boundary == "cleanup":
                send_signal(controllers[0])
            close_scope(scope, deadline=deadline)
            parents_closed.append((scope, deadline))
        else:
            close_scope(scope, deadline=deadline)

    monkeypatch.setattr(process, "start_owned_process", interrupt)
    monkeypatch.setattr(process, "close_owner", record_close)
    if fixture == "close":
        monkeypatch.setattr(scope_module.PosixScope, "create", observe_scope)
        monkeypatch.setattr(scope_module.PosixScope, "close", record_parent_close)
    expected = (
        KeyboardInterrupt if signum == signal.SIGINT else process.RunnerSignalInterrupt
    )
    try:
        # A regressed controller=None fixture must raise a typed interruption,
        # not deliver default SIGTERM to the pytest process before rescue.
        with process.controlled_termination_signals(), pytest.raises(expected):
            if fixture == "silent":
                test_low_level_launch_does_not_pollute_callers_output(tmp_path, capsys)
            elif fixture == "close":
                test_owner_close_deadline_retains_responsibility_until_lock_release(
                    tmp_path, monkeypatch
                )
            else:
                test_native_signal_eperm_waits_for_reap_and_complete_cleanup(
                    tmp_path, monkeypatch, fixture.removeprefix("native-")
                )
        assert len(owners) == 1
        owner, deadline = owners[0]
        if fixture == "close" and boundary == "cleanup":
            # The normal body deliberately rejects two closes before proving
            # and closing the child; cleanup then closes its synthetic parent.
            assert [result for _, _, result in closed] == [False, False, True]
            assert all(item is owner and end <= deadline for item, end, _ in closed)
        else:
            assert closed == [(owner, deadline, True)]
        assert owner.process.poll() is not None
        assert owner.posix_scope is not None
        assert not owner.posix_scope.capability.directory.exists()
        with pytest.raises(ProcessLookupError):
            os.killpg(owner.process.pid, 0)
        if fixture == "close":
            assert len(scopes) == 2 and scopes[1][0] is owner.posix_scope
            parent = scopes[0][0]
            assert owner.posix_scope.parent == parent.capability
            assert parents_closed == [(parent, scopes[0][1])]
            assert not parent.capability.directory.exists()
        assert pending == [signum]
    finally:
        # Failure-only rescue cannot satisfy the assertions above. Retain the
        # original deadlines, including this fixture's exact synthetic parent.
        failure = sys.exception()
        try:
            with process.controlled_termination_signals() as controller:
                with controller.defer():
                    for owner, deadline in owners:
                        if not any(
                            item is owner and result for item, _, result in closed
                        ):
                            empty = process.terminate_owned_tree(
                                owner, deadline=deadline
                            )
                            released = close(owner, deadline=deadline)
                            assert empty and released
                    if fixture == "close" and scopes:
                        parent, parent_deadline = scopes[0]
                        if parent.capability.directory.exists():
                            assert not parent.has_pending(deadline=parent_deadline)
                            # Only this recorded ledger has no launched group.
                            parent.proven_empty = True
                            close_scope(parent, deadline=parent_deadline)
        except BaseException as rescue_error:
            if failure is None:
                raise
            failure.add_note(
                f"Owner boundary control rescue also failed: {rescue_error}"
            )
