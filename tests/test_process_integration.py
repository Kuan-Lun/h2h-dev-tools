"""Execute the installed process contract with real disposable child processes."""

from __future__ import annotations

import errno
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from h2h_dev_tools import process


@pytest.mark.skipif(os.name != "posix", reason="POSIX retained waitable identity")
@pytest.mark.parametrize("exit_code", [0, 7])
@pytest.mark.parametrize("wait_timeout", [None, 3])
def test_wait_retains_group_identity_until_cleanup_and_retires_all_signals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int,
    wait_timeout: float | None,
) -> None:
    deadline = time.monotonic() + 10
    command = process.ProcessCommand(
        "retained group identity",
        (sys.executable, "-I", "-S", "-c", f"raise SystemExit({exit_code})"),
        tmp_path,
    )
    with process.controlled_termination_signals() as controller:
        owner = process.start_owned_process(
            command, deadline=deadline, signal_controller=controller
        )
        try:
            assert owner.process.wait(timeout=wait_timeout) == exit_code
            receipt = os.waitid(
                os.P_PID, owner.process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT
            )
            assert receipt is not None and receipt.si_pid == owner.process.pid
            assert process.wait_for_owned_tree_exit(owner, deadline=deadline)
            with pytest.raises(ChildProcessError):
                os.waitid(
                    os.P_PID, owner.process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT
                )
            with monkeypatch.context() as boundary:

                def forbid_reused_group(group: int, number: int) -> None:
                    pytest.fail(f"Retired PGID was used again: {group}, {number}")

                boundary.setattr(os, "killpg", forbid_reused_group)
                assert process.terminate_owned_tree(owner, deadline=deadline)
                assert owner.process.poll() == exit_code
            assert process.close_owner(owner, deadline=deadline)
        finally:
            scope = owner.posix_scope
            if scope is not None and scope.capability.directory.exists():
                with controller.defer():
                    assert process.terminate_owned_tree(owner, deadline=deadline)
                    assert process.close_owner(owner, deadline=deadline)


@pytest.mark.skipif(os.name != "posix", reason="POSIX blocking wait identity")
@pytest.mark.parametrize("stopped", [False, True])
def test_unbounded_wait_allows_concurrent_group_signals_and_canonical_retirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stopped: bool
) -> None:
    from h2h_dev_tools._process_posix import PinnedProcess

    deadline = time.monotonic() + 10
    script = "import os,signal,time; "
    if stopped:
        script += "os.kill(os.getpid(), signal.SIGSTOP); "
    script += "time.sleep(30)"
    command = process.ProcessCommand(
        "blocking waiter", (sys.executable, "-I", "-S", "-c", script), tmp_path
    )
    entered = threading.Event()
    original_waitid = os.waitid
    errors: list[BaseException] = []
    results: list[int] = []

    with process.controlled_termination_signals() as controller:
        owner = process.start_owned_process(
            command, deadline=deadline, signal_controller=controller
        )
        assert isinstance(owner.process, PinnedProcess)

        def observe(kind: int, pid: int, options: int) -> Any:
            if pid == owner.process.pid and not options & os.WNOHANG:
                entered.set()
            return original_waitid(kind, pid, options)

        def wait() -> None:
            try:
                results.append(owner.process.wait())
            except BaseException as error:
                errors.append(error)

        monkeypatch.setattr(os, "waitid", observe)
        waiter = threading.Thread(target=wait, daemon=True)
        waiter.start()
        try:
            assert entered.wait(2)
            if stopped:
                while time.monotonic() < deadline:
                    receipt = original_waitid(
                        os.P_PID,
                        owner.process.pid,
                        os.WSTOPPED | os.WNOWAIT | os.WNOHANG,
                    )
                    if receipt is not None and receipt.si_code == os.CLD_STOPPED:
                        break
                    time.sleep(0.01)
                else:
                    pytest.fail("child never reached its stopped state")
                assert waiter.is_alive()
                assert owner.process.poll() is None
                owner.process.signal_group(signal.SIGCONT, send=os.killpg)
            assert process.terminate_owned_tree(owner, deadline=deadline)
            waiter.join(2)
            assert not waiter.is_alive()
            assert not errors
            assert results == [-signal.SIGTERM]
            assert not owner.process._blocking_observers
            assert owner.process._identity_retired
            with pytest.raises(ChildProcessError):
                original_waitid(
                    os.P_PID, owner.process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG
                )
        finally:
            with controller.defer():
                # Continue a stopped leaf even if an earlier assertion failed,
                # so its ordinary TERM cleanup can make progress.
                owner.process.signal_group(signal.SIGCONT, send=os.killpg)
                assert process.terminate_owned_tree(owner, deadline=deadline)
                waiter.join(2)
                assert not waiter.is_alive()
                assert process.close_owner(owner, deadline=deadline)


@pytest.mark.skipif(os.name != "posix", reason="POSIX interruptible blocking wait")
@pytest.mark.parametrize("number", [signal.SIGINT, signal.SIGTERM])
def test_signal_interrupt_clears_blocking_observation_before_owned_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, number: int
) -> None:
    from h2h_dev_tools._process_posix import PinnedProcess

    deadline = time.monotonic() + 10
    command = process.ProcessCommand(
        "interrupted blocking waiter",
        (sys.executable, "-I", "-S", "-c", "import time; time.sleep(3)"),
        tmp_path,
    )
    entered = threading.Event()
    sent = threading.Event()
    original_waitid = os.waitid
    with process.controlled_termination_signals() as controller:
        owner = process.start_owned_process(
            command, deadline=deadline, signal_controller=controller
        )
        assert isinstance(owner.process, PinnedProcess)

        def observe(kind: int, pid: int, options: int) -> Any:
            if pid == owner.process.pid and not options & os.WNOHANG:
                entered.set()
            return original_waitid(kind, pid, options)

        def interrupt() -> None:
            if entered.wait(2):
                time.sleep(0.02)
                sent.set()
                os.kill(os.getpid(), number)

        monkeypatch.setattr(os, "waitid", observe)
        sender = threading.Thread(target=interrupt)
        sender.start()
        try:
            expected = (
                KeyboardInterrupt
                if number == signal.SIGINT
                else process.RunnerSignalInterrupt
            )
            with pytest.raises(expected):
                owner.process.wait()
            assert sent.is_set()
            assert not owner.process._blocking_observers
            assert owner.process.poll() is None
        finally:
            sender.join(3)
            assert not sender.is_alive()
            with controller.defer():
                assert process.terminate_owned_tree(owner, deadline=deadline)
                assert process.close_owner(owner, deadline=deadline)


@pytest.mark.skipif(os.name != "posix", reason="POSIX retained waitable identity")
def test_foreign_reap_rejects_signals_and_retains_cleanup_responsibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deadline = time.monotonic() + 10
    command = process.ProcessCommand(
        "foreign reap", (sys.executable, "-I", "-S", "-c", "pass"), tmp_path
    )
    with process.controlled_termination_signals() as controller:
        owner = process.start_owned_process(
            command, deadline=deadline, signal_controller=controller
        )
        scope = owner.posix_scope
        assert scope is not None
        manually_reaped = False
        try:
            assert owner.process.wait(timeout=3) == 0
            assert os.waitpid(owner.process.pid, 0)[0] == owner.process.pid
            manually_reaped = True
            with monkeypatch.context() as boundary:

                def forbid_unowned_signal(group: int, number: int) -> None:
                    pytest.fail(f"Unproven PGID was signalled: {group}, {number}")

                boundary.setattr(os, "killpg", forbid_unowned_signal)
                with pytest.raises(process.ProcessOwnershipError) as caught:
                    process.terminate_owned_tree(owner, deadline=deadline)
                assert caught.value.cleanup_proven is False
                assert not scope.proven_empty
                assert scope.capability.directory.is_dir()
        finally:
            # This fixture deliberately violates the owner API. Its fixed leaf
            # has no descendants; only that independent fact permits rescue.
            if manually_reaped:
                assert not scope.has_pending(deadline=deadline)
                scope.proven_empty = True
                # Independent fixture-only proof: this deliberately reaped leaf
                # cannot have descendants; release its retained native handle.
                from h2h_dev_tools._process_posix import PinnedProcess

                assert isinstance(owner.process, PinnedProcess)
                owner.process._identity_retired = True
            else:
                assert process.terminate_owned_tree(owner, deadline=deadline)
            assert process.close_owner(owner, deadline=deadline)


@pytest.mark.skipif(os.name != "posix", reason="Real POSIX scope ledger locks")
@pytest.mark.parametrize(
    ("scope_phase", "bounded", "status"),
    [
        (phase, bounded, status)
        for phase in ("admission", "pending")
        for bounded in (False, True)
        for status in (0, 7)
    ]
    + [("term-grace", True, 124)],
)
def test_scope_lock_wait_preserves_original_cleanup_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope_phase: str,
    bounded: bool,
    status: int,
) -> None:
    import threading

    from h2h_dev_tools import _process_scope as scopes

    original_start = process.start_owned_process
    original_close = process.close_owner
    original_admission = scopes.PosixScope.close_admission
    original_pending = scopes.PosixScope.has_pending
    owners: list[process.OwnedProcess] = []
    scope_deadlines: list[float] = []
    close_deadlines: list[float] = []
    errors: list[BaseException] = []
    locked = threading.Event()
    release = threading.Event()
    threads: list[threading.Thread] = []
    test_deadline = time.monotonic() + 12

    def start(
        phase: process.ProcessCommand,
        *,
        deadline: float,
        signal_controller: process.SignalController | None = None,
    ) -> process.OwnedProcess:
        owner = original_start(
            phase, deadline=deadline, signal_controller=signal_controller
        )
        owners.append(owner)
        return owner

    def contend(scope: scopes.PosixScope, deadline: float) -> None:
        if not owners or scope is not owners[0].posix_scope:
            return
        scope_deadlines.append(deadline)
        if threads:
            return

        def hold() -> None:
            try:
                with scope.capability.locked(deadline=test_deadline):
                    locked.set()
                    # Exceed the 250ms exit probe or the unmodified 2s TERM
                    # grace, while remaining inside the original owner budget.
                    release.wait(timeout=2.2 if scope_phase == "term-grace" else 0.6)
            except BaseException as error:
                errors.append(error)
                locked.set()

        thread = threading.Thread(target=hold)
        threads.append(thread)
        thread.start()
        assert locked.wait(timeout=max(0, test_deadline - time.monotonic()))
        assert not errors

    def admission(scope: scopes.PosixScope, *, deadline: float) -> None:
        if scope_phase == "admission":
            contend(scope, deadline)
        original_admission(scope, deadline=deadline)

    def pending(scope: scopes.PosixScope, *, deadline: float) -> bool:
        if scope_phase != "admission":
            contend(scope, deadline)
        return original_pending(scope, deadline=deadline)

    def close(
        owner: process.OwnedProcess,
        *,
        deadline: float,
        diagnostic: Callable[[str], None] | None = None,
    ) -> bool:
        close_deadlines.append(deadline)
        return original_close(owner, deadline=deadline, diagnostic=diagnostic)

    monkeypatch.setattr(process, "start_owned_process", start)
    monkeypatch.setattr(process, "close_owner", close)
    monkeypatch.setattr(scopes.PosixScope, "close_admission", admission)
    monkeypatch.setattr(scopes.PosixScope, "has_pending", pending)
    code = (
        "import time; time.sleep(30)"
        if scope_phase == "term-grace"
        else f"raise SystemExit({status})"
    )
    command = process.ProcessCommand(
        "scope lock contention", (sys.executable, "-c", code), tmp_path
    )
    started = time.monotonic()
    hard_deadline = started + (5 if scope_phase == "term-grace" else 8)
    with process.controlled_termination_signals() as controller:
        try:
            assert (
                process.run_command(
                    command,
                    hard_deadline=hard_deadline if bounded else None,
                    termination_reserve_seconds=4 if scope_phase == "term-grace" else 2,
                    signal_controller=controller,
                )
                == status
            )
            assert 0.5 <= time.monotonic() - started < 8
            assert len(owners) == len(close_deadlines) == 1
            assert scope_deadlines and set(scope_deadlines) == {close_deadlines[0]}
            if bounded:
                assert close_deadlines == [hard_deadline]
            else:
                assert started + 2 <= close_deadlines[0] <= test_deadline
            owner = owners[0]
            assert owner.process.poll() is not None
            with pytest.raises(ProcessLookupError):
                os.killpg(owner.process.pid, 0)
            assert owner.posix_scope is not None
            assert not owner.posix_scope.capability.directory.exists()
        finally:
            with controller.defer():
                release.set()
                for thread in threads:
                    thread.join(timeout=max(0, test_deadline - time.monotonic()))
                    assert not thread.is_alive()
                # Failure-only rescue follows the before-rescue assertions and
                # retains the original test deadline, including on old code.
                for owner in owners:
                    scope = owner.posix_scope
                    if scope is not None and scope.capability.directory.exists():
                        assert process.terminate_owned_tree(
                            owner, deadline=test_deadline
                        )
                        assert original_close(owner, deadline=test_deadline)
                assert not errors


def _source_bootstrap(package: Path) -> str:
    return (
        "import runpy\nfrom pathlib import Path\n"
        f"package = Path({str(package)!r})\n"
        "runpy.run_path(str(package / 'source_loader.py'))['load_package'](\n"
        "    package, 'h2h_dev_tools')\n"
    )


@contextmanager
def _diagnostic_launcher(
    command: process.ProcessCommand, *, deadline: float
) -> Iterator[process.OwnedProcess]:
    with process.controlled_termination_signals() as controller:
        owner: process.OwnedProcess | None = None
        try:
            with controller.defer():
                owner = process.start_owned_process(
                    command, deadline=deadline, signal_controller=controller
                )
            yield owner
        finally:
            # A failed oracle may need rescue; a successful body must already
            # have proved the complete tree empty without sending any signal.
            failure = sys.exception()
            with controller.defer():
                if owner is not None:
                    if failure is not None:
                        assert process.terminate_owned_tree(owner, deadline=deadline)
                    assert process.close_owner(owner, deadline=deadline)
                    scope = owner.posix_scope
                    if scope is not None:
                        assert not scope.capability.directory.exists()
                        if scope.parent is not None:
                            with scope.parent.locked(deadline=deadline):
                                assert not (
                                    scope.parent.directory
                                    / "pending"
                                    / scope.capability.token
                                ).exists()


def _assert_diagnostic_cleanup(owner: process.OwnedProcess, *, deadline: float) -> None:
    if os.name == "posix":
        scope = owner.posix_scope
        assert scope is not None
        assert not scope.has_pending(deadline=deadline), (
            "Diagnostic nested cleanup obligations remain"
        )
    # This observation retires only the pinned zombie, never a surviving writer.
    # The independent OS absence assertion follows retirement, before any rescue.
    assert process.wait_for_owned_tree_exit(
        owner, deadline=deadline, poll_deadline=time.monotonic()
    ), "Diagnostic launcher/writer group survived"
    if os.name == "posix":
        with pytest.raises(ProcessLookupError):
            os.killpg(owner.process.pid, 0)


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
    # Retain the original 5s leader wait and 5s failure-only rescue envelope.
    deadline = time.monotonic() + 10
    try:
        command = process.ProcessCommand(
            "full diagnostic pipe",
            (sys.executable, "-I", "-B", "-c", script),
            tmp_path,
            stdout=write_descriptor if standard_output else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL if standard_output else write_descriptor,
        )
        with _diagnostic_launcher(command, deadline=deadline) as owner:
            assert owner.process.wait(timeout=5) == 7
            assert marker.read_text() == "True"
            assert os.get_blocking(write_descriptor)
            _assert_diagnostic_cleanup(owner, deadline=deadline)
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
    # Preserve the original ready/leader/rescue stages, each bounded by 5s.
    # The inner command still owns its original 3s timeout and 1s reserve.
    deadline = time.monotonic() + 15
    try:
        command = process.ProcessCommand(
            "diagnostic cleanup under backpressure",
            (sys.executable, "-I", "-B", "-c", script),
            tmp_path,
            stdout=write_descriptor if standard_output else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL if standard_output else write_descriptor,
        )
        with _diagnostic_launcher(command, deadline=deadline) as owner:
            ready_deadline = time.monotonic() + 5
            while not ready.exists():
                assert owner.process.poll() is None
                assert time.monotonic() < ready_deadline, (
                    "The child did not fill its pipe"
                )
                time.sleep(0.01)
            group = int(ready.read_text())
            if trigger == "cancel":
                owner.process.send_signal(signal.SIGTERM)
            expected = 143 if trigger == "cancel" else process.TIMEOUT_EXIT_CODE
            assert owner.process.wait(timeout=5) == expected
            assert completed.read_text() == str(expected)
            with pytest.raises(ProcessLookupError):
                os.killpg(group, 0)
            assert os.get_blocking(write_descriptor)
            _assert_diagnostic_cleanup(owner, deadline=deadline)
    finally:
        os.close(read_descriptor)
        os.close(write_descriptor)


@pytest.mark.skipif(os.name != "posix", reason="Real process group and scope ledger")
@pytest.mark.parametrize("remaining", ["writer", "obligation"])
def test_diagnostic_cleanup_oracle_rejects_remaining_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remaining: str
) -> None:
    from h2h_dev_tools import _process_scope as scopes

    ready = tmp_path / "writer-ready"
    completed = tmp_path / "launcher-completed"
    writer = (
        "import os\nfrom pathlib import Path\n"
        "os.set_blocking(1, False)\n"
        "try:\n"
        "    while True: os.write(1, b'x' * 8192)\n"
        "except BlockingIOError:\n    pass\n"
        "finally:\n    os.set_blocking(1, True)\n"
        f"Path({str(ready)!r}).write_text(str(os.getpid()))\n"
        "os.write(1, b'x' * 8192)\n"
    )
    script = (
        "import subprocess, sys, time\nfrom pathlib import Path\n"
        f"subprocess.Popen((sys.executable, '-I', '-B', '-c', {writer!r}))\n"
        "deadline = time.monotonic() + 5\n"
        f"while not Path({str(ready)!r}).exists():\n"
        "    assert time.monotonic() < deadline\n"
        "    time.sleep(.01)\n"
        f"Path({str(completed)!r}).write_text('0')\n"
        if remaining == "writer"
        else "pass"
    )
    read_descriptor, write_descriptor = os.pipe()
    deadline = time.monotonic() + 10
    owner: process.OwnedProcess | None = None
    try:
        command = process.ProcessCommand(
            "diagnostic oracle negative control",
            (sys.executable, "-I", "-B", "-c", script),
            tmp_path,
            stdout=write_descriptor,
            stderr=subprocess.DEVNULL,
        )
        message = (
            "Diagnostic launcher/writer group survived"
            if remaining == "writer"
            else "Diagnostic nested cleanup obligations remain"
        )
        # Only this exact oracle rejection is expected. Cleanup in the context's
        # finally cannot create a passing observation or satisfy this match.
        with pytest.raises(AssertionError, match=message):
            with _diagnostic_launcher(command, deadline=deadline) as owner:
                scope = owner.posix_scope
                assert scope is not None
                assert owner.process.wait(timeout=5) == 0
                if remaining == "writer":
                    # The old leader/status/marker oracle would pass here, even
                    # though the actual same-group writer cannot finish its write.
                    assert completed.read_text() == "0"
                    writer_pid = int(ready.read_text())
                    assert writer_pid != owner.process.pid
                    assert os.getpgid(writer_pid) == owner.process.pid
                    os.kill(writer_pid, 0)
                    _assert_diagnostic_cleanup(owner, deadline=deadline)
                else:
                    nested: scopes.PosixScope | None = None
                    with process.controlled_termination_signals() as controller:
                        try:
                            with (
                                controller.defer(),
                                monkeypatch.context() as environment,
                            ):
                                environment.setenv(
                                    scopes.ENVIRONMENT_KEY, scope.capability.encode()
                                )
                                nested = scopes.PosixScope.create(deadline=deadline)
                            _assert_diagnostic_cleanup(owner, deadline=deadline)
                        finally:
                            with controller.defer():
                                if nested is not None:
                                    # This fixture registered a real obligation but
                                    # never admitted a process. Discharge it only
                                    # after the oracle has had to reject it.
                                    nested.proven_empty = True
                                    nested.close(deadline=deadline)
        assert owner is not None and owner.process.poll() is not None
        with pytest.raises(ProcessLookupError):
            os.killpg(owner.process.pid, 0)
        assert owner.posix_scope is not None
        assert not owner.posix_scope.capability.directory.exists()
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

    def wait_then_contend(
        owner: process.OwnedProcess,
        *,
        deadline: float,
        poll_deadline: float | None = None,
    ) -> bool:
        owners.append(owner)
        assert original_wait(owner, deadline=deadline, poll_deadline=poll_deadline)
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
            assert "Cannot close an unproven group identity" in messages.pop()
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
            assert all(number != 0 for number, _ in observations)
            with pytest.raises(ProcessLookupError):
                real_killpg(owner.process.pid, 0)
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


@pytest.mark.skipif(
    sys.platform != "linux", reason="Linux retained pidfd group identity"
)
def test_pidfd_cleanup_survives_leader_reap_fork_transfer_and_nondumpable_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from h2h_dev_tools._process_posix import PinnedProcess

    ready = tmp_path / "descendant-ready"
    grandchild = (
        "import ctypes,os,signal,sys,time\nfrom pathlib import Path\n"
        "assert ctypes.CDLL(None).prctl(4,0,0,0,0)==0\n"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        "Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )
    child = (
        "import subprocess,sys\n"
        f"subprocess.Popen([sys.executable,'-I','-S','-c',{grandchild!r},sys.argv[1]])\n"
    )
    leader = (
        "import subprocess,sys\n"
        f"subprocess.Popen([sys.executable,'-I','-S','-c',{child!r},sys.argv[1]])\n"
    )
    deadline = time.monotonic() + 10
    with process.controlled_termination_signals() as controller:
        with controller.defer():
            owner = process.start_owned_process(
                process.ProcessCommand(
                    "fork transfer",
                    (sys.executable, "-I", "-S", "-c", leader, str(ready)),
                    tmp_path,
                ),
                deadline=deadline,
                signal_controller=controller,
            )
        try:
            assert owner.process.wait(timeout=3) == 0
            while not ready.exists():
                assert time.monotonic() < deadline - 4
                time.sleep(0.01)
            assert isinstance(owner.process, PinnedProcess)
            assert not process.wait_for_owned_tree_exit(
                owner, deadline=deadline, poll_deadline=time.monotonic() + 0.05
            )
            assert owner.process._leader_reaped
            with pytest.raises(ChildProcessError):
                os.waitpid(owner.process.pid, os.WNOHANG)
            with monkeypatch.context() as boundary:

                def forbid_numeric_group(group: int, number: int) -> None:
                    pytest.fail("Linux cleanup used a reusable numeric PGID")

                boundary.setattr(os, "killpg", forbid_numeric_group)
                assert process.terminate_owned_tree(owner, deadline=deadline)
            assert process.close_owner(owner, deadline=deadline)
            assert (
                not owner.posix_scope
                or not owner.posix_scope.capability.directory.exists()
            )
        finally:
            with controller.defer():
                if (
                    owner.posix_scope
                    and owner.posix_scope.capability.directory.exists()
                ):
                    assert process.terminate_owned_tree(owner, deadline=deadline)
                    assert process.close_owner(owner, deadline=deadline)


@pytest.mark.skipif(
    sys.platform != "linux", reason="Linux post-spawn pidfd acquisition failure"
)
@pytest.mark.parametrize("nested_completes", [False, True])
def test_pidfd_acquisition_failure_preserves_live_nested_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nested_completes: bool
) -> None:
    import json

    from h2h_dev_tools import _process_posix as native
    from h2h_dev_tools import _process_scope as scopes

    ready = tmp_path / "nested-ready.json"
    release = tmp_path / "fixture-release"
    completed = tmp_path / "nested-clean"
    # The fixed nested leaf also accepts the fixture's release marker, so even
    # a regression that kills its owner cannot leave it running indefinitely.
    leaf = (
        "import sys,time\nfrom pathlib import Path\n"
        "while not Path(sys.argv[1]).exists(): time.sleep(.01)\n"
    )
    target = """import json,os,runpy,signal,sys,time
from pathlib import Path
package=Path(sys.argv[1])
runpy.run_path(str(package/'source_loader.py'))['load_package'](package,'h2h_dev_tools')
from h2h_dev_tools import process as p
from h2h_dev_tools import _process_scope as scopes
ready,release,completed=map(Path,sys.argv[2:5])
cooperative=sys.argv[5]=='True'
deadline=float(sys.argv[6])
leaf=sys.argv[7]
with p.controlled_termination_signals() as controller:
    with controller.defer():
        inner=p.start_owned_process(
            p.ProcessCommand('nested leaf',(sys.executable,'-I','-S','-c',leaf,str(release)),ready.parent),
            deadline=deadline,signal_controller=controller)
        scope=inner.posix_scope
        assert scope is not None and scope.parent is not None
        if not cooperative:
            signal.signal(signal.SIGTERM,signal.SIG_IGN)
        data={'pid':inner.process.pid,'scope':scope.capability.encode(),
              'parent':scope.parent.encode(),'outer':os.environ[scopes.ENVIRONMENT_KEY]}
        staging=ready.with_suffix('.pending')
        staging.write_text(json.dumps(data))
        staging.replace(ready)
    try:
        while not release.exists():
            time.sleep(.01)
    except (KeyboardInterrupt,p.RunnerSignalInterrupt):
        pass
    finally:
        with controller.defer():
            # Only the explicit fixture marker admits an independent rescue
            # budget; production TERM cleanup retains the original deadline.
            cleanup_deadline=time.monotonic()+5 if release.exists() else deadline
            assert p.terminate_owned_tree(inner,deadline=cleanup_deadline)
            assert p.close_owner(inner,deadline=cleanup_deadline)
            completed.touch()
"""
    deadline = time.monotonic() + 5
    original_open = native._pidfd_open
    original_signal = os.killpg
    nested_handle: int | None = None
    nested_identity: dict[str, Any] | None = None
    signals: list[int] = []
    owner: process.OwnedProcess | None = None
    acquisition_error = OSError(errno.EMFILE, "injected post-spawn fd exhaustion")

    def fail_target_acquisition(pid: int) -> int:
        nonlocal nested_handle, nested_identity
        if pid == os.getpid():
            return original_open(pid)  # Keep the actual Linux capability probe.
        while not ready.exists() and time.monotonic() < deadline - 2:
            time.sleep(0.01)
        if ready.exists():
            nested_identity = json.loads(ready.read_text())
            assert nested_identity is not None
            # Independent fixture authority is acquired before the failure.
            # This handle is used only for rescue if a regression kills the owner.
            nested_handle = original_open(nested_identity["pid"])
        raise acquisition_error

    def observe_signal(group: int, number: int) -> None:
        assert nested_identity is not None, "Nested target did not reach readiness"
        outer = scopes._Capability.decode(nested_identity["outer"])
        assert (outer.directory / "closed").exists()
        if number == signal.SIGKILL:
            assert not tuple((outer.directory / "pending").iterdir()), (
                "Acquisition failure killed an owner with nested obligations"
            )
        signals.append(number)
        original_signal(group, number)

    with process.controlled_termination_signals() as controller:
        try:
            with monkeypatch.context() as boundary:
                boundary.setattr(native, "_pidfd_open", fail_target_acquisition)
                boundary.setattr(os, "killpg", observe_signal)
                with (
                    controller.defer(),
                    pytest.raises(process.ProcessOwnershipError) as caught,
                ):
                    process.start_owned_process(
                        process.ProcessCommand(
                            "post-spawn identity failure",
                            (
                                sys.executable,
                                "-I",
                                "-S",
                                "-B",
                                "-c",
                                target,
                                str(Path(process.__file__).parent),
                                str(ready),
                                str(release),
                                str(completed),
                                str(nested_completes),
                                str(deadline),
                                leaf,
                            ),
                            tmp_path,
                        ),
                        deadline=deadline,
                        signal_controller=controller,
                    )
            failure = caught.value
            owner = failure.owner
            assert owner is not None
            assert isinstance(failure.__cause__, native.AcquisitionError)
            assert failure.__cause__.__cause__ is acquisition_error
            assert not failure.cleanup_proven
            assert isinstance(owner.process, native.PinnedProcess)
            assert owner.process._pidfd is None
            assert owner.process._pidfd_acquisition_failed
            assert not owner.process._leader_reaped
            assert not owner.process._identity_retired
            scope = owner.posix_scope
            assert scope is not None and scope.capability.directory.is_dir()
            assert not scope.proven_empty
            # A reaped leader would raise ECHILD. This receipt never reaps it.
            receipt = os.waitid(
                os.P_PID, owner.process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT
            )
            if nested_completes:
                assert completed.exists()
                assert receipt is not None
                assert signals == [signal.SIGTERM, signal.SIGKILL]
                assert not tuple((scope.capability.directory / "pending").iterdir())
            else:
                assert not completed.exists()
                assert receipt is None
                assert signals == [signal.SIGTERM]
                assert tuple((scope.capability.directory / "pending").iterdir())
        finally:
            with controller.defer():
                release.touch()
                rescue_deadline = time.monotonic() + 8
                try:
                    if owner is not None:
                        child = owner.process
                        assert isinstance(child, native.PinnedProcess)
                        # The failure retained this waitable leader, so opening
                        # its original pidfd after lifting fault injection is safe.
                        child._pidfd = original_open(child.pid)
                        child._pidfd_acquisition_failed = False
                        scope = owner.posix_scope
                        assert scope is not None
                        if child.poll() is not None and scope.has_pending(
                            deadline=rescue_deadline
                        ):
                            # A failing regression may have killed the nested
                            # owner. Independently prove its fixed leaf group
                            # empty before releasing only that exact obligation.
                            assert (
                                nested_handle is not None
                                and nested_identity is not None
                            )
                            while True:
                                try:
                                    native._pidfd_signal(nested_handle, 0)
                                except ProcessLookupError:
                                    break
                                assert time.monotonic() < rescue_deadline
                                time.sleep(0.01)
                            nested_scope = scopes.PosixScope(
                                scopes._Capability.decode(nested_identity["scope"]),
                                scopes._Capability.decode(nested_identity["parent"]),
                                proven_empty=True,
                            )
                            nested_scope.close(deadline=rescue_deadline)
                        assert process.terminate_owned_tree(
                            owner, deadline=rescue_deadline
                        )
                        assert process.close_owner(owner, deadline=rescue_deadline)
                finally:
                    if nested_handle is not None:
                        os.close(nested_handle)
