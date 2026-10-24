from __future__ import annotations

import os
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from h2h_dev_tools import process
from h2h_dev_tools import review_execution as execution

pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="Online partition owners require POSIX; offline review remains portable.",
)


def _invocation(root: Path, name: str, code: str = "pass") -> execution.Invocation:
    return execution.Invocation(
        name, (sys.executable, "-I", "-c", code), "immutable prompt", root / name
    )


def _track(monkeypatch: pytest.MonkeyPatch) -> list[process.OwnedProcess]:
    owners: list[process.OwnedProcess] = []
    original = process.start_owned_process

    def start(*args: Any, **kwargs: Any) -> process.OwnedProcess:
        owner = original(*args, **kwargs)
        owners.append(owner)
        return owner

    monkeypatch.setattr(process, "start_owned_process", start)
    return owners


def _empty(owners: list[process.OwnedProcess]) -> None:
    assert owners
    for owner in owners:
        assert owner.process.poll() is not None
        with pytest.raises(ProcessLookupError):
            os.killpg(owner.process.pid, 0)
        assert owner.posix_scope is not None
        assert owner.posix_scope.proven_empty
        assert not owner.posix_scope.capability.directory.exists()


def test_each_admission_rechecks_deadline_after_previous_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owners = _track(monkeypatch)
    original = process.start_owned_process

    def delayed(*args: Any, **kwargs: Any) -> process.OwnedProcess:
        owner = original(*args, **kwargs)
        time.sleep(0.2)
        return owner

    monkeypatch.setattr(process, "start_owned_process", delayed)
    with pytest.raises(execution.ExecutionError, match="deadline"):
        execution.run_invocations(
            tmp_path,
            [_invocation(tmp_path, str(i)) for i in range(5)],
            deadline=time.monotonic() + 6.15,
            validate=lambda _: None,
        )
    assert len(owners) == 1
    assert not (tmp_path / "1").exists()
    _empty(owners)


def test_prompt_export_expiry_does_not_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owners = _track(monkeypatch)
    original = Path.write_text

    def slow(self: Path, *args: Any, **kwargs: Any) -> int:
        result = original(self, *args, **kwargs)
        time.sleep(0.2)
        return result

    monkeypatch.setattr(Path, "write_text", slow)
    with pytest.raises(execution.ExecutionError, match="deadline"):
        execution.run_invocations(
            tmp_path,
            [_invocation(tmp_path, "slow")],
            deadline=time.monotonic() + 6.1,
            validate=lambda _: None,
        )
    assert owners == []


@pytest.mark.parametrize(
    "fault",
    [
        "nonzero",
        "validation",
        "late-validation",
        "timeout",
        "owner-death",
        "surviving-child",
    ],
)
def test_failure_closes_all_active_owners_and_stops_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    owners = _track(monkeypatch)
    blocker = "import time; time.sleep(60)"
    code = {
        "nonzero": "raise SystemExit(7)",
        "validation": "pass",
        "late-validation": "pass",
        "timeout": blocker,
        "owner-death": "import os, signal; os.kill(os.getppid(), signal.SIGKILL)",
        "surviving-child": 'import subprocess,sys; subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"])',
    }[fault]
    invocations = [
        _invocation(tmp_path, "first", code),
        *[_invocation(tmp_path, str(i), blocker) for i in range(5)],
    ]
    validated: list[str] = []

    def validate(item: execution.Invocation) -> None:
        validated.append(item.id)
        if fault == "validation":
            raise ValueError("synthetic malformed/finding evidence")
        if fault == "late-validation":
            time.sleep(0.6)

    with pytest.raises((execution.ExecutionError, ValueError)):
        execution.run_invocations(
            tmp_path, invocations, deadline=time.monotonic() + 6.5, validate=validate
        )
    assert 1 <= len(owners) <= execution.CONCURRENCY
    assert not (tmp_path / "3").exists(), (
        "Failure must close admission before a new wave."
    )
    _empty(owners)


def test_late_passing_output_cannot_rescue_expired_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owners = _track(monkeypatch)
    code = 'import signal,time; from pathlib import Path; signal.signal(signal.SIGTERM, lambda *_: (Path("late.json").write_text("{\\"verdict\\":\\"pass\\"}"), exit(0))); time.sleep(60)'
    validated: list[str] = []
    with pytest.raises(execution.ExecutionError, match="deadline"):
        execution.run_invocations(
            tmp_path,
            [_invocation(tmp_path, "late", code)],
            deadline=time.monotonic() + 6.4,
            validate=lambda item: validated.append(item.id),
        )
    assert (tmp_path / "late.json").exists()
    assert validated == []
    _empty(owners)


def test_stream_descriptors_are_closed_after_each_wave(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owners = _track(monkeypatch)
    observed: list[int] = []
    original = process.start_owned_process

    def start(phase: process.ProcessCommand, **kwargs: Any) -> process.OwnedProcess:
        if len(owners) >= execution.CONCURRENCY:
            assert all(owner.process.stdout is None for owner in owners)
        observed.append(len(list(Path("/dev/fd").iterdir())))
        return original(phase, **kwargs)

    monkeypatch.setattr(process, "start_owned_process", start)
    results = execution.run_invocations(
        tmp_path,
        [_invocation(tmp_path, str(i)) for i in range(32)],
        deadline=time.monotonic() + 15,
        validate=lambda _: None,
    )
    assert len(results) == 32
    assert max(observed) - min(observed) < 24
    _empty(owners)


def test_integration_reserve_is_part_of_original_budget() -> None:
    assert execution.shard_deadline(100, 3700, 51) == 3100
    assert execution.shard_deadline(100, 1000, 3) == 820
    assert execution.shard_deadline(100, 1000, 1) == 1000
    with pytest.raises(execution.ExecutionError):
        execution.shard_deadline(100, 110, 2)


def _wait_for_orphan_group_exit(group: int, *, deadline: float) -> None:
    while True:
        try:
            os.killpg(group, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            # XNU can return EPERM while an exited group awaits reaping. It is
            # still unknown, never proof of absence: require ESRCH below the
            # original fixture deadline just as for a group that remains live.
            pass
        assert time.monotonic() < deadline, (
            "Synthetic nested leaf cleanup remained unproven"
        )
        time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="XNU reports EPERM for an exited but unreaped process group",
)
def test_orphan_group_exit_waits_for_actual_reap_after_native_eperm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deadline = time.monotonic() + 5
    observations: list[str] = []
    real_killpg = os.killpg
    with process.controlled_termination_signals() as controller:
        owner: process.OwnedProcess | None = None
        try:
            with controller.defer():
                owner = process.start_owned_process(
                    process.ProcessCommand(
                        "known no-descendant exit",
                        (sys.executable, "-I", "-S", "-c", "pass"),
                        tmp_path,
                    ),
                    deadline=deadline,
                    signal_controller=controller,
                )
            # Observe real exit without consuming the child's wait status. No
            # sleep duration is used as evidence that the process has exited.
            status = None
            while status is None:
                status = os.waitid(
                    os.P_PID, owner.process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG
                )
                assert time.monotonic() < deadline
                time.sleep(0.001)
            assert status.si_pid == owner.process.pid
            assert status.si_code == os.CLD_EXITED
            assert status.si_status == 0
            assert owner.process.returncode is None

            def observe_and_reap(group: int, number: int) -> None:
                try:
                    real_killpg(group, number)
                except PermissionError:
                    observations.append("permission-denied")
                    # Real OS reaping causes the next probe to observe ESRCH.
                    # Re-raise the genuine first result to the fixture oracle.
                    assert owner.process.wait(timeout=deadline - time.monotonic()) == 0
                    raise
                except ProcessLookupError:
                    observations.append("absent")
                    raise

            with monkeypatch.context() as boundary:
                boundary.setattr(os, "killpg", observe_and_reap)
                _wait_for_orphan_group_exit(owner.process.pid, deadline=deadline)
            assert observations == ["permission-denied", "absent"]
            with pytest.raises(ChildProcessError):
                os.waitpid(owner.process.pid, os.WNOHANG)
        finally:
            with controller.defer():
                if owner is not None:
                    assert process.terminate_owned_tree(owner, deadline=deadline)
                    assert process.close_owner(owner, deadline=deadline)


@pytest.mark.parametrize("denied", [False, True])
def test_orphan_group_exit_requires_absence_within_original_deadline(
    monkeypatch: pytest.MonkeyPatch, denied: bool
) -> None:
    calls: list[int] = []

    def unresolved(group: int, number: int) -> None:
        assert group == 31337
        assert number == 0
        calls.append(group)
        if denied:
            raise PermissionError("No absence evidence")

    with monkeypatch.context() as boundary:
        boundary.setattr(os, "killpg", unresolved)
        started = time.monotonic()
        deadline = started + 0.03
        with pytest.raises(AssertionError, match="cleanup remained unproven"):
            _wait_for_orphan_group_exit(31337, deadline=deadline)
        assert time.monotonic() >= deadline
        assert time.monotonic() - started < 1
    assert len(calls) >= 2


@pytest.mark.skipif(sys.platform != "darwin", reason="XNU orphan-group fixture")
@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize("boundary", ["launch", "cleanup"])
def test_orphan_fixture_cleans_up_when_launch_handoff_is_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signum: int, boundary: str
) -> None:
    original = process.start_owned_process
    close = process.close_owner
    owners: list[tuple[process.OwnedProcess, float]] = []
    closed: list[tuple[process.OwnedProcess, float, bool]] = []
    pending: list[int | None] = []
    controllers: list[process.SignalController | None] = []

    def send_signal(controller: process.SignalController | None) -> None:
        os.kill(os.getpid(), signum)
        assert controller is not None and controller.defer_depth > 0
        pending.append(controller.pending_signal)

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
        if boundary == "cleanup":
            send_signal(controllers[0])
        result = close(owner, deadline=deadline, diagnostic=diagnostic)
        closed.append((owner, deadline, result))
        return result

    monkeypatch.setattr(process, "start_owned_process", interrupt)
    monkeypatch.setattr(process, "close_owner", record_close)
    expected = (
        KeyboardInterrupt if signum == signal.SIGINT else process.RunnerSignalInterrupt
    )
    try:
        with pytest.raises(expected):
            test_orphan_group_exit_waits_for_actual_reap_after_native_eperm(
                tmp_path, monkeypatch
            )
        assert len(owners) == 1
        owner, deadline = owners[0]
        assert closed == [(owner, deadline, True)]
        assert pending == [signum]
        assert owner.process.poll() is not None
        assert owner.posix_scope is not None
        assert not owner.posix_scope.capability.directory.exists()
        with pytest.raises(ProcessLookupError):
            os.killpg(owner.process.pid, 0)
    finally:
        failure = sys.exception()
        for owner, deadline in owners:
            if (owner, deadline, True) not in closed:
                try:
                    with process.controlled_termination_signals() as controller:
                        with controller.defer():
                            empty = process.terminate_owned_tree(
                                owner, deadline=deadline
                            )
                            released = close(owner, deadline=deadline)
                            assert empty and released
                except BaseException as rescue_error:
                    if failure is None:
                        raise
                    failure.add_note(
                        f"Owner boundary control rescue also failed: {rescue_error}"
                    )


def test_unexpected_registered_nested_owner_death_cannot_prove_cleanup(
    tmp_path: Path,
) -> None:
    import json
    import subprocess

    package = Path(process.__file__).parent
    # Isolate the deliberately unrecoverable registration from pytest's own
    # canonical parent. This test retains the failed ledger as evidence.
    environment = dict(
        os.environ, TMPDIR=str(tmp_path), TMP=str(tmp_path), TEMP=str(tmp_path)
    )
    environment.pop("H2H_PRIVATE_POSIX_PROCESS_SCOPE", None)
    nested = tmp_path / "nested.py"
    nested.write_text(
        "import os,runpy,sys,time\nfrom pathlib import Path\n"
        f"package=Path({str(package)!r})\n"
        'runpy.run_path(str(package/"source_loader.py"))["load_package"](package,"h2h_dev_tools")\n'
        "from h2h_dev_tools import process\n"
        'owner=process.start_owned_process(process.ProcessCommand("nested leaf", (sys.executable,"-c","import time; time.sleep(60)"), Path.cwd()), deadline=time.monotonic()+30, signal_controller=None)\n'
        'Path("leaf-pid").write_text(str(owner.process.pid))\n'
        "os._exit(0)\n"
    )
    outer = tmp_path / "outer.py"
    outer.write_text(
        "import json,runpy,sys,time\nfrom pathlib import Path\n"
        f"package=Path({str(package)!r})\n"
        'runpy.run_path(str(package/"source_loader.py"))["load_package"](package,"h2h_dev_tools")\n'
        "from h2h_dev_tools import process,review_execution as execution\n"
        "try:\n"
        ' execution.run_invocations(Path.cwd(), [execution.Invocation("orphan", (sys.executable,"nested.py"), "", Path("invocation"))], deadline=time.monotonic()+9, validate=lambda _: Path("validated").touch())\n'
        "except process.ProcessOwnershipError as error:\n"
        ' Path("outcome.json").write_text(json.dumps({"unproven": not error.cleanup_proven}))\n'
        "else:\n"
        ' raise AssertionError("Unproven nested obligation accepted")\n'
    )
    with (tmp_path / "outer.log").open("wb") as output:
        child = subprocess.Popen(
            (sys.executable, "-I", str(outer)),
            cwd=tmp_path,
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        leaf: int | None = None
        try:
            assert child.wait(timeout=15) == 0, (tmp_path / "outer.log").read_text()
            leaf = int((tmp_path / "leaf-pid").read_text())
            assert json.loads((tmp_path / "outcome.json").read_text()) == {
                "unproven": True
            }
            assert not (tmp_path / "validated").exists()
            assert list(tmp_path.glob("h2h-process-scope-*/pending/*"))
            os.kill(leaf, 0)  # Outer group exit did NOT prove its nested leaf gone.
        finally:
            if leaf is None and (tmp_path / "leaf-pid").exists():
                leaf = int((tmp_path / "leaf-pid").read_text())
            for group in [child.pid, *([] if leaf is None else [leaf])]:
                try:
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            child.wait(timeout=5)
            until = time.monotonic() + 5
            if leaf is not None:
                _wait_for_orphan_group_exit(leaf, deadline=until)


@pytest.mark.parametrize("during", ["spawn-return", "prompt-export"])
def test_exited_failure_is_observed_before_admitting_another_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, during: str
) -> None:
    owners = _track(monkeypatch)
    original_start = process.start_owned_process
    original_write = Path.write_text
    release = tmp_path / "release-failure"

    def start(*args: Any, **kwargs: Any) -> process.OwnedProcess:
        owner = original_start(*args, **kwargs)
        if len(owners) == 1 and during == "spawn-return":
            assert owner.process.wait(timeout=5) == 7
        return owner

    def write(path: Path, *args: Any, **kwargs: Any) -> int:
        result = original_write(path, *args, **kwargs)
        if path == tmp_path / "second/prompt.txt" and during == "prompt-export":
            release.touch()
            assert owners[0].process.wait(timeout=5) == 7
        return result

    monkeypatch.setattr(process, "start_owned_process", start)
    monkeypatch.setattr(Path, "write_text", write)
    first = "raise SystemExit(7)"
    if during == "prompt-export":
        first = (
            "from pathlib import Path\nimport time\n"
            f"while not Path({str(release)!r}).exists(): time.sleep(0.01)\n" + first
        )
    with pytest.raises(execution.ExecutionError, match="exited with 7"):
        execution.run_invocations(
            tmp_path,
            [
                _invocation(tmp_path, "first", first),
                _invocation(tmp_path, "second", "import time; time.sleep(60)"),
                _invocation(tmp_path, "third", "import time; time.sleep(60)"),
            ],
            deadline=time.monotonic() + 15,
            validate=lambda _: None,
        )
    assert len(owners) == 1, "An already exited failure must close admission"
    assert not (tmp_path / "third").exists()
    _empty(owners)


@pytest.mark.parametrize("peer_ignores_term", [False, True])
def test_one_busy_admission_cannot_starve_other_review_cleanup(
    tmp_path: Path, peer_ignores_term: bool
) -> None:
    import json
    import textwrap

    package = Path(process.__file__).parent
    root = tmp_path / "isolated cleanup"
    root.mkdir()
    script = root / "probe.py"
    script.write_text(
        "import runpy\nfrom pathlib import Path\n"
        f"package = Path({str(package)!r})\n"
        'runpy.run_path(str(package / "source_loader.py"))["load_package"](package, "h2h_dev_tools")\n'
        f"peer_ignores_term = {peer_ignores_term!r}\n"
        + textwrap.dedent(
            r'''
            import fcntl, json, os, signal, sys, threading, time
            from h2h_dev_tools import _process_diagnostic, process, review_execution as execution

            root = Path.cwd()
            diagnostic_threads = []
            writer_calls = []
            write_diagnostic = _process_diagnostic.write_diagnostic
            def write(*args, **kwargs):
                writer_calls.append(threading.current_thread().name)
                return write_diagnostic(*args, **kwargs)
            _process_diagnostic.write_diagnostic = write
            diagnostic = process.report_diagnostic
            def report(message, **kwargs):
                assert threading.current_thread() is threading.main_thread()
                diagnostic_threads.append(threading.current_thread().name)
                diagnostic(message, **kwargs)
            process.report_diagnostic = report
            owners = []
            held = None
            start = process.start_owned_process
            def launch(*args, **kwargs):
                global held
                owner = start(*args, **kwargs)
                owners.append(owner)
                (root / "groups.json").write_text(json.dumps([o.process.pid for o in owners]))
                if len(owners) == 4:
                    until = time.monotonic() + 5
                    while not all((root / f"ready-{i}").exists() for i in range(4)):
                        assert time.monotonic() < until
                        time.sleep(.01)
                    scope = owners[1].posix_scope
                    assert scope is not None
                    held = (scope.capability.directory / "lock").open("rb")
                    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    (root / "release-failure").touch()
                return owner
            process.start_owned_process = launch
            child = r"""
            import signal, sys, time
            from pathlib import Path
            index = int(sys.argv[1])
            ignore_term = sys.argv[2] == "True"
            root = Path.cwd()
            def stop(*_):
                (root / f"term-{index}").touch()
                if not ignore_term: raise SystemExit(0)
            signal.signal(signal.SIGTERM, stop)
            (root / f"ready-{index}").touch()
            while True:
                if index == 0 and (root / "release-failure").exists():
                    raise SystemExit(7)
                time.sleep(.01)
            """
            import textwrap
            child = textwrap.dedent(child)
            invocations = [execution.Invocation(str(i), (sys.executable, "-I", "-c", child, str(i), str(peer_ignores_term)), "prompt", root / str(i)) for i in range(5)]
            before = {thread.ident for thread in threading.enumerate()}
            report = {}
            started = time.monotonic()
            deadline = started + 12
            try:
                try:
                    execution.run_invocations(root, invocations, deadline=deadline, validate=lambda _: None)
                except process.ProcessOwnershipError as error:
                    report["unproven"] = not error.cleanup_proven
                    report["error"] = str(error)
                else:
                    raise AssertionError("Busy scope must remain unproven")
                report["diagnostic_threads"] = diagnostic_threads
                report["writer_calls"] = writer_calls
                report["elapsed"] = time.monotonic() - started
                report["budget"] = deadline - started
                report["busy_scope"] = str(owners[1].posix_scope.capability.directory)
                report["admitted"] = len(owners)
                report["fifth_exists"] = (root / "4").exists()
                report["extra_threads"] = [thread.name for thread in threading.enumerate() if thread.ident not in before]
                report["peer_terms"] = [(root / f"term-{i}").exists() for i in (2, 3)]
                report["peer_empty"] = []
                for owner in owners[2:]:
                    owner.process.poll()
                    try:
                        os.killpg(owner.process.pid, 0)
                    except ProcessLookupError:
                        report["peer_empty"].append(True)
                    else:
                        report["peer_empty"].append(False)
                report["peer_scopes_removed"] = [not owner.posix_scope.capability.directory.exists() for owner in owners[2:]]
                report["busy_scope_retained"] = owners[1].posix_scope.capability.directory.exists()
                (root / "outcome.json").write_text(json.dumps(report))
            finally:
                if held is not None:
                    fcntl.flock(held, fcntl.LOCK_UN)
                    held.close()
                # Fixture rescue has its own authority, after recording failure.
                # It cannot contribute evidence to the failed scheduler run.
                until = time.monotonic() + 10
                for owner in owners:
                    scope = owner.posix_scope
                    if scope is not None and scope.capability.directory.exists():
                        assert process.terminate_owned_tree(owner, deadline=until)
                        scope.close(deadline=until)
                    owner.process.wait(timeout=5)
            '''
        ),
        encoding="utf-8",
    )
    environment = dict(os.environ, TMPDIR=str(root), TMP=str(root), TEMP=str(root))
    with (root / "probe.log").open("wb") as output:
        status = process.run_command(
            process.ProcessCommand(
                "isolated review cleanup proof",
                (sys.executable, "-I", "-B", str(script)),
                root,
                environment=environment,
                stdout=output,
                stderr=output,
            ),
            hard_deadline=time.monotonic() + 30,
        )
    assert status == 0, (root / "probe.log").read_text()
    report = json.loads((root / "outcome.json").read_text())
    assert report["unproven"] is True
    assert "Partitioned review cleanup could not be proven" in report["error"]
    assert f"Process scope lock deadline: {report['busy_scope']}" in report["error"]
    # An unproven owner carries its diagnostics in the exception. Even a
    # controlling-thread message would start a new optional writer too late.
    assert report["diagnostic_threads"] == []
    assert report["writer_calls"] == []
    assert report["elapsed"] < report["budget"] == 12
    assert report["admitted"] == 4 and not report["fifth_exists"]
    assert report["busy_scope_retained"] is True
    assert report["extra_threads"] == []
    assert report["peer_terms"] == [True, True], report
    assert report["peer_empty"] == [True, True], report
    assert report["peer_scopes_removed"] == [True, True], report


@pytest.mark.parametrize("fail_first_close", [False, True])
def test_finished_owner_closure_uses_explicit_phase_deadlines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_first_close: bool
) -> None:
    owners = _track(monkeypatch)
    close = process.close_owner
    terminate = process.terminate_owned_tree
    close_deadlines: list[float] = []
    terminate_deadlines: list[float] = []
    diagnostic_writes: list[str] = []

    def closing(owner: process.OwnedProcess, *, deadline: float, **kwargs: Any) -> bool:
        close_deadlines.append(deadline)
        assert callable(kwargs.get("diagnostic"))
        if fail_first_close and len(close_deadlines) == 1:
            kwargs["diagnostic"]("Unproven normal owner closure")
            return False
        return close(owner, deadline=deadline, **kwargs)

    def terminating(
        owner: process.OwnedProcess, *, deadline: float, **kwargs: Any
    ) -> bool:
        terminate_deadlines.append(deadline)
        assert callable(kwargs.get("diagnostic"))
        return terminate(owner, deadline=deadline, **kwargs)

    monkeypatch.setattr(process, "close_owner", closing)
    monkeypatch.setattr(process, "terminate_owned_tree", terminating)
    monkeypatch.setattr(
        process,
        "report_diagnostic",
        lambda message, **kwargs: diagnostic_writes.append(message),
    )
    deadline = time.monotonic() + 15

    def run() -> list[dict[str, object]]:
        return execution.run_invocations(
            tmp_path,
            [_invocation(tmp_path, "finished")],
            deadline=deadline,
            validate=lambda _: None,
        )

    if fail_first_close:
        with pytest.raises(
            execution.ExecutionError, match="cleanup could not be proven"
        ):
            run()
        assert len(close_deadlines) == 2
        assert terminate_deadlines == close_deadlines[1:]
        assert close_deadlines[1] <= deadline
    else:
        assert run() == [{"id": "finished", "exit_code": 0, "owned_tree_empty": True}]
        assert len(close_deadlines) == 1
        assert terminate_deadlines == []
    assert close_deadlines[0] == deadline - execution.CLEANUP_RESERVE
    assert diagnostic_writes == []
    _empty(owners)
