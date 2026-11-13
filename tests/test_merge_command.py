"""The Git owner works within a consumer's exported package namespace."""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest
from test_merge_workflow import owned_fixture_process

from h2h_dev_tools import (
    bundle,
    code_review,
    merge_command,
    process,
    review_execution,
    review_plan,
)


def _retained_ownership_error() -> process.ProcessOwnershipError:
    error = process.ProcessOwnershipError("native cleanup unavailable")
    error.__cause__ = OSError("scope close failed")
    error.__context__ = RuntimeError("command termination failed")
    error.__suppress_context__ = False
    error.add_note("retained nested obligation")
    return error


@pytest.mark.parametrize(
    "boundary",
    [
        "execute.git",
        "execute.recovery",
        "execute.review",
        "workflow.diagnostic",
        "workflow.completion",
        "main",
    ],
)
def test_ownership_failure_sidecar_preserves_details_without_new_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    receipt = tmp_path / "cleanup-7"
    error = _retained_ownership_error()
    reporter = Mock(side_effect=error)
    spawn = Mock(side_effect=AssertionError("must not admit a new process"))
    monkeypatch.setattr(process, "report_diagnostic", reporter)
    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(merge_command, "_run_git", Mock(side_effect=error))

    @contextmanager
    def destination(*_args: object, **_kwargs: object) -> Iterator[io.StringIO]:
        yield io.StringIO()

    monkeypatch.setattr(merge_command, "_review", Mock(side_effect=error))
    if boundary.startswith("execute."):
        status = merge_command._execute(
            tmp_path,
            ("fixture",),
            review=boundary == "execute.review",
            recovery=boundary == "execute.recovery",
            receipt=receipt,
        )
    elif boundary.startswith("workflow."):
        monkeypatch.setattr(os, "fdopen", destination)
        status = merge_command._workflow_diagnostic(
            "fixture", receipt, completion=boundary == "workflow.completion"
        )
    else:
        monkeypatch.setattr(merge_command, "_execute", Mock(side_effect=error))
        monkeypatch.setattr(
            sys,
            "argv",
            ["merge-command", "--cleanup-receipt", str(receipt), str(tmp_path), "git"],
        )
        status = merge_command.main()

    assert status == 125
    assert not receipt.exists()
    assert reporter.call_count == int(boundary.startswith("workflow."))
    spawn.assert_not_called()
    document = json.loads((tmp_path / "cleanup-7.failure.json").read_bytes())
    assert document == {
        "schema": 1,
        "kind": "merge-owner-failure-diagnostic",
        "authoritative": False,
        "exit_code": 125,
        "cleanup_state": "unknown",
        "boundary": boundary,
        "exceptions": [
            {
                "type": "h2h_dev_tools.process.ProcessOwnershipError",
                "message": "native cleanup unavailable",
                "notes": ["retained nested obligation"],
                "cause": 1,
                "context": 2,
                "context_suppressed": False,
            },
            {
                "type": "builtins.OSError",
                "message": "scope close failed",
                "notes": [],
                "cause": None,
                "context": None,
                "context_suppressed": False,
            },
            {
                "type": "builtins.RuntimeError",
                "message": "command termination failed",
                "notes": [],
                "cause": None,
                "context": None,
                "context_suppressed": False,
            },
        ],
        "truncated": False,
    }
    assert not error.cleanup_proven
    assert error.__notes__ == ["retained nested obligation"]


@pytest.mark.parametrize("status", [0, 7, 125])
def test_clean_command_does_not_create_failure_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    receipt = tmp_path / "cleanup"
    monkeypatch.setattr(merge_command, "_run_git", Mock(return_value=status))
    result = merge_command._execute(
        tmp_path, ("fixture",), review=False, recovery=False, receipt=receipt
    )
    assert result == (1 if status == 125 else status)
    assert receipt.read_text() == "owned_tree_empty\n"
    assert not (tmp_path / "cleanup.failure.json").exists()


@pytest.mark.parametrize("fault", ["open", "write", "short", "close", "existing"])
def test_failure_sidecar_io_cannot_replace_125_or_create_cleanup_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    receipt = tmp_path / "cleanup"
    destination = tmp_path / "cleanup.failure.json"
    error = _retained_ownership_error()
    monkeypatch.setattr(merge_command, "_run_git", Mock(side_effect=error))
    report = Mock(side_effect=AssertionError("must not emit fallback output"))
    monkeypatch.setattr(process, "report_diagnostic", report)
    original_write, original_close = os.write, os.close
    if fault == "existing":
        destination.write_bytes(b"retain earlier diagnostic")

    def close(descriptor: int) -> None:
        original_close(descriptor)
        raise OSError("close failed")

    with monkeypatch.context() as io_fault:
        if fault == "open":
            io_fault.setattr(os, "open", Mock(side_effect=OSError("open failed")))
        write = Mock(
            side_effect=OSError("write failed")
            if fault == "write"
            else lambda descriptor, payload: original_write(
                descriptor, b"{" if fault == "short" else payload
            )
        )
        io_fault.setattr(os, "write", write)
        if fault == "close":
            io_fault.setattr(os, "close", close)
        status = merge_command._execute(
            tmp_path, ("fixture",), review=False, recovery=False, receipt=receipt
        )
    assert status == 125
    assert not receipt.exists()
    assert write.call_count == (0 if fault in {"open", "existing"} else 1)
    report.assert_not_called()
    assert not error.cleanup_proven
    if fault == "existing":
        assert destination.read_bytes() == b"retain earlier diagnostic"
    elif fault in {"write", "short"}:
        with pytest.raises(json.JSONDecodeError):
            json.loads(destination.read_bytes())


def test_failure_sidecar_bounds_unicode_notes_and_exception_graph() -> None:
    error = _retained_ownership_error()
    current: BaseException = error
    for _ in range(10):
        cause = RuntimeError('漢字\n"' * 4096)
        current.__cause__ = cause
        for _ in range(10):
            current.add_note('漢字\n"' * 4096)
        current = cause
    payload = merge_command._ownership_failure_payload(error, boundary="execute.git")
    assert len(payload) <= merge_command.OWNERSHIP_FAILURE_LIMIT_BYTES
    document = json.loads(payload)
    assert document["truncated"]
    assert len(document["exceptions"]) == 8
    assert any(node["cause"] == "omitted" for node in document["exceptions"])
    assert all(len(node["notes"]) <= 8 for node in document["exceptions"])
    assert "漢字" in document["exceptions"][1]["message"]

    error.__cause__ = error
    error.__suppress_context__ = True
    document = json.loads(
        merge_command._ownership_failure_payload(error, boundary="execute.git")
    )
    assert len(document["exceptions"]) == 1
    assert document["exceptions"][0]["cause"] == 0
    assert document["exceptions"][0]["context"] is None
    assert document["exceptions"][0]["context_suppressed"]


@pytest.mark.skipif(os.name != "posix", reason="Exercises POSIX pipe backpressure")
def test_ownership_failure_sidecar_ignores_full_output_pipe(tmp_path: Path) -> None:
    package = Path(process.__file__).resolve().parent
    actor = f"""import runpy, subprocess
from pathlib import Path
package = Path({str(package)!r})
runpy.run_path(str(package / 'source_loader.py'))['load_package'](package, 'h2h_dev_tools')
from h2h_dev_tools import merge_command, process
error = process.ProcessOwnershipError('synthetic unproven cleanup')
error.__cause__ = OSError('retained scope failure')
error.add_note('retained terminal note')
def fail(*args, **kwargs):
    raise error
def forbidden(*args, **kwargs):
    raise AssertionError('no new reporter or process is allowed')
merge_command._run_git = fail
process.report_diagnostic = forbidden
subprocess.Popen = forbidden
raise SystemExit(merge_command._execute(
    Path({str(tmp_path)!r}), ('fixture',), review=False, recovery=True,
    receipt=Path({str(tmp_path / "cleanup")!r}),
))
"""
    read_descriptor, write_descriptor = os.pipe()
    try:
        os.set_blocking(write_descriptor, False)
        try:
            while True:
                os.write(write_descriptor, b"x" * 8192)
        except BlockingIOError:
            pass
        os.set_blocking(write_descriptor, True)
        with owned_fixture_process(
            process.ProcessCommand(
                "terminal ownership diagnostic with full output pipe",
                (sys.executable, "-I", "-B", "-c", actor),
                tmp_path,
                stdout=write_descriptor,
                stderr=write_descriptor,
            ),
            deadline=time.monotonic() + 10,
        ) as owned:
            # The parent never reads the full pipe; a traceback or new reporter
            # must not be needed for the synthetic failure's natural exit.
            assert owned.process.wait(timeout=5) == 125
            assert os.get_blocking(write_descriptor)
            assert not (tmp_path / "cleanup").exists()
            document = json.loads((tmp_path / "cleanup.failure.json").read_bytes())
            assert document["cleanup_state"] == "unknown"
            assert document["exceptions"][0]["notes"] == ["retained terminal note"]
            assert document["exceptions"][1]["message"] == "retained scope failure"
    finally:
        os.close(read_descriptor)
        os.close(write_descriptor)


@pytest.mark.parametrize("completion", [False, True])
@pytest.mark.parametrize("unknown_cleanup", [False, True])
def test_workflow_output_requires_writer_cleanup_before_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    completion: bool,
    unknown_cleanup: bool,
) -> None:
    receipt = tmp_path / "cleanup"
    messages: list[str] = []
    stream = io.StringIO()

    @contextmanager
    def destination(
        descriptor: int, mode: str, *, encoding: str, closefd: bool
    ) -> Iterator[io.StringIO]:
        assert (descriptor, mode, encoding, closefd) == (3, "w", "utf-8", False)
        yield stream

    def report(message: str) -> None:
        assert sys.stderr is stream
        assert not receipt.exists()
        messages.append(message)
        if unknown_cleanup:
            raise process.ProcessOwnershipError("Writer cleanup is unknown")

    monkeypatch.setattr(os, "fdopen", destination)
    monkeypatch.setattr(process, "report_diagnostic", report)
    status = merge_command._workflow_diagnostic(
        "finished", receipt, completion=completion
    )
    assert messages == ["finished" if completion else "git-flow-merge: finished"]
    if unknown_cleanup:
        assert status == process.TERMINATION_FAILED_EXIT_CODE
        assert not receipt.exists()
    else:
        assert status == 0
        assert receipt.read_text() == "owned_tree_empty\n"


@pytest.mark.skipif(os.name != "posix", reason="Exercises POSIX pipe backpressure")
@pytest.mark.parametrize("stream_name", ["stdout", "stderr"])
@pytest.mark.parametrize("prefilled", [False, True])
@pytest.mark.parametrize("original_blocking", [False, True])
def test_recovery_never_waits_for_an_open_undrained_diagnostic_pipe(
    tmp_path: Path, stream_name: str, prefilled: bool, original_blocking: bool
) -> None:
    receipt = tmp_path / "cleanup"
    marker = tmp_path / "completed"
    read_descriptor, write_descriptor = os.pipe()
    try:
        if prefilled:
            os.set_blocking(write_descriptor, False)
            try:
                while True:
                    os.write(write_descriptor, b"x" * 8192)
            except BlockingIOError:
                pass
        os.set_blocking(write_descriptor, original_blocking)
        child = (
            "import sys\nfrom pathlib import Path\n"
            "sys.stderr.write('x' * (192 * 1024)); sys.stderr.flush()\n"
            f"Path({str(marker)!r}).touch()\nraise SystemExit(7)\n"
        )
        with owned_fixture_process(
            process.ProcessCommand(
                "undrained recovery diagnostic fixture",
                (
                    sys.executable,
                    "-I",
                    "-B",
                    merge_command.__file__,
                    "--cleanup-receipt",
                    str(receipt),
                    "--recovery",
                    str(tmp_path),
                    "--",
                    sys.executable,
                    "-c",
                    child,
                ),
                tmp_path,
                stdout=write_descriptor
                if stream_name == "stdout"
                else subprocess.DEVNULL,
                stderr=write_descriptor
                if stream_name == "stderr"
                else subprocess.DEVNULL,
            ),
            deadline=time.monotonic() + 10,  # Original five-second wait plus rescue.
        ) as owned:
            owner = owned.process
            # The reader deliberately stays open without consuming any bytes.
            # A full diagnostic pipe must not participate in the recovery wait.
            assert owner.wait(timeout=5) == 7
            assert marker.exists()
            assert receipt.read_text() == "owned_tree_empty\n"
            # dup/inheritance shares the open-file description with our writer.
            assert os.get_blocking(write_descriptor) == original_blocking
    finally:
        os.close(read_descriptor)
        os.close(write_descriptor)


@pytest.mark.parametrize("poisoned_source_package", ["merge_command.py"], indirect=True)
@pytest.mark.parametrize("failed_operation", ["seek", "read", "close"])
def test_recovery_diagnostic_io_error_preserves_result_and_cleanup_receipt(
    tmp_path: Path, failed_operation: str, poisoned_source_package: Path
) -> None:
    receipt = tmp_path / "cleanup"
    marker = tmp_path / "completed"
    script = (
        "import runpy\nfrom pathlib import Path\n"
        f"package = Path({str(poisoned_source_package)!r})\n"
        "runpy.run_path(str(package / 'source_loader.py'))['load_package'](package, 'h2h_dev_tools')\n"
        "from h2h_dev_tools import merge_command\n"
        "original = merge_command.tempfile.TemporaryFile\n"
        "class BrokenReplay:\n"
        "    def __init__(self): self.stream = original()\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *args): self.close()\n"
        "    def fileno(self): return self.stream.fileno()\n"
        "    def seek(self, offset):\n"
        f"        if {failed_operation == 'seek'!r}: raise OSError('injected seek failure')\n"
        "        return self.stream.seek(offset)\n"
        "    def read(self, size):\n"
        f"        if {failed_operation == 'read'!r}: raise OSError('injected read failure')\n"
        "        return self.stream.read(size)\n"
        "    def close(self):\n"
        "        self.stream.close()\n"
        f"        if {failed_operation == 'close'!r}: raise OSError('injected close failure')\n"
        "merge_command.tempfile.TemporaryFile = BrokenReplay\n"
        "raise SystemExit(merge_command.main())\n"
    )
    child = (
        f"from pathlib import Path; Path({str(marker)!r}).touch(); "
        "print('recovery output'); raise SystemExit(7)"
    )
    result = subprocess.run(
        (
            sys.executable,
            "-I",
            "-B",
            "-c",
            script,
            "--cleanup-receipt",
            str(receipt),
            "--recovery",
            str(tmp_path),
            "--",
            sys.executable,
            "-c",
            child,
        ),
        capture_output=True,
        check=False,
        timeout=15,
    )
    assert marker.exists()
    assert result.returncode == 7, result.stderr
    assert receipt.read_text() == "owned_tree_empty\n"


def test_recovery_finishes_before_replaying_bounded_unterminated_output(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "completed"
    receipt = tmp_path / "cleanup"
    child = (
        "import sys\nfrom pathlib import Path\n"
        "sys.stdout.write('o' * (96 * 1024))\nsys.stdout.flush()\n"
        "sys.stderr.write('e' * (96 * 1024))\nsys.stderr.flush()\n"
        f"Path({str(marker)!r}).touch()\n"
    )
    diagnostics = tmp_path / "diagnostics"
    with diagnostics.open("wb") as diagnostic_output:
        result = subprocess.run(
            (
                sys.executable,
                "-I",
                "-B",
                merge_command.__file__,
                "--cleanup-receipt",
                str(receipt),
                "--recovery",
                str(tmp_path),
                "--",
                sys.executable,
                "-c",
                child,
            ),
            stdout=subprocess.DEVNULL,
            stderr=diagnostic_output,
            check=False,
            timeout=15,
        )
    diagnostic_bytes = diagnostics.read_bytes()
    assert result.returncode == 0, diagnostic_bytes
    assert marker.exists()
    assert receipt.read_text() == "owned_tree_empty\n"
    assert len(diagnostic_bytes) < 70_000
    truncation = b"merge-command: recovery diagnostics truncated after 64 KiB"
    assert truncation in diagnostic_bytes
    # A writable sink receives the complete bounded prefix, even if the low-level
    # diagnostic transport must split it into smaller messages for the platform.
    start = diagnostic_bytes.index(b"o" * 1024)
    prefix = diagnostic_bytes[start : diagnostic_bytes.index(truncation)]
    assert prefix == b"o" * (64 * 1024) + b"\n"


def test_exported_git_owner_passes_strict_typing_without_installed_toolkit(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.h2h-dev-tools]\ncomponents = ["git-flow"]\n'
        '[tool.mypy]\npython_version = "3.14"\nstrict = true\n'
        'files = ["scripts/_shared"]\n'
    )
    bundle.sync_bundle(tmp_path)
    # Consumers expose _shared as their own package, while the launcher must also
    # remain directly executable after the shell copies its source closure.
    (tmp_path / "scripts/_shared/__init__.py").write_text("")
    result = subprocess.run(
        (
            sys.executable,
            "-I",
            "-B",
            "-m",
            "mypy",
            "--no-site-packages",
            f"--cache-dir={os.devnull}",
            "--config-file=pyproject.toml",
        ),
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "interrupt", [KeyboardInterrupt(), process.RunnerSignalInterrupt(15)]
)
def test_git_owner_interruption_cannot_claim_review_publication_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupt: BaseException
) -> None:
    receipt = tmp_path / "cleanup"

    def interrupt_git(
        phase: process.ProcessCommand, *, recovery: bool, deadline: float | None
    ) -> int:
        del phase
        assert not recovery
        assert deadline is None
        raise interrupt

    monkeypatch.setattr(merge_command, "_run_git", interrupt_git)
    with pytest.raises(type(interrupt)):
        merge_command._execute(
            tmp_path, ("git", "status"), review=False, recovery=False, receipt=receipt
        )
    assert not receipt.exists()


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16-le"])
@pytest.mark.parametrize("shape", ["split-unicode", "trailing-newlines", "truncated"])
def test_recovery_replay_preserves_exact_decoded_text_and_sink_encoding(
    monkeypatch: pytest.MonkeyPatch, encoding: str, shape: str
) -> None:
    if shape == "split-unicode":
        data = b"a" * 2047 + "漢字✓\n\n尾\n\n".encode()
    elif shape == "trailing-newlines":
        data = b"x" * 2046 + b"\n\ntail\n\n\n"
    else:
        data = ("漢字\n\n" * 10000).encode()
    limit = 64 * 1024
    expected = data[:limit].decode("utf-8", errors="replace")
    if len(data) > limit:
        expected += "\nmerge-command: recovery diagnostics truncated after 64 KiB\n"
    binary = io.BytesIO()
    sink = io.TextIOWrapper(binary, encoding=encoding, newline="\n")
    monkeypatch.setattr(sys, "stderr", sink)
    monkeypatch.setattr(time, "monotonic", lambda: 100.0)
    merge_command._replay_recovery_output(io.BytesIO(data), deadline=101.0)
    sink.flush()
    assert binary.getvalue() == expected.encode(encoding)
    sink.close()


@pytest.mark.parametrize(
    "failure",
    [
        KeyboardInterrupt(),
        process.RunnerSignalInterrupt(signal.SIGTERM),
        code_review.ReviewError("evidence rejected"),
        review_execution.ExecutionError("deadline exhausted"),
        review_plan.PlanError("coverage rejected"),
    ],
)
@pytest.mark.parametrize("diagnostic", ["complete", "unknown", "unlink-fails"])
def test_post_proof_diagnostic_revokes_receipt_before_new_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
    diagnostic: str,
) -> None:
    receipt = tmp_path / "cleanup"
    deadline = time.monotonic() + 10
    called: list[str] = []
    unknown = process.ProcessOwnershipError("diagnostic cleanup unknown")
    unlink_failure = OSError("cleanup proof cannot be revoked")

    @contextmanager
    def after_proof(
        repository: Path, command: tuple[str, ...], environment: dict[str, str]
    ) -> Iterator[int]:
        del repository, command
        environment["H2H_REVIEW_DEADLINE"] = str(deadline)
        yield 0
        assert receipt.read_text() == "owned_tree_empty\n"
        raise failure

    def report(message: str, *, deadline: float | None) -> None:
        assert not receipt.exists(), "old proof survived new diagnostic admission"
        assert deadline == expected_deadline
        called.append(message)
        if diagnostic == "unknown":
            raise unknown

    original_unlink = Path.unlink

    def unlink(path: Path, *, missing_ok: bool = False) -> None:
        if path == receipt:
            raise unlink_failure
        original_unlink(path, missing_ok=missing_ok)

    expected_deadline = deadline
    monkeypatch.setattr(merge_command, "_review", after_proof)
    monkeypatch.setattr(process, "report_diagnostic", report)
    if diagnostic == "unlink-fails":
        monkeypatch.setattr(Path, "unlink", unlink)
    if diagnostic != "complete":
        expected = unknown if diagnostic == "unknown" else unlink_failure
        with pytest.raises(type(expected)) as raised:
            merge_command._execute(
                tmp_path, ("review",), review=True, recovery=False, receipt=receipt
            )
        assert raised.value is expected
        if diagnostic == "unlink-fails":
            assert not called
            assert receipt.read_text() == "owned_tree_empty\n"
        else:
            assert len(called) == 1
            assert not receipt.exists()
    else:
        status = merge_command._execute(
            tmp_path, ("review",), review=True, recovery=False, receipt=receipt
        )
        assert status == (
            130
            if isinstance(failure, KeyboardInterrupt)
            else 143
            if isinstance(failure, process.RunnerSignalInterrupt)
            else 1
        )
        assert len(called) == 1
        assert receipt.read_text() == "owned_tree_empty\n"


_POST_PROOF_DIAGNOSTIC_ACTOR = """
import json, os, runpy, signal, subprocess, sys, time
from contextlib import contextmanager
from pathlib import Path
package, state = map(Path, sys.argv[1:3])
deadline = float(sys.argv[3])
scenario = sys.argv[4]
runpy.run_path(str(package / 'source_loader.py'))['load_package'](
    package, 'h2h_dev_tools')
from h2h_dev_tools import code_review, merge_command, process
from h2h_dev_tools import _process_diagnostic, _process_scope
# Model the normal standalone shell-to-launcher boundary. The test guardian
# still owns the actual process group; this does not model dead nested owners.
os.environ.pop(_process_scope.ENVIRONMENT_KEY, None)
receipt = state / 'cleanup'
real_popen = subprocess.Popen
def observe_popen(command, **kwargs):
    child = real_popen(command, **kwargs)
    assert command[5] == _process_diagnostic._WRITER
    marker = state / 'writer.pending'
    marker.write_text(json.dumps(dict(
        pid=child.pid, group=os.getpgid(child.pid), launcher=os.getpid(),
        launcher_group=os.getpgrp(), receipt_exists=receipt.exists())))
    marker.replace(state / 'writer.json')
    # Hold at the true admitted writer boundary, before its owner can reap it.
    # The pipe is full and left unread. The original absolute fixture deadline
    # bounds even this injected pause; production diagnostic budgets are intact.
    while time.monotonic() < deadline:
        time.sleep(0.001)
    raise RuntimeError('test did not kill the launcher at its observed boundary')
subprocess.Popen = observe_popen
@contextmanager
def review(repository, command, environment):
    environment['H2H_REVIEW_DEADLINE'] = str(deadline)
    with code_review._publication(state / 'review-receipt') as publication:
        publication.staging.write_text('provisional evidence')
        yield 0
        assert receipt.read_text() == 'owned_tree_empty\\n'
        if scenario == 'signal':
            os.kill(os.getpid(), signal.SIGTERM)
        else:
            raise code_review.ReviewError('evidence failed after cleanup proof')
merge_command._review = review
raise SystemExit(merge_command._execute(
    state, ('review',), review=True, recovery=False, receipt=receipt))
"""


@pytest.mark.skipif(os.name != "posix", reason="Real same-group POSIX writer death")
@pytest.mark.parametrize("scenario", ["signal", "evidence"])
def test_killed_post_proof_launcher_leaves_no_proof_for_live_writer(
    tmp_path: Path, scenario: str
) -> None:
    read_descriptor, write_descriptor = os.pipe()
    deadline = time.monotonic() + 15
    owner: process.OwnedProcess | None = None
    failure: BaseException | None = None
    try:
        os.set_blocking(write_descriptor, False)
        try:
            while True:
                os.write(write_descriptor, b"x" * 8192)
        except BlockingIOError:
            pass
        os.set_blocking(write_descriptor, True)
        with process.controlled_termination_signals() as controller:
            try:
                with controller.defer():
                    try:
                        owner = process.start_owned_process(
                            process.ProcessCommand(
                                "post-proof diagnostic launcher",
                                (
                                    sys.executable,
                                    "-I",
                                    "-B",
                                    "-c",
                                    _POST_PROOF_DIAGNOSTIC_ACTOR,
                                    str(Path(process.__file__).resolve().parent),
                                    str(tmp_path),
                                    str(deadline),
                                    scenario,
                                ),
                                tmp_path,
                                stdout=subprocess.DEVNULL,
                                stderr=write_descriptor,
                            ),
                            deadline=deadline,
                            signal_controller=controller,
                        )
                    except process.ProcessOwnershipError as launch_error:
                        owner = launch_error.owner
                        raise
                marker = tmp_path / "writer.json"
                while not marker.exists():
                    assert owner.process.poll() is None
                    assert time.monotonic() < deadline - 6
                    time.sleep(0.001)
                writer = json.loads(marker.read_text())
                assert writer["group"] == writer["launcher_group"] == owner.process.pid
                os.kill(writer["pid"], 0)
                owner.process.kill()
                assert (
                    owner.process.wait(timeout=max(0, deadline - time.monotonic()))
                    == -9
                )
                os.kill(writer["pid"], 0)
                tree_empty = process.wait_for_owned_tree_exit(
                    owner,
                    deadline=deadline,
                    poll_deadline=time.monotonic() + 0.1,
                )
                receipt_exists = (tmp_path / "cleanup").exists()
                (tmp_path / "before-rescue.json").write_text(
                    json.dumps(
                        dict(
                            writer,
                            tree_empty=tree_empty,
                            receipt_after_kill=receipt_exists,
                        )
                    )
                )
                assert not tree_empty, (
                    "the blocked writer must outlive its killed launcher"
                )
                assert not receipt_exists, (
                    "stale proof would let the shell recover early"
                )
            except BaseException as error:
                failure = error
                raise
            finally:
                with controller.defer():
                    if owner is not None:
                        try:
                            empty = process.terminate_owned_tree(
                                owner, deadline=deadline
                            )
                            closed = process.close_owner(owner, deadline=deadline)
                            assert empty and closed, "Post-proof writer rescue unproven"
                        except BaseException as error:
                            if failure is None:
                                raise
                            failure.add_note(
                                f"Post-proof writer rescue failed: {error}"
                            )
    finally:
        os.close(read_descriptor)
        os.close(write_descriptor)


_OUTER_IO_ACTOR = """
import os, runpy, sys, tempfile, time
from contextlib import contextmanager
from pathlib import Path
package, state = map(Path, sys.argv[1:3])
mode, fault = sys.argv[3:5]
runpy.run_path(str(package / 'source_loader.py'))['load_package'](
    package, 'h2h_dev_tools')
from h2h_dev_tools import code_review, merge_command, process
receipt = state / 'cleanup'
original_write = Path.write_text
original_replace = Path.replace
original_unlink = Path.unlink
original_rmtree = tempfile.TemporaryDirectory._rmtree
def fail():
    original_write(state / 'fault', fault)
    raise OSError('injected outer I/O failure: ' + fault)
def write(path, *args, **kwargs):
    if fault == 'write' and path == receipt.with_suffix('.pending'): fail()
    return original_write(path, *args, **kwargs)
def replace(path, target):
    if fault == 'replace' and Path(target) == receipt: fail()
    return original_replace(path, target)
def unlink(path, *, missing_ok=False):
    if fault == 'revoke' and path == receipt: fail()
    return original_unlink(path, missing_ok=missing_ok)
def rmtree(cls, name, *args, **kwargs):
    if fault == 'pending' and Path(name).parent == state: fail()
    return original_rmtree(name, *args, **kwargs)
Path.write_text = write
Path.replace = replace
Path.unlink = unlink
tempfile.TemporaryDirectory._rmtree = classmethod(rmtree)
@contextmanager
def review(repository, command, environment):
    deadline = time.monotonic() + 10
    environment['H2H_REVIEW_DEADLINE'] = str(deadline)
    status = merge_command._run_owned(process.ProcessCommand(
        'review fixture child', command, repository, environment=environment),
        hard_deadline=deadline)
    with code_review._publication(state / 'passing-review') as publication:
        with tempfile.TemporaryDirectory(prefix='pending-', dir=state):
            publication.staging.write_text('provisional candidate')
            yield status
            if fault == 'revoke':
                process.report_diagnostic = report
                raise code_review.ReviewError('reject provisional evidence')
        publication.complete(deadline)
merge_command._review = review
if fault == 'revoke':
    def report(*args, **kwargs):
        (state / 'reporter-admitted').touch()
        raise AssertionError('reporter admitted before proof revocation')
sys.argv = [sys.argv[0], '--cleanup-receipt', str(receipt)]
if mode != 'ordinary': sys.argv.append('--' + mode)
sys.argv += [str(state), '--', sys.executable, '-I', '-B', '-c',
    'from pathlib import Path; Path(' + repr(str(state / 'child-completed')) + ').touch()']
raise SystemExit(merge_command.main())
"""


@pytest.mark.skipif(os.name != "posix", reason="Exercises POSIX pipe backpressure")
@pytest.mark.parametrize(
    ("mode", "fault"),
    [
        (mode, fault)
        for mode in ("ordinary", "review", "recovery")
        for fault in ("write", "replace")
    ]
    + [("review", "pending"), ("review", "revoke")],
)
def test_outer_io_failure_exits_without_writing_to_full_stderr(
    tmp_path: Path, mode: str, fault: str
) -> None:
    read_descriptor, write_descriptor = os.pipe()
    deadline = time.monotonic() + 15
    owner: process.OwnedProcess | None = None
    failure: BaseException | None = None
    try:
        os.set_blocking(write_descriptor, False)
        try:
            while True:
                os.write(write_descriptor, b"x" * 8192)
        except BlockingIOError:
            pass
        os.set_blocking(write_descriptor, True)
        with process.controlled_termination_signals() as controller:
            try:
                with controller.defer():
                    try:
                        owner = process.start_owned_process(
                            process.ProcessCommand(
                                "outer I/O failure launcher",
                                (
                                    sys.executable,
                                    "-I",
                                    "-B",
                                    "-c",
                                    _OUTER_IO_ACTOR,
                                    str(Path(process.__file__).resolve().parent),
                                    str(tmp_path),
                                    mode,
                                    fault,
                                ),
                                tmp_path,
                                stdout=subprocess.DEVNULL,
                                stderr=write_descriptor,
                            ),
                            deadline=deadline,
                            signal_controller=controller,
                        )
                    except process.ProcessOwnershipError as launch_error:
                        owner = launch_error.owner
                        raise
                try:
                    status = owner.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    (tmp_path / "before-rescue.json").write_text(
                        json.dumps({"natural_exit": None, "owned_tree_empty": False})
                    )
                    raise
                empty = process.wait_for_owned_tree_exit(owner, deadline=deadline)
                (tmp_path / "before-rescue.json").write_text(
                    json.dumps({"natural_exit": status, "owned_tree_empty": empty})
                )
                # These assertions precede rescue: killing a blocked traceback
                # must never satisfy the normal-exit or nested-cleanup oracle.
                assert status == 1
                assert empty
                assert (tmp_path / "child-completed").exists()
                assert (tmp_path / "fault").read_text() == fault
                assert not (tmp_path / "passing-review").exists()
                assert not (tmp_path / "reporter-admitted").exists()
                receipt = tmp_path / "cleanup"
                if fault in {"pending", "revoke"}:
                    # No new owner was admitted after this valid process proof.
                    assert receipt.read_text() == "owned_tree_empty\n"
                else:
                    assert not receipt.exists()
                assert os.get_blocking(write_descriptor)
            except BaseException as error:
                failure = error
                raise
            finally:
                with controller.defer():
                    if owner is not None:
                        try:
                            empty = process.terminate_owned_tree(
                                owner, deadline=deadline
                            )
                            closed = process.close_owner(owner, deadline=deadline)
                            assert empty and closed, "Outer I/O fixture rescue unproven"
                        except BaseException as error:
                            if failure is None:
                                raise
                            failure.add_note(
                                f"Outer I/O fixture rescue failed: {error}"
                            )
    finally:
        os.close(read_descriptor)
        os.close(write_descriptor)
