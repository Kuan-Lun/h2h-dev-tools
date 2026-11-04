"""Native shared-descriptor and leaf-writer ownership regressions."""

from __future__ import annotations

import errno
import json
import os
import select
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from h2h_dev_tools import process

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="Native POSIX shared open-file-description contract"
)

_ACTOR = r"""
import json, os, runpy, signal, subprocess, sys, time
from pathlib import Path

package, directory = map(Path, sys.argv[1:3])
number, gate, standard_output = map(int, sys.argv[3:6])
mode = sys.argv[6]
runpy.run_path(str(package / 'source_loader.py'))['load_package'](
    package, 'h2h_dev_tools')
from h2h_dev_tools import process

helpers = []
cancel_observation = {}
real_popen = subprocess.Popen

def observed_popen(*args, **kwargs):
    child = real_popen(*args, **kwargs)
    helpers.append((child, os.getpgid(child.pid)))
    if mode.startswith('admission-'):
        cancel_observation.update(live=True, deferred=controller.defer_depth)
        os.kill(os.getpid(), signal.SIGTERM if mode.endswith('term') else signal.SIGINT)
    elif mode.startswith('cancel-'):
        real_wait = child.wait
        first_wait = True
        def interrupted_wait(*args, **kwargs):
            nonlocal first_wait
            if first_wait:
                first_wait = False
                # The first wait starts only after admission's defer ends. The
                # full pipe stays undrained; observe the real child before the
                # real signal. Cleanup waits still call the actual OS wait.
                os.kill(child.pid, 0)
                cancel_observation.update(live=True, deferred=controller.defer_depth)
                os.kill(os.getpid(), signal.SIGTERM if mode == 'cancel-term' else signal.SIGINT)
            return real_wait(*args, **kwargs)
        child.wait = interrupted_wait
    return child

subprocess.Popen = observed_popen
real_set_blocking = os.set_blocking

def observed_set_blocking(fd, blocking):
    real_set_blocking(fd, blocking)
    if fd in (1, 2):
        # Hold a real changed OFD flag long enough for the other process to see
        # it. This schedules the old bug; it does not fabricate an OS result.
        time.sleep(0.05)

os.set_blocking = observed_set_blocking
(directory / f'{number}.ready').touch()
if os.read(gate, 1) != b'G':
    raise RuntimeError('missing test admission')
os.close(gate)
started = time.monotonic()
kwargs = {'standard_output': bool(standard_output)}
if mode == 'expired':
    kwargs['deadline'] = started - 1
if mode == 'closed':
    os.close(1 if standard_output else 2)
result = {}
try:
    if mode.startswith(('cancel-', 'admission-')):
        with process.controlled_termination_signals() as controller:
            process.report_diagnostic(
                f'actor-{number}: 可見的 diagnostic', deadline=started + 1,
                signal_controller=controller, **kwargs)
    else:
        process.report_diagnostic(f'actor-{number}: 可見的 diagnostic', **kwargs)
except process.RunnerSignalInterrupt as error:
    result['signal'] = error.signal_number
except KeyboardInterrupt:
    result['signal'] = signal.SIGINT
except BaseException as error:
    result['error'] = repr(error)
result.update(elapsed=time.monotonic() - started, group=os.getpgrp(),
              cancel_observation=cancel_observation)
records = []
for child, group in helpers:
    # A wait here must not be the operation that makes a missing runtime reap
    # look successful. Inspect returncode first, then independently ask the OS.
    runtime_returncode = child.returncode
    try:
        os.waitpid(child.pid, os.WNOHANG)
        already_reaped = False
    except ChildProcessError:
        already_reaped = True
    records.append(dict(pid=child.pid, group=group,
                        returncode=runtime_returncode, reaped=already_reaped))
result['helpers'] = records
(directory / f'{number}.json').write_text(json.dumps(result))
"""


def _fill_pipe(descriptor: int) -> None:
    os.set_blocking(descriptor, False)
    try:
        while os.write(descriptor, b"x" * 4096):
            pass
    except BlockingIOError:
        pass


def _pipe_actors(
    directory: Path,
    package: Path,
    *,
    full: bool,
    blocking: bool,
    standard_output: bool,
    mode: str = "normal",
    count: int = 2,
) -> tuple[list[dict[str, Any]], bytes, set[bool]]:
    read_fd, write_fd = os.pipe()
    gate_read, gate_write = os.pipe()
    children: list[subprocess.Popen[bytes]] = []
    output = bytearray()
    observed_flags: set[bool] = set()
    deadline = time.monotonic() + 10
    try:
        if full:
            _fill_pipe(write_fd)
        os.set_blocking(write_fd, blocking)
        for number in range(count):
            children.append(
                subprocess.Popen(
                    (
                        sys.executable,
                        "-I",
                        "-B",
                        "-c",
                        _ACTOR,
                        str(package),
                        str(directory),
                        str(number),
                        str(gate_read),
                        str(int(standard_output)),
                        mode,
                    ),
                    stdin=subprocess.DEVNULL,
                    stdout=write_fd,
                    stderr=write_fd,
                    pass_fds=(gate_read,),
                    # Ordinary nondetached children remain in the pytest owner.
                )
            )
        while not all((directory / f"{n}.ready").exists() for n in range(count)):
            assert all(child.poll() is None for child in children)
            assert time.monotonic() < deadline - 2, "actor bootstrap stalled"
            time.sleep(0.005)
        os.write(gate_write, b"G" * count)
        while True:
            observed_flags.add(os.get_blocking(write_fd))
            if not full and select.select([read_fd], [], [], 0)[0]:
                output.extend(os.read(read_fd, 65536))
            if all(child.poll() is not None for child in children):
                break
            assert time.monotonic() < deadline - 2, "diagnostic blocked its caller"
            time.sleep(0.001)
        if not full:
            while select.select([read_fd], [], [], 0)[0]:
                output.extend(os.read(read_fd, 65536))
        assert all(child.returncode == 0 for child in children), bytes(output)
        records = [
            json.loads((directory / f"{n}.json").read_text()) for n in range(count)
        ]
        return records, bytes(output), observed_flags
    finally:
        # Fixture rescue cannot turn a failed assertion into cleanup evidence.
        # No process is detached from the enclosing canonical pytest owner.
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=max(0.01, deadline - time.monotonic()))
        for descriptor in (gate_read, gate_write, read_fd, write_fd):
            os.close(descriptor)


def _assert_reaped_same_group(records: list[dict[str, Any]]) -> None:
    identifiers: set[int] = set()
    for record in records:
        assert "error" not in record, record
        assert record["group"] == os.getpgrp()
        assert len(record["helpers"]) == 1, record
        for helper in record["helpers"]:
            assert helper["group"] == record["group"]
            assert helper["returncode"] is not None
            assert helper["reaped"] is True
            assert helper["pid"] not in identifiers
            identifiers.add(helper["pid"])
            with pytest.raises(ProcessLookupError):
                os.kill(helper["pid"], 0)


@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize("blocking", [False, True])
@pytest.mark.parametrize("standard_output", [False, True])
@pytest.mark.parametrize("poisoned_source_package", ["process.py"], indirect=True)
def test_parallel_diagnostics_preserve_shared_pipe_flags_and_reap_writers(
    tmp_path: Path,
    poisoned_source_package: Path,
    full: bool,
    blocking: bool,
    standard_output: bool,
) -> None:
    records, output, flags = _pipe_actors(
        tmp_path,
        poisoned_source_package,
        full=full,
        blocking=blocking,
        standard_output=standard_output,
    )
    # This is an OS observation across independently executing processes, not
    # an assertion that a mocked flags function was or was not called.
    assert flags == {blocking}
    _assert_reaped_same_group(records)
    # One second permits scheduler noise around the 250 ms writer budget; the
    # parent never drains a full pipe before both callers have returned.
    assert all(record["elapsed"] < 1 for record in records), records
    if not full:
        assert sorted(output.decode().splitlines()) == [
            "actor-0: 可見的 diagnostic",
            "actor-1: 可見的 diagnostic",
        ]
    elif blocking:
        assert all(record["helpers"][0]["returncode"] < 0 for record in records)


@pytest.mark.parametrize("standard_output", [False, True])
@pytest.mark.parametrize("mode", ["expired", "closed"])
def test_unavailable_diagnostic_output_preserves_caller(
    tmp_path: Path, standard_output: bool, mode: str
) -> None:
    records, output, flags = _pipe_actors(
        tmp_path,
        Path(process.__file__).resolve().parent,
        full=False,
        blocking=True,
        standard_output=standard_output,
        mode=mode,
        count=1,
    )
    assert output == b""
    assert flags == {True}
    assert "error" not in records[0], records
    if mode == "expired":
        assert records[0]["helpers"] == []
    elif records[0]["helpers"]:
        _assert_reaped_same_group(records)
    assert records[0]["elapsed"] < 1


@pytest.mark.parametrize(
    "mode, signal_number",
    [
        ("cancel-term", signal.SIGTERM),
        ("cancel-int", signal.SIGINT),
        ("admission-term", signal.SIGTERM),
        ("admission-int", signal.SIGINT),
    ],
)
def test_cancelled_full_pipe_writer_is_reaped_before_signal_returns(
    tmp_path: Path, mode: str, signal_number: int
) -> None:
    records, output, flags = _pipe_actors(
        tmp_path,
        Path(process.__file__).resolve().parent,
        full=True,
        blocking=True,
        standard_output=False,
        mode=mode,
        count=1,
    )
    assert output == b""
    assert flags == {True}
    _assert_reaped_same_group(records)
    assert records[0]["signal"] == signal_number
    assert records[0]["cancel_observation"]["live"] is True
    assert bool(records[0]["cancel_observation"]["deferred"]) == mode.startswith(
        "admission-"
    )
    assert records[0]["helpers"][0]["returncode"] < 0
    assert records[0]["elapsed"] < 1


_SCOPED_ACTOR = r"""
import json, os, runpy, signal, subprocess, sys, time
from pathlib import Path
package, state = map(Path, sys.argv[1:3])
runpy.run_path(str(package / 'source_loader.py'))['load_package'](package, 'h2h_dev_tools')
from h2h_dev_tools import process
from h2h_dev_tools._process_scope import ENVIRONMENT_KEY, PosixScope
parent = Path(json.loads(os.environ[ENVIRONMENT_KEY])['directory'])
# The prior main command's scope has already closed. The terminal writer must
# register its own obligation rather than borrowing this completed scope.
completed = PosixScope.create(deadline=time.monotonic() + 2)
completed.proven_empty = True
completed.close(deadline=time.monotonic() + 2)
real_popen = subprocess.Popen

def observe_popen(*args, **kwargs):
    child = real_popen(*args, **kwargs)
    real_wait = child.wait
    def observe_wait(*args, **kwargs):
        result = real_wait(*args, **kwargs)
        try:
            os.waitpid(child.pid, os.WNOHANG)
            reaped = False
        except ChildProcessError:
            reaped = True
        (state / 'reaped.json').write_text(json.dumps(dict(pid=child.pid, reaped=reaped)))
        return result
    child.wait = observe_wait
    (state / 'admitted.json').write_text(json.dumps(dict(
        pid=child.pid, group=os.getpgid(child.pid), caller_group=os.getpgrp(),
        pending=bool(tuple((parent / 'pending').iterdir())))))
    limit = time.monotonic() + 1
    while not (parent / 'closed').exists():
        if time.monotonic() >= limit:
            raise RuntimeError('parent failed to start termination')
        time.sleep(0.001)
    # Keep the true leaf's wait behind the parent's closed-admission boundary.
    # Old code can now attempt group KILL before any child wait; the parent's
    # observation hook rescues that attempt only after recording the violation.
    time.sleep(0.03)
    return child

subprocess.Popen = observe_popen
try:
    with process.controlled_termination_signals() as controller:
        process.report_diagnostic('terminal output', deadline=time.monotonic() + 2,
                                  signal_controller=controller)
except (process.RunnerSignalInterrupt, KeyboardInterrupt):
    pass
signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    time.sleep(0.01)
"""


def test_terminal_writer_obligation_prevents_force_kill_before_real_reap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The actual parent's force-kill boundary observes the leaf's OS reap."""
    from h2h_dev_tools._process_posix import PinnedProcess

    read_fd, write_fd = os.pipe()
    _fill_pipe(write_fd)
    os.set_blocking(write_fd, True)
    deadline = time.monotonic() + 8
    owner = None
    kill_observations: list[bool] = []
    real_signal_group = PinnedProcess.signal_group

    def observe_signal_group(
        child: PinnedProcess, number: int, *, send: Callable[[int, int], None]
    ) -> None:
        if number == signal.SIGKILL:
            marker = tmp_path / "reaped.json"
            kill_observations.append(marker.exists())
            # Rescue only the old incorrect ordering, after recording it. This
            # prevents deliberately orphaning a real leaf in the negative run.
            while not marker.exists() and time.monotonic() < deadline - 1:
                time.sleep(0.001)
        real_signal_group(child, number, send=send)

    try:
        with process.controlled_termination_signals() as controller:
            try:
                with controller.defer():
                    try:
                        owner = process.start_owned_process(
                            process.ProcessCommand(
                                "terminal diagnostic scope probe",
                                (
                                    sys.executable,
                                    "-I",
                                    "-B",
                                    "-c",
                                    _SCOPED_ACTOR,
                                    str(Path(process.__file__).resolve().parent),
                                    str(tmp_path),
                                ),
                                tmp_path,
                                stdout=write_fd,
                                stderr=write_fd,
                            ),
                            deadline=deadline,
                            signal_controller=controller,
                        )
                    except process.ProcessOwnershipError as error:
                        owner = error.owner
                        raise
                while not (tmp_path / "admitted.json").exists():
                    assert owner.process.poll() is None
                    assert time.monotonic() < deadline - 3
                    time.sleep(0.001)
                with monkeypatch.context() as boundary:
                    # Exercise the post-grace branch directly; no production grace
                    # or total deadline is increased to make the test pass.
                    boundary.setattr(process, "TERMINATION_GRACE_SECONDS", 0)
                    # Observe the shared boundary without replacing either
                    # platform's native sender (Darwin killpg or Linux pidfd).
                    boundary.setattr(
                        PinnedProcess, "signal_group", observe_signal_group
                    )
                    assert process.terminate_owned_tree(owner, deadline=deadline)
                assert process.close_owner(owner, deadline=deadline)
                owner = None
                admitted = json.loads((tmp_path / "admitted.json").read_text())
                reaped = json.loads((tmp_path / "reaped.json").read_text())
                (tmp_path / "force-kill-observations.json").write_text(
                    json.dumps(kill_observations)
                )
                assert admitted["pending"] is True, admitted
                assert admitted["group"] == admitted["caller_group"]
                assert reaped == {"pid": admitted["pid"], "reaped": True}
                assert kill_observations == [True]
            finally:
                with controller.defer():
                    if owner is not None:
                        assert process.terminate_owned_tree(owner, deadline=deadline)
                        assert process.close_owner(owner, deadline=deadline)
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_closed_parent_admits_no_terminal_diagnostic_leaf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from h2h_dev_tools import _process_diagnostic as diagnostic
    from h2h_dev_tools._process_scope import ENVIRONMENT_KEY, PosixScope

    deadline = time.monotonic() + 3
    parent = PosixScope.create(deadline=deadline)
    children: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def observed_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        child: subprocess.Popen[bytes] = real_popen(*args, **kwargs)
        children.append(child)
        return child

    try:
        parent.close_admission(deadline=deadline)
        with monkeypatch.context() as boundary:
            boundary.setenv(ENVIRONMENT_KEY, parent.capability.encode())
            boundary.setattr(subprocess, "Popen", observed_popen)
            with (tmp_path / "output").open("wb") as output:
                with process.controlled_termination_signals() as controller:
                    diagnostic.write_diagnostic(
                        output.fileno(),
                        b"late output",
                        deadline=deadline,
                        signal_controller=controller,
                    )
        assert not parent.has_pending(deadline=deadline)
        assert children == []
        assert (tmp_path / "output").read_bytes() == b""
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=max(0.01, deadline - time.monotonic()))
        parent.proven_empty = True
        parent.close(deadline=deadline)


@pytest.mark.parametrize("stage", ["create", "registered", "close", "reap"])
@pytest.mark.parametrize("error_type", [OSError, RuntimeError])
def test_diagnostic_scope_failures_remain_typed_and_retain_uncertain_obligations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    error_type: type[Exception],
) -> None:
    from h2h_dev_tools import _process_diagnostic as diagnostic
    from h2h_dev_tools._process_scope import ENVIRONMENT_KEY, PosixScope

    deadline = time.monotonic() + 3
    parent = PosixScope.create(deadline=deadline)
    scopes: list[PosixScope] = []
    children: list[subprocess.Popen[bytes]] = []
    original_error = error_type(f"injected {stage} filesystem failure")
    real_create = PosixScope.create
    real_close = PosixScope.close
    real_popen = subprocess.Popen

    def failing_create(*, deadline: float) -> PosixScope:
        if stage == "create":
            raise original_error
        scope = real_create(deadline=deadline)
        scopes.append(scope)
        if stage == "registered":
            raise original_error
        return scope

    def failing_close(scope: PosixScope, *, deadline: float) -> None:
        if stage == "close" and scope in scopes:
            raise original_error
        real_close(scope, deadline=deadline)

    def observed_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        child: subprocess.Popen[bytes] = real_popen(*args, **kwargs)
        children.append(child)
        if stage == "reap":
            actual_wait = child.wait

            def uncertain_wait(*args: Any, **kwargs: Any) -> int:
                actual_wait(*args, **kwargs)
                raise original_error

            monkeypatch.setattr(child, "wait", uncertain_wait)
        return child

    try:
        with monkeypatch.context() as boundary:
            boundary.setenv(ENVIRONMENT_KEY, parent.capability.encode())
            boundary.setattr(PosixScope, "create", failing_create)
            boundary.setattr(PosixScope, "close", failing_close)
            boundary.setattr(subprocess, "Popen", observed_popen)
            with (tmp_path / "output").open("wb") as output:
                with process.controlled_termination_signals() as controller:
                    with pytest.raises(process.ProcessOwnershipError) as raised:
                        diagnostic.write_diagnostic(
                            output.fileno(),
                            b"output",
                            deadline=deadline,
                            signal_controller=controller,
                        )
        assert raised.value.__cause__ is original_error
        assert raised.value.cleanup_proven is False
        assert parent.has_pending(deadline=deadline) is (stage != "create")
        assert len(children) == (1 if stage in {"close", "reap"} else 0)
        for child in children:
            assert child.returncode is not None
            with pytest.raises(ChildProcessError):
                os.waitpid(child.pid, os.WNOHANG)
    finally:
        # Fixture recovery is justified by actual waits or the recorded absence
        # of Popen, not by replacing the failing runtime's ownership evidence.
        for child in children:
            if child.poll() is None:
                child.kill()
            subprocess.Popen.wait(child, timeout=max(0.01, deadline - time.monotonic()))
        for scope in scopes:
            scope.proven_empty = True
            real_close(scope, deadline=deadline)
        parent.proven_empty = True
        real_close(parent, deadline=deadline)


@pytest.mark.parametrize("stage", ["spawn", "expired", "signal"])
def test_registered_unlaunched_diagnostic_closes_with_original_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    from h2h_dev_tools import _process_diagnostic as diagnostic
    from h2h_dev_tools._process_scope import ENVIRONMENT_KEY, PosixScope

    deadline = time.monotonic() + 3
    parent = PosixScope.create(deadline=deadline)
    scopes: list[PosixScope] = []
    close_deadlines: list[float] = []
    spawn_attempts: list[bool] = []
    real_create = PosixScope.create
    real_close = PosixScope.close
    real_remaining = process.seconds_remaining

    def observed_create(*, deadline: float) -> PosixScope:
        scope = real_create(deadline=deadline)
        scopes.append(scope)
        if stage == "signal":
            controller.receive(signal.SIGTERM)
        return scope

    def observed_close(scope: PosixScope, *, deadline: float) -> None:
        close_deadlines.append(deadline)
        real_close(scope, deadline=deadline)

    def remaining(bound: float) -> float:
        # Only the execution-slice check after real registration expires. The
        # original absolute teardown bound remains untouched and observable.
        return 0 if stage == "expired" and scopes else real_remaining(bound)

    def unavailable_spawn(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        spawn_attempts.append(True)
        raise OSError("No diagnostic child was created")

    try:
        with monkeypatch.context() as boundary:
            boundary.setenv(ENVIRONMENT_KEY, parent.capability.encode())
            boundary.setattr(PosixScope, "create", observed_create)
            boundary.setattr(PosixScope, "close", observed_close)
            boundary.setattr(process, "seconds_remaining", remaining)
            boundary.setattr(subprocess, "Popen", unavailable_spawn)
            with (tmp_path / "output").open("wb") as output:
                with process.controlled_termination_signals() as controller:
                    try:
                        diagnostic.write_diagnostic(
                            output.fileno(),
                            b"output",
                            deadline=deadline,
                            signal_controller=controller,
                        )
                    except process.RunnerSignalInterrupt as error:
                        assert stage == "signal"
                        assert error.signal_number == signal.SIGTERM
                    else:
                        assert stage != "signal"
        assert len(scopes) == 1
        assert not scopes[0].capability.directory.exists()
        assert close_deadlines == [deadline]
        assert spawn_attempts == ([True] if stage == "spawn" else [])
        assert not parent.has_pending(deadline=deadline)
    finally:
        # If a regression fails to close, the fixture knows Popen never returned
        # a child and can discharge only that exact synthetic obligation.
        for scope in scopes:
            if scope.capability.directory.exists():
                scope.proven_empty = True
                real_close(scope, deadline=deadline)
        parent.proven_empty = True
        real_close(parent, deadline=deadline)


@pytest.mark.parametrize("failure", ["launch", "body", "retained-error"])
@pytest.mark.parametrize("signal_number", [signal.SIGINT, signal.SIGTERM])
def test_terminal_writer_fixture_cleans_before_restoring_signal_handlers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    signal_number: int,
) -> None:
    start = process.start_owned_process
    terminate = process.terminate_owned_tree
    close = process.close_owner
    pipe = os.pipe
    owners: list[tuple[process.OwnedProcess, float]] = []
    closed: list[tuple[process.OwnedProcess, float, bool]] = []
    pipes: list[tuple[int, int]] = []
    cleanup_signals: list[int] = []
    body_failed = False
    checked = False

    def record_pipe() -> tuple[int, int]:
        descriptors = pipe()
        if not pipes:
            pipes.append(descriptors)
        return descriptors

    def start_and_interrupt(
        command: process.ProcessCommand,
        *,
        deadline: float,
        signal_controller: process.SignalController | None,
    ) -> process.OwnedProcess:
        assert signal_controller is not None
        assert signal_controller.defer_depth > 0
        owner = start(command, deadline=deadline, signal_controller=signal_controller)
        owners.append((owner, deadline))
        if failure in {"launch", "retained-error"}:
            os.kill(os.getpid(), signal_number)
        if failure == "retained-error":
            error = process.ProcessOwnershipError("retained diagnostic fixture owner")
            error.owner = owner
            raise error
        return owner

    def interrupt_cleanup(
        owner: process.OwnedProcess,
        *,
        deadline: float,
        diagnostic: Callable[[str], None] | None = None,
    ) -> bool:
        nonlocal body_failed
        if failure == "body" and not body_failed:
            body_failed = True
            raise RuntimeError("fixture body failed before cleanup")
        cleanup_signals.append(signal_number)
        os.kill(os.getpid(), signal_number)
        return terminate(owner, deadline=deadline, diagnostic=diagnostic)

    def record_close(
        owner: process.OwnedProcess,
        *,
        deadline: float,
        diagnostic: Callable[[str], None] | None = None,
    ) -> bool:
        result = close(owner, deadline=deadline, diagnostic=diagnostic)
        closed.append((owner, deadline, result))
        return result

    # If the fixture restores handlers too early, the second real signal reaches
    # this controller instead of terminating pytest. It still interrupts the
    # fixture cleanup, so only its own close can satisfy the oracle below.
    with process.controlled_termination_signals() as controller:
        try:
            with monkeypatch.context() as boundary:
                boundary.setattr(os, "pipe", record_pipe)
                boundary.setattr(process, "start_owned_process", start_and_interrupt)
                boundary.setattr(process, "terminate_owned_tree", interrupt_cleanup)
                boundary.setattr(process, "close_owner", record_close)
                with pytest.raises(
                    (KeyboardInterrupt, process.RunnerSignalInterrupt)
                ) as caught:
                    test_terminal_writer_obligation_prevents_force_kill_before_real_reap(
                        tmp_path, monkeypatch
                    )
            assert len(owners) == 1
            owner, deadline = owners[0]
            assert owner.posix_scope is not None
            (tmp_path / "fixture-cleanup-before-rescue.json").write_text(
                json.dumps(
                    {
                        "failure": failure,
                        "signal": signal_number,
                        "pid": owner.process.pid,
                        "original_deadline": deadline,
                        "closed": [(bound, result) for _, bound, result in closed],
                        "scope_removed": not owner.posix_scope.capability.directory.exists(),
                        "returncode": owner.process.returncode,
                        "cleanup_signals": cleanup_signals,
                    }
                )
            )
            assert closed == [(owner, deadline, True)]
            assert not owner.posix_scope.capability.directory.exists()
            assert cleanup_signals == [signal_number]
            if signal_number == signal.SIGINT:
                assert isinstance(caught.value, KeyboardInterrupt)
            else:
                assert isinstance(caught.value, process.RunnerSignalInterrupt)
                assert caught.value.signal_number == signal_number
            with pytest.raises(ProcessLookupError):
                os.killpg(owner.process.pid, 0)
            assert len(pipes) == 1
            for descriptor in pipes[0]:
                with pytest.raises(OSError) as descriptor_error:
                    os.fstat(descriptor)
                assert descriptor_error.value.errno == errno.EBADF
            checked = True
        finally:
            if not checked:
                # Failure-only rescue follows the fixture's recorded outcome;
                # it cannot manufacture its close evidence or extend its bound.
                with controller.defer():
                    for owner, deadline in owners:
                        if (owner, deadline, True) not in closed:
                            assert terminate(owner, deadline=deadline)
                            assert close(owner, deadline=deadline)
                    for descriptors in pipes:
                        for descriptor in descriptors:
                            try:
                                os.close(descriptor)
                            except OSError as error:
                                assert error.errno == errno.EBADF


def test_terminal_writer_fixture_closes_pipe_when_signal_setup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipe = os.pipe
    pipes: list[tuple[int, int]] = []
    setup_error = RuntimeError("signal controller entry failed")

    def record_pipe() -> tuple[int, int]:
        descriptors = pipe()
        pipes.append(descriptors)
        return descriptors

    def refuse_handler(number: int, handler: Any) -> None:
        raise setup_error

    try:
        with monkeypatch.context() as boundary:
            boundary.setattr(os, "pipe", record_pipe)
            boundary.setattr(signal, "signal", refuse_handler)
            with pytest.raises(RuntimeError) as caught:
                test_terminal_writer_obligation_prevents_force_kill_before_real_reap(
                    tmp_path, monkeypatch
                )
        assert caught.value is setup_error
        assert len(pipes) == 1
        for descriptor in pipes[0]:
            with pytest.raises(OSError) as descriptor_error:
                os.fstat(descriptor)
            assert descriptor_error.value.errno == errno.EBADF
    finally:
        # Inspect EBADF before rescue; handler entry failed before any launch.
        for descriptors in pipes:
            for descriptor in descriptors:
                try:
                    os.close(descriptor)
                except OSError as error:
                    assert error.errno == errno.EBADF
