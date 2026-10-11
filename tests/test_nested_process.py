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
if level == depth:
    if scenario.startswith("exit-"):
        raise SystemExit(int(scenario.removeprefix("exit-")))
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    (root / "leaf-ready").touch()
    while True:
        time.sleep(0.01)

package = Path(package)
sys.path.insert(0, str(package.parent))
runpy.run_path(str(package / "source_loader.py"))["load_package"](
    package, "h2h_dev_tools"
)
from h2h_dev_tools import process

command = (
    sys.executable, "-I", __file__, str(package), str(root), scenario,
    str(level + 1), str(depth),
)
if scenario == "missing-executable" and level == 1:
    command = (str(root / "missing-executable"),)
phase = process.ProcessCommand(name, command, root)

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
                    while True:
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
    if scenario == "missing-executable":
        result["cause"] = type(error.__cause__).__name__
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

    def assert_no_scopes(self) -> None:
        assert not list((self.root / "scopes").glob("h2h-process-scope-*")), self.log()


@contextmanager
def _probe(tmp_path: Path, *, scenario: str, depth: int = 2) -> Iterator[_Probe]:
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
    package = Path(process.__file__).resolve().parent
    with (tmp_path / "owner.log").open("wb") as output:
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
        try:
            yield probe
        finally:
            # No cleanup claim is synthesized. Even a failed regression must
            # terminate every recorded real group and verify each PID is gone.
            identities = probe.identities()
            for group in {owner.pid, *(item.pgid for item in identities.values())}:
                try:
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            owner.wait(timeout=5)
            deadline = time.monotonic() + 8
            survivors = [item.pid for item in identities.values() if _alive(item.pid)]
            while survivors and time.monotonic() < deadline:
                time.sleep(0.02)
                survivors = [pid for pid in survivors if _alive(pid)]
            assert not survivors, f"Probe cleanup failed for PIDs {survivors}"


@pytest.mark.parametrize("delay_inner", [False, True])
@pytest.mark.parametrize("depth", [2, 3])
def test_outer_cancellation_waits_for_nested_session_cleanup(
    tmp_path: Path, delay_inner: bool, depth: int
) -> None:
    with _probe(tmp_path, scenario="cancel", depth=depth) as probe:
        probe.wait_for("leaf-ready")
        inner = probe.identities()["owner1"]
        if delay_inner:
            os.kill(inner.pid, signal.SIGSTOP)
        started = time.monotonic()
        probe.owner.send_signal(signal.SIGTERM)
        if delay_inner:
            # Real descheduling, not an altered cleanup grace or mocked result.
            time.sleep(0.4)
            os.kill(inner.pid, signal.SIGCONT)
        assert probe.owner.wait(timeout=10) == 143, probe.log()
        assert time.monotonic() - started < 8
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


def test_failed_nested_exec_releases_obligation_without_hiding_failure(
    tmp_path: Path,
) -> None:
    with _probe(tmp_path, scenario="missing-executable") as probe:
        assert probe.owner.wait(timeout=10) == 125, probe.log()
        assert probe.result() == {"kind": "return", "status": 125}
        assert probe.result("owner1")["kind"] == "ownership-error"
        assert probe.result("owner1")["cause"] == "FileNotFoundError"
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
        os.kill(identities["owner1"].pid, signal.SIGKILL)
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
