"""Real cooperating POSIX owners must prove every nested session has exited."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from h2h_dev_tools import process

pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="Exercises POSIX sessions and signals; Windows uses nested Job Objects",
)

_CHILD = r"""
import json
import os
from pathlib import Path
import runpy
import signal
import sys
import threading
import time

package, directory, scenario, level_text, depth_text = sys.argv[1:]
root = Path(directory)
level, depth = int(level_text), int(depth_text)
name = f"owner{level}" if level < depth else "leaf"

def record(path, value):
    temporary = root / (path + ".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    temporary.replace(root / path)

record(name + ".identity.json", {"pid": os.getpid(), "pgid": os.getpgrp()})
release = root / "fixture-release"
if release.exists():
    raise SystemExit(0)
if level == depth:
    if scenario.startswith("exit-"):
        raise SystemExit(int(scenario.removeprefix("exit-")))
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    (root / "leaf-ready").touch()
    while not release.exists():
        time.sleep(0.01)
    raise SystemExit(0)

package = Path(package)
sys.path.insert(0, str(package.parent))
runpy.run_path(str(package / "source_loader.py"))["load_package"](
    package, "h2h_dev_tools"
)
from h2h_dev_tools import process

control_finished = threading.Event()
control_threads = []
if scenario == "cancel-paused" and level == 0:
    original_start = process.start_owned_process
    def start_with_pause_control(phase, *, deadline, signal_controller):
        owner = original_start(
            phase, deadline=deadline, signal_controller=signal_controller)
        def pause_inner():
            paused = False
            try:
                while not (root / "pause-inner").exists():
                    if release.exists() or control_finished.is_set():
                        return
                    time.sleep(.001)
                # The original native handle belongs to this creating owner.
                # Never resolve a PID from the fixture's historical inventory.
                owner.process.send_signal(signal.SIGSTOP)
                paused = True
                (root / "inner-paused").touch()
                while not (root / "cancellation-delivered").exists():
                    if release.exists():
                        return
                    time.sleep(.001)
                time.sleep(.4)
            finally:
                if paused:
                    owner.process.send_signal(signal.SIGCONT)
                    (root / "inner-resumed").touch()
        worker = threading.Thread(target=pause_inner)
        control_threads.append(worker)
        worker.start()
        return owner
    process.start_owned_process = start_with_pause_control
if scenario == "killed-owner" and level == 1:
    def kill_self_when_requested():
        while not (root / "kill-inner").exists():
            if release.exists():
                return
            time.sleep(.001)
        os.kill(os.getpid(), signal.SIGKILL)
    threading.Thread(target=kill_self_when_requested, daemon=True).start()

# Delay real metadata operations without changing any ownership deadline.
metadata_mode = os.environ.get("H2H_TEST_METADATA_WINDOW")
if level == 1 and metadata_mode:
    parent = Path(json.loads(os.environ["H2H_PRIVATE_POSIX_PROCESS_SCOPE"])["directory"])
    original_mkdir, original_unlink = Path.mkdir, Path.unlink
    def metadata_pause(path, boundary):
        record("metadata-window.json", {"scope": str(path.parent), "boundary": boundary,
               "parent": str(parent), "parent_pending": len(list((parent / "pending").iterdir())),
               "local_names": sorted(item.name for item in path.parent.iterdir())})
        time.sleep(0.15)
    def observed_mkdir(path, *args, **kwargs):
        result = original_mkdir(path, *args, **kwargs)
        if metadata_mode == "create" and path.name == "pending" and (parent / "closed").exists():
            metadata_pause(path, "after-local-pending-mkdir-before-parent-registration")
        return result
    def observed_unlink(path, *args, **kwargs):
        result = original_unlink(path, *args, **kwargs)
        if metadata_mode == "close" and path.name == "closed" and (parent / "closed").exists():
            metadata_pause(path, "after-closed-unlink")
        return result
    Path.mkdir, Path.unlink = observed_mkdir, observed_unlink

command = (
    sys.executable, "-I", __file__, str(package), str(root), scenario,
    str(level + 1), str(depth),
)
environment = None
if level == 1:
    if scenario == "missing-executable":
        command = (str(root / "missing-executable"),)
    elif scenario == "empty-argv":
        command = ()
    elif scenario == "nul-argv":
        command = (sys.executable, "\0")
    elif scenario == "invalid-env-key":
        environment = {"INVALID=KEY": "value"}
    elif scenario == "nul-env-key":
        environment = {"INVALID\0KEY": "value"}
    elif scenario == "nul-env-value":
        environment = {"KEY": "invalid\0value"}
    elif scenario == "invalid-env-value-type":
        environment = {"KEY": 1}
phase = process.ProcessCommand(name, command, root, environment=environment)

try:
    if scenario == "phases" and level == 1:
        # The outer owner still owns this live command while the inner owner
        # has no currently running child. Its next phase must be refused once
        # the outer owner closes admission, including across this exact gap.
        first = process.ProcessCommand(
            "first phase", (sys.executable, "-I", "-c", "pass"), root
        )
        assert process.run_command(first, hard_deadline=None) == 0
        signal.signal(signal.SIGTERM, lambda *_: (root / "gap-term").touch())
        (root / "between-phases").touch()
        while not (root / "admit-next-phase").exists():
            if release.exists():
                raise SystemExit(0)
            time.sleep(0.01)
        status = process.run_command(phase, hard_deadline=None)
    elif scenario == "scope-lock" and level == 1:
        from contextlib import contextmanager
        from h2h_dev_tools import _process_scope
        original_lock = _process_scope._Capability.locked
        parent_directory = Path(json.loads(
            os.environ[_process_scope.ENVIRONMENT_KEY]
        )["directory"])
        @contextmanager
        def hold_parent_registration_lock(capability, *, deadline):
            with original_lock(capability, deadline=deadline):
                if capability.directory == parent_directory:
                    (root / "scope-lock-held").touch()
                    # A stopped owner can hold a genuine flock indefinitely.
                    # Outer cleanup still owes a bounded, unproven result.
                    while not release.exists():
                        time.sleep(0.01)
                yield
        _process_scope._Capability.locked = hold_parent_registration_lock
        status = process.run_command(phase, hard_deadline=None)
    elif scenario == "startup" and level == 1:
        with process.controlled_termination_signals() as controller:
            original_start = process._start_posix_process
            def start_after_actual_signal(command):
                (root / "registered-before-spawn").touch()
                while controller.pending_signal is None:
                    if release.exists():
                        raise ValueError("fixture released before spawn")
                    time.sleep(0.01)
                return original_start(command)
            process._start_posix_process = start_after_actual_signal
            status = process.run_command(
                phase, hard_deadline=None, signal_controller=controller
            )
    else:
        status = process.run_command(phase, hard_deadline=None)
    result = {"kind": "return", "status": status}
except process.RunnerSignalInterrupt as error:
    status = 128 + error.signal_number
    result = {"kind": "interrupted", "status": status}
except process.ProcessOwnershipError as error:
    status = 125
    result = {
        "kind": "ownership-error", "status": status,
        "cleanup_proven": error.cleanup_proven,
    }
    if scenario in {
        "missing-executable", "empty-argv", "nul-argv", "invalid-env-key",
        "nul-env-key", "nul-env-value", "invalid-env-value-type",
    }:
        result["cause"] = type(error.__cause__).__name__
finally:
    control_finished.set()
    for worker in control_threads:
        worker.join(timeout=2)
        assert not worker.is_alive()
record(name + ".result.json", result)
raise SystemExit(status)
"""


@dataclass(frozen=True)
class _Identity:
    pid: int
    pgid: int


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@dataclass
class _Probe:
    root: Path
    owner: subprocess.Popen[bytes]
    processes_proven_empty: bool = False

    def wait_for(self, filename: str) -> None:
        deadline = time.monotonic() + 15
        while not (self.root / filename).exists():
            assert self.owner.poll() is None, self.log()
            assert time.monotonic() < deadline, self.log()
            time.sleep(0.01)

    def identities(self) -> dict[str, _Identity]:
        result: dict[str, _Identity] = {}
        for path in self.root.glob("*.identity.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            result[path.name.removesuffix(".identity.json")] = _Identity(
                data["pid"], data["pgid"]
            )
        return result

    def log(self) -> str:
        return (self.root / "owner.log").read_text(encoding="utf-8")

    def result(self, name: str = "owner0") -> dict[str, object]:
        data: dict[str, object] = json.loads(
            (self.root / f"{name}.result.json").read_text(encoding="utf-8")
        )
        return data

    def assert_all_exited(self) -> None:
        assert not {
            name: identity.pid
            for name, identity in self.identities().items()
            if _alive(identity.pid)
        }, self.log()
        for identity in self.identities().values():
            with pytest.raises(ProcessLookupError):
                os.killpg(identity.pgid, 0)
        self.processes_proven_empty = True

    def assert_no_scopes(self) -> None:
        assert not list((self.root / "scopes").glob("h2h-process-scope-*")), self.log()


@contextmanager
def _probe(
    tmp_path: Path,
    *,
    scenario: str,
    depth: int = 2,
    metadata_window: str | None = None,
) -> Iterator[_Probe]:
    script = tmp_path / "probe.py"
    script.write_text(_CHILD, encoding="utf-8")
    scopes = tmp_path / "scopes"
    scopes.mkdir()
    environment = dict(
        os.environ, TMPDIR=str(scopes), TMP=str(scopes), TEMP=str(scopes)
    )
    # This disposable fault experiment owns its own tree. An intentionally
    # killed supervisor must not leave a stale obligation in pytest's owner.
    environment.pop("H2H_PRIVATE_POSIX_PROCESS_SCOPE", None)
    environment.pop("H2H_TEST_METADATA_WINDOW", None)
    if metadata_window is not None:
        environment["H2H_TEST_METADATA_WINDOW"] = metadata_window
    package = Path(process.__file__).resolve().parent
    with (
        (tmp_path / "owner.log").open("wb") as output,
        process.controlled_termination_signals() as controller,
    ):
        probe: _Probe | None = None
        try:
            with controller.defer():
                owner = subprocess.Popen(
                    (
                        sys.executable,
                        "-I",
                        str(script),
                        str(package),
                        str(tmp_path),
                        scenario,
                        "0",
                        str(depth),
                    ),
                    env=environment,
                    cwd=tmp_path,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                probe = _Probe(tmp_path, owner)
            yield probe
        finally:
            with controller.defer():
                if probe is not None and not probe.processes_proven_empty:
                    # A historical PID/PGID grants no signal authority. These
                    # fixed actors release only on this failure-rescue marker;
                    # normal assertions must prove exit before it is written.
                    (tmp_path / "fixture-release").touch()
                    probe.owner.wait(timeout=5)
                    deadline = time.monotonic() + 8
                    while True:
                        survivors = [
                            item.pid
                            for item in probe.identities().values()
                            if _alive(item.pid)
                        ]
                        if not survivors or time.monotonic() >= deadline:
                            break
                        time.sleep(0.02)
                    assert not survivors, f"Probe actors survived release: {survivors}"


@pytest.mark.parametrize("delay_inner", [False, True])
@pytest.mark.parametrize("depth", [2, 3])
def test_outer_cancellation_waits_for_nested_session_cleanup(
    tmp_path: Path, delay_inner: bool, depth: int
) -> None:
    scenario = "cancel-paused" if delay_inner else "cancel"
    with _probe(tmp_path, scenario=scenario, depth=depth) as probe:
        probe.wait_for("leaf-ready")
        if delay_inner:
            (tmp_path / "pause-inner").touch()
            probe.wait_for("inner-paused")
        started = time.monotonic()
        probe.owner.send_signal(signal.SIGTERM)
        if delay_inner:
            # The creating owner holds the real STOP for 0.4s after cancellation,
            # then resumes through the same native identity even on failure.
            (tmp_path / "cancellation-delivered").touch()
        assert probe.owner.wait(timeout=10) == 143, probe.log()
        assert time.monotonic() - started < 8
        if delay_inner:
            assert (tmp_path / "inner-resumed").exists()
            assert time.monotonic() - started >= 0.4
        assert probe.result()["kind"] == "interrupted"
        assert len(probe.identities()) == depth + 1
        probe.assert_all_exited()
        probe.assert_no_scopes()


@pytest.mark.parametrize("status", [0, 7])
def test_nested_completion_releases_all_obligations(
    tmp_path: Path, status: int
) -> None:
    with _probe(tmp_path, scenario=f"exit-{status}", depth=3) as probe:
        assert probe.owner.wait(timeout=10) == status, probe.log()
        assert probe.result() == {"kind": "return", "status": status}
        assert len(probe.identities()) == 4
        probe.assert_all_exited()
        probe.assert_no_scopes()


@pytest.mark.parametrize(
    ("scenario", "cause"),
    [
        ("missing-executable", "FileNotFoundError"),
        ("empty-argv", "IndexError"),
        ("nul-argv", "ValueError"),
        ("invalid-env-key", "ValueError"),
        ("nul-env-key", "ValueError"),
        ("nul-env-value", "ValueError"),
        ("invalid-env-value-type", "TypeError"),
    ],
)
def test_failed_nested_exec_releases_obligation_without_hiding_failure(
    tmp_path: Path,
    scenario: str,
    cause: str,
) -> None:
    with _probe(tmp_path, scenario=scenario) as probe:
        assert probe.owner.wait(timeout=10) == 125, probe.log()
        assert probe.result() == {"kind": "return", "status": 125}
        assert probe.result("owner1")["kind"] == "ownership-error"
        assert probe.result("owner1")["cause"] == cause
        assert set(probe.identities()) == {"owner0", "owner1"}
        assert "owned_tree_empty=True" in probe.log()
        probe.assert_all_exited()
        probe.assert_no_scopes()


def test_outer_close_rejects_inner_next_phase(tmp_path: Path) -> None:
    with _probe(tmp_path, scenario="phases") as probe:
        probe.wait_for("between-phases")
        probe.owner.send_signal(signal.SIGTERM)
        probe.wait_for("gap-term")
        (tmp_path / "admit-next-phase").touch()
        assert probe.owner.wait(timeout=10) == 143, probe.log()
        assert probe.result("owner1")["kind"] == "ownership-error"
        assert not (tmp_path / "leaf.identity.json").exists()
        probe.assert_all_exited()
        probe.assert_no_scopes()


def test_registered_startup_is_owned_before_new_session_exists(tmp_path: Path) -> None:
    with _probe(tmp_path, scenario="startup") as probe:
        probe.wait_for("registered-before-spawn")
        probe.owner.send_signal(signal.SIGTERM)
        assert probe.owner.wait(timeout=10) == 143, probe.log()
        assert probe.result("owner1")["kind"] == "interrupted"
        probe.assert_all_exited()
        probe.assert_no_scopes()


def test_killed_nested_owner_cannot_publish_empty_tree(tmp_path: Path) -> None:
    with _probe(tmp_path, scenario="killed-owner") as probe:
        probe.wait_for("leaf-ready")
        identities = probe.identities()
        started = time.monotonic()
        (tmp_path / "kill-inner").touch()
        assert probe.owner.wait(timeout=10) == 125, probe.log()
        assert time.monotonic() - started < 8
        assert probe.result() == {
            "kind": "ownership-error",
            "status": 125,
            "cleanup_proven": False,
        }
        assert _alive(identities["leaf"].pid)
        assert "owned_tree_empty=True" not in probe.log()
        assert list((tmp_path / "scopes").glob("h2h-process-scope-*/pending/*"))


def test_scope_lock_contention_is_bounded_and_never_proves_empty(
    tmp_path: Path,
) -> None:
    with _probe(tmp_path, scenario="scope-lock") as probe:
        probe.wait_for("scope-lock-held")
        inner = probe.identities()["owner1"]
        started = time.monotonic()
        probe.owner.send_signal(signal.SIGTERM)
        assert probe.owner.wait(timeout=10) == 125, probe.log()
        assert time.monotonic() - started < 8
        assert probe.result() == {
            "kind": "ownership-error",
            "status": 125,
            "cleanup_proven": False,
        }
        assert _alive(inner.pid)
        assert not (tmp_path / "leaf.identity.json").exists()
        assert "owned_tree_empty=True" not in probe.log()
        assert list((tmp_path / "scopes").glob("h2h-process-scope-*"))


@pytest.mark.parametrize("boundary", ["create", "close"])
def test_cancellation_cannot_leave_unowned_scope_metadata(
    tmp_path: Path, boundary: str
) -> None:
    with _probe(tmp_path, scenario="cancel", metadata_window=boundary) as probe:
        probe.wait_for("leaf-ready")
        started = time.monotonic()
        probe.owner.send_signal(signal.SIGTERM)
        assert probe.owner.wait(timeout=10) == 143, probe.log()
        assert time.monotonic() - started < 8
        assert probe.result()["kind"] == "interrupted"
        probe.assert_all_exited()
        probe.assert_no_scopes()
        marker = tmp_path / "metadata-window.json"
        if boundary == "create":
            # Closed admission must reject before any unpublished allocation.
            assert not marker.exists()
        else:
            observed = json.loads(marker.read_text())
            assert observed["parent_pending"] == 1
            assert observed["local_names"] == ["lock", "pending", "token"]


def test_probe_does_not_rescue_or_signal_after_complete_process_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    killpg = os.killpg

    def reject_historical_signal(group: int, number: int) -> None:
        assert number == 0, "completed probe signalled a historical PGID"
        killpg(group, number)

    monkeypatch.setattr(os, "killpg", reject_historical_signal)
    with _probe(tmp_path, scenario="exit-0") as probe:
        assert probe.owner.wait(timeout=10) == 0, probe.log()
        probe.assert_all_exited()
        probe.assert_no_scopes()
        assert not (tmp_path / "fixture-release").exists()
    assert probe.processes_proven_empty
    assert not (tmp_path / "fixture-release").exists()


@pytest.mark.parametrize(
    ("scenario", "ready"),
    [
        ("cancel", "leaf-ready"),
        ("cancel-paused", "inner-paused"),
        ("phases", "between-phases"),
        ("startup", "registered-before-spawn"),
        ("scope-lock", "scope-lock-held"),
    ],
)
def test_failed_probe_releases_fixed_actors_without_historical_signals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str, ready: str
) -> None:
    killpg = os.killpg
    probe: _Probe | None = None

    def reject_historical_signal(group: int, number: int) -> None:
        assert number == 0, "failed probe guessed signal authority from a PGID"
        killpg(group, number)

    monkeypatch.setattr(os, "killpg", reject_historical_signal)
    try:
        with pytest.raises(AssertionError, match="injected fixture assertion"):
            with _probe(tmp_path, scenario=scenario) as probe:
                if scenario == "cancel-paused":
                    probe.wait_for("leaf-ready")
                    (tmp_path / "pause-inner").touch()
                probe.wait_for(ready)
                assert not (tmp_path / "fixture-release").exists()
                assert not probe.processes_proven_empty
                raise AssertionError("injected fixture assertion before cleanup")
        assert (tmp_path / "fixture-release").exists()
        assert probe is not None
        probe.assert_all_exited()
        probe.assert_no_scopes()
    finally:
        # Keep this negative control safe against the old teardown: that path
        # is rejected before its numeric kill and never writes the release.
        if probe is not None and not probe.processes_proven_empty:
            (tmp_path / "fixture-release").touch()
            deadline = time.monotonic() + 8
            probe.owner.wait(timeout=max(0, deadline - time.monotonic()))
            while any(_alive(item.pid) for item in probe.identities().values()):
                assert time.monotonic() < deadline, probe.log()
                time.sleep(0.01)
