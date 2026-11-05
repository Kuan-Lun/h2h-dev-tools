"""Exercise Windows diagnostic admission and Job cleanup without native Win32 APIs."""

from __future__ import annotations

import io
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from h2h_dev_tools import _process_diagnostic as diagnostic
from h2h_dev_tools import process


@dataclass
class WindowsWriter:
    """Stateful OS boundary double; assertions cover ownership, not implementation."""

    mode: str
    events: list[str] = field(default_factory=list)
    assigned: bool = False
    terminated: bool = False
    reaped: bool = False
    closed: bool = False
    status: int | None = None
    gate_bytes: bytes = b""
    waits: list[float] = field(default_factory=list)
    argv: tuple[str, ...] = ()
    job: Mock = field(default_factory=Mock)
    child: Mock = field(default_factory=Mock)
    popen: Mock = field(default_factory=Mock)


def windows_writer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    mode: str = "success",
    controller: process.SignalController | None = None,
) -> WindowsWriter:
    state = WindowsWriter(mode)
    interpreter = tmp_path / "base interpreter.exe"
    interpreter.touch()
    gate = Mock()
    state.child.stdin = gate
    interrupted = False
    first_wait = True

    def assign(child: object) -> None:
        assert child is state.child
        state.events.append("assign")
        if mode == "assignment-failure":
            raise OSError("injected assignment failure")
        state.assigned = True

    def write(value: bytes) -> int:
        assert state.assigned, "Writer was released before Job assignment"
        state.events.append("gate")
        state.gate_bytes += value
        return 0 if mode == "partial-gate" else len(value)

    def terminate() -> None:
        assert state.assigned
        state.events.append("terminate")
        if mode == "termination-failure":
            raise OSError("injected Job termination failure")
        state.terminated = True

    def wait_empty(*, deadline: float) -> bool:
        assert deadline == 101.0, "Cleanup must retain the original absolute deadline"
        state.events.append("query-empty")
        if mode == "query-failure":
            raise OSError("injected Job query failure")
        return mode != "still-active"

    def close() -> None:
        state.events.append("close-job")
        if mode == "close-failure":
            raise OSError("injected Job close failure")
        state.closed = True

    def kill() -> None:
        assert not state.assigned, "Assigned helper cleanup must use its Job"
        state.events.append("kill-leaf")
        state.status = -9

    def wait(*, timeout: float) -> int:
        nonlocal first_wait
        assert 0.0 <= timeout <= 1.0
        state.waits.append(timeout)
        state.events.append("wait")
        if mode == "write-timeout" and not state.terminated:
            raise subprocess.TimeoutExpired("diagnostic", timeout)
        if mode == "wait-unexpected" and first_wait:
            first_wait = False
            raise RuntimeError("unexpected wait failure")
        if mode == "reap-failure" and state.terminated:
            raise subprocess.TimeoutExpired("diagnostic cleanup", timeout)
        state.status = 0 if state.status is None else state.status
        state.reaped = True
        return state.status

    def popen(argv: tuple[str, ...], **kwargs: object) -> Mock:
        nonlocal interrupted
        state.events.append("spawn")
        state.argv = argv
        assert argv[0] == str(interpreter), "Do not launch the venv redirector"
        assert "-I" in argv and "-S" in argv and "-c" in argv
        assert kwargs.get("start_new_session", False) is False
        assert kwargs["stdout"] == 42
        assert kwargs["stderr"] == subprocess.DEVNULL
        assert kwargs["stdin"] == subprocess.PIPE
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        assert "__PYVENV_LAUNCHER__" not in environment
        if mode == "spawn-failure":
            raise OSError("injected creation failure")
        if controller is not None and not interrupted:
            interrupted = True
            controller.receive(signal.SIGINT)
            assert controller.pending_signal == signal.SIGINT
        return state.child

    state.job.assign.side_effect = assign
    state.job.terminate.side_effect = terminate
    state.job.wait_empty.side_effect = wait_empty
    state.job.close.side_effect = close
    state.child.kill.side_effect = kill
    state.child.wait.side_effect = wait
    state.child.poll.side_effect = lambda: state.status
    gate.write.side_effect = write
    gate.close.side_effect = lambda: state.events.append("close-gate")
    state.popen.side_effect = popen
    native = SimpleNamespace(
        name="nt",
        environ={"__PYVENV_LAUNCHER__": "untrusted redirector", "KEEP": "yes"},
        get_blocking=Mock(side_effect=AssertionError("Do not inspect shared flags")),
        set_blocking=Mock(side_effect=AssertionError("Do not mutate shared flags")),
    )
    monkeypatch.setattr(diagnostic, "os", native)
    monkeypatch.setattr(
        diagnostic,
        "sys",
        SimpleNamespace(
            executable="venv redirector.exe", _base_executable=str(interpreter)
        ),
    )
    monkeypatch.setattr(time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(subprocess, "Popen", state.popen)
    monkeypatch.setattr(process.WindowsJob, "create", Mock(return_value=state.job))
    return state


@pytest.mark.parametrize("mode", ["success", "write-timeout", "partial-gate"])
def test_windows_diagnostic_writer_is_job_owned_before_release_and_fully_reaped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str
) -> None:
    state = windows_writer(monkeypatch, tmp_path, mode=mode)
    diagnostic.write_diagnostic(
        42,
        "診斷 ✓\n".encode(),
        deadline=101.0,
        work_deadline=101.0,
        signal_controller=process.SignalController(),
    )
    assert state.events.index("assign") < state.events.index("gate")
    assert state.gate_bytes == b"\x01"
    assert state.assigned and state.terminated and state.reaped and state.closed
    assert state.events.index("query-empty") < state.events.index("close-job")
    assert all(timeout <= 1.0 for timeout in state.waits)


@pytest.mark.parametrize("after_preparation", [100.13, 100.30])
def test_windows_preparation_expiry_closes_empty_job_without_spawning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, after_preparation: float
) -> None:
    state = windows_writer(monkeypatch, tmp_path)
    clock = 100.0

    def delayed_job() -> Mock:
        nonlocal clock
        clock = after_preparation
        return state.job

    monkeypatch.setattr(time, "monotonic", lambda: clock)
    monkeypatch.setattr(process.WindowsJob, "create", delayed_job)
    diagnostic.write_diagnostic(
        42,
        b"expired during preparation\n",
        deadline=101.0,
        work_deadline=100.25,
        signal_controller=process.SignalController(),
    )
    assert clock == after_preparation < 101.0
    state.popen.assert_not_called()
    assert state.events == ["close-job"]
    assert state.closed and not state.assigned
    assert not state.gate_bytes


@pytest.mark.parametrize("mode", ["spawn-failure", "assignment-failure"])
def test_windows_diagnostic_admission_failure_never_releases_writer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str
) -> None:
    state = windows_writer(monkeypatch, tmp_path, mode=mode)
    diagnostic.write_diagnostic(
        42,
        b"message\n",
        deadline=101.0,
        work_deadline=101.0,
        signal_controller=process.SignalController(),
    )
    assert not state.gate_bytes and not state.assigned
    assert state.closed
    if mode == "assignment-failure":
        assert "kill-leaf" in state.events and state.reaped
    else:
        state.child.kill.assert_not_called()
        state.child.wait.assert_not_called()


def test_windows_diagnostic_spawn_signal_cannot_escape_job_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    controller = process.SignalController()
    state = windows_writer(monkeypatch, tmp_path, controller=controller)
    with pytest.raises(KeyboardInterrupt):
        diagnostic.write_diagnostic(
            42,
            b"message\n",
            deadline=101.0,
            work_deadline=101.0,
            signal_controller=controller,
        )
    assert not state.gate_bytes
    assert state.assigned and state.terminated and state.reaped and state.closed
    assert controller.defer_depth == 0


@pytest.mark.parametrize(
    "mode",
    [
        "termination-failure",
        "query-failure",
        "still-active",
        "reap-failure",
        "close-failure",
    ],
)
def test_windows_diagnostic_cannot_discard_unproven_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str
) -> None:
    state = windows_writer(monkeypatch, tmp_path, mode=mode)
    with pytest.raises(process.ProcessOwnershipError):
        diagnostic.write_diagnostic(
            42,
            b"message\n",
            deadline=101.0,
            work_deadline=101.0,
            signal_controller=process.SignalController(),
        )
    assert "close-job" in state.events
    if mode != "close-failure":
        assert state.closed


def test_windows_diagnostic_unexpected_wait_error_still_closes_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = windows_writer(monkeypatch, tmp_path, mode="wait-unexpected")
    with pytest.raises(RuntimeError, match="unexpected wait failure"):
        diagnostic.write_diagnostic(
            42,
            b"message\n",
            deadline=101.0,
            work_deadline=101.0,
            signal_controller=process.SignalController(),
        )
    assert state.terminated and state.reaped and state.closed


@pytest.mark.parametrize("remaining", [-1.0, 0.0, 0.001])
def test_windows_diagnostic_cannot_admit_work_without_cleanup_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, remaining: float
) -> None:
    state = windows_writer(monkeypatch, tmp_path)
    diagnostic.write_diagnostic(
        42,
        b"message\n",
        deadline=100.0 + remaining,
        work_deadline=100.0 + remaining,
        signal_controller=process.SignalController(),
    )
    state.popen.assert_not_called()
    assert not state.events


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16-le"])
@pytest.mark.parametrize("pipe_full", [False, True])
def test_actual_windows_leaf_program_preserves_short_write_unicode_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, encoding: str, pipe_full: bool
) -> None:
    state = windows_writer(monkeypatch, tmp_path)
    payload = "清理已完成 😀\n".encode(encoding)
    diagnostic.write_diagnostic(
        42,
        payload,
        deadline=101.0,
        work_deadline=101.0,
        signal_controller=process.SignalController(),
    )
    code_index = state.argv.index("-c")
    program = state.argv[code_index + 1]
    monkeypatch.setattr(sys, "argv", ["-c", *state.argv[code_index + 2 :]])
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"\x01")))
    chunks: list[bytes] = []

    def write(descriptor: int, value: bytes) -> int:
        assert descriptor == 1
        if pipe_full:
            raise BlockingIOError("full inherited pipe")
        chunks.append(value[:3])
        return len(chunks[-1])

    monkeypatch.setattr(os, "write", write)
    if pipe_full:
        with pytest.raises(BlockingIOError):
            exec(compile(program, "<actual-diagnostic-leaf>", "exec"), {})
        assert not chunks
    else:
        exec(compile(program, "<actual-diagnostic-leaf>", "exec"), {})
        assert b"".join(chunks) == payload
        assert len(chunks) > 1


def test_actual_windows_leaf_program_cannot_write_without_admission_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = windows_writer(monkeypatch, tmp_path)
    diagnostic.write_diagnostic(
        42,
        b"message\n",
        deadline=101.0,
        work_deadline=101.0,
        signal_controller=process.SignalController(),
    )
    index = state.argv.index("-c")
    monkeypatch.setattr(sys, "argv", ["-c", *state.argv[index + 2 :]])
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"")))
    write = Mock()
    monkeypatch.setattr(os, "write", write)
    with pytest.raises(SystemExit) as error:
        exec(compile(state.argv[index + 1], "<actual-diagnostic-leaf>", "exec"), {})
    assert error.value.code == 125
    write.assert_not_called()


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16-le"])
def test_diagnostic_payload_cap_keeps_whole_unicode_and_explicit_truncation(
    encoding: str,
) -> None:
    message = "漢✓😀" * 10000
    payload = diagnostic.encode_diagnostic(message, encoding=encoding, errors="strict")
    assert len(payload) <= 20 * 1024
    decoded = payload.decode(encoding, errors="strict")
    suffix = "\n[diagnostic truncated]\n"
    assert decoded.endswith(suffix)
    prefix = decoded.removesuffix(suffix)
    assert prefix and message.startswith(prefix)
    # The selected prefix is maximal in Unicode characters, not arbitrary bytes.
    assert len((prefix + message[len(prefix)] + suffix).encode(encoding)) > 20 * 1024


def test_windows_command_line_guard_counts_utf16_units_and_closes_unstarted_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = windows_writer(monkeypatch, tmp_path)
    # Below the limit in Python characters, above it in Windows UTF-16 units.
    executable = "Z:\\" + "😀" * 17000
    assert len(executable) < 32767 < len(executable.encode("utf-16-le")) // 2
    monkeypatch.setattr(
        diagnostic,
        "sys",
        SimpleNamespace(executable="venv redirector.exe", _base_executable=executable),
    )
    diagnostic.write_diagnostic(
        42,
        b"message\n",
        deadline=101.0,
        work_deadline=101.0,
        signal_controller=process.SignalController(),
    )
    state.popen.assert_not_called()
    assert state.events == ["close-job"]
    assert state.closed
