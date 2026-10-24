from __future__ import annotations

import errno
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO

import pytest

from h2h_dev_tools import process, review_execution
from h2h_dev_tools.executable_fixture import write_python_executable

_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name != "posix", reason="POSIX owner cleanup FD capacity")
@pytest.mark.parametrize("unavailable", [errno.EMFILE, errno.ENFILE])
@pytest.mark.parametrize("failed_descriptor", [1, 2, 3])
def test_owned_git_capacity_probe_rejects_before_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unavailable: int,
    failed_descriptor: int,
) -> None:
    opened: list[IO[bytes]] = []
    captures: list[IO[bytes]] = []
    attempted = 0
    admitted = False
    open_path = Path.open
    temporary_file = tempfile.TemporaryFile

    def capture() -> IO[bytes]:
        stream = temporary_file()
        captures.append(stream)
        return stream

    def probe(path: Path, mode: str) -> IO[bytes]:
        nonlocal attempted
        assert path == Path(os.devnull) and mode == "rb"
        assert len(captures) == 2 and all(not stream.closed for stream in captures)
        attempted += 1
        if attempted == failed_descriptor:
            raise OSError(unavailable, "synthetic descriptor pressure")
        stream = open_path(path, "rb")
        opened.append(stream)
        return stream

    def launch(
        phase: process.ProcessCommand,
        *,
        hard_deadline: float | None,
        termination_reserve_seconds: float,
    ) -> int:
        nonlocal admitted
        admitted = True
        raise AssertionError("A failed capacity probe must not admit a Git owner")

    monkeypatch.setattr(tempfile, "TemporaryFile", capture)
    monkeypatch.setattr(Path, "open", probe)
    monkeypatch.setattr(process, "run_command", launch)
    with pytest.raises(
        review_execution.GitCommandError, match="file descriptors for owned cleanup"
    ) as caught:
        review_execution.git_output(tmp_path, "status")
    assert isinstance(caught.value.__cause__, OSError)
    assert caught.value.__cause__.errno == unavailable
    assert attempted == failed_descriptor and len(opened) == failed_descriptor - 1
    assert not admitted
    assert all(stream.closed for stream in [*opened, *captures])


@pytest.mark.skipif(os.name != "posix", reason="Real POSIX fsmonitor descendant")
@pytest.mark.parametrize(
    "query",
    [
        "reviewer-fsmonitor",
        "planner-metadata",
        "planner-batch",
        "classifier",
        "timeout",
        "cancel",
    ],
)
def test_review_git_query_rejects_fsmonitor_descendant_before_return(
    tmp_path: Path,
    query: str,
) -> None:
    root = tmp_path / "repository"
    root.mkdir()

    def git(*arguments: str) -> None:
        subprocess.run(("git", *arguments), cwd=root, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.name", "Review ownership fixture")
    git("config", "user.email", "review@example.invalid")
    (root / "tracked.txt").write_text("tracked\n")
    git("add", ".")
    git("commit", "-qm", "test: initial tree")
    state = tmp_path / "state"
    state.mkdir()
    pid_file = state / "descendant.pid"
    child_source = (
        "import os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"path = Path({str(pid_file)!r})\n"
        "if not path.exists():\n"
        "    subprocess.Popen([sys.executable, '-I', '-S', '-B', '-c', "
        f"{('import os, time; from pathlib import Path; Path(' + repr(str(pid_file)) + ').write_text(str(os.getpid())); time.sleep(60)')!r}], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "    deadline = time.monotonic() + 5\n"
        "    while not path.exists() and time.monotonic() < deadline:\n"
        "        time.sleep(0.01)\n"
    )
    environment = os.environ.copy()
    blob = subprocess.check_output(
        ("git", "rev-parse", "HEAD:tracked.txt"), cwd=root, text=True
    ).strip()
    if query == "reviewer-fsmonitor":
        hook = write_python_executable(
            tmp_path / "fsmonitor",
            child_source + "sys.stdout.buffer.write(b'fixture-token\\0/\\0')\n",
        )
        git("config", "core.fsmonitor", str(hook))
    else:
        real_git = shutil.which("git")
        assert real_git is not None
        binary = tmp_path / "bin"
        binary.mkdir()
        write_python_executable(
            binary / "git",
            child_source
            + ("time.sleep(60)\n" if query in {"timeout", "cancel"} else "")
            + f"os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])\n",
        )
        environment["PATH"] = str(binary) + os.pathsep + os.environ.get("PATH", "")
    calls = {
        "reviewer-fsmonitor": "code_review.Reviewer(root)._git_bytes('diff', '--name-only', '-z')",
        "planner-metadata": "review_plan._git(root, 'rev-parse', 'HEAD', deadline=deadline)",
        "planner-batch": f"review_plan._blobs(root, [dict(blob={blob!r}, size=8)], deadline)",
        "classifier": "change_scope.classify_changes('HEAD', 'HEAD', root, deadline=deadline, git_reader=review_execution.git_output)",
        "timeout": "review_execution.git_output(root, 'status', deadline=deadline)",
        "cancel": "review_execution.git_output(root, 'status', deadline=deadline)",
    }
    driver = tmp_path / "driver.py"
    report = state / "report.json"
    driver.write_text(
        "import json, os, runpy, time\n"
        "from pathlib import Path\n"
        f"package = Path({str(_ROOT / 'src/h2h_dev_tools')!r})\n"
        "runpy.run_path(str(package / 'source_loader.py'))['load_package'](package, 'h2h_dev_tools')\n"
        "from h2h_dev_tools import change_scope, code_review, review_execution, review_plan\n"
        f"root = Path({str(root)!r})\n"
        f"deadline = time.monotonic() + {2 if query == 'timeout' else 15}\n"
        "error = None\n"
        "try:\n"
        f"    {calls[query]}\n"
        "except BaseException as caught:\n"
        "    error = type(caught).__name__ + ': ' + str(caught)\n"
        f"pid = int(Path({str(pid_file)!r}).read_text())\n"
        "try:\n"
        "    os.kill(pid, 0)\n"
        "    alive = True\n"
        "except ProcessLookupError:\n"
        "    alive = False\n"
        f"Path({str(report)!r}).write_text(json.dumps(dict(error=error, alive=alive, pid=pid)))\n"
    )
    scopes = tmp_path / "scopes"
    scopes.mkdir()
    deadline = time.monotonic() + 20
    with (
        process.controlled_termination_signals() as controller,
        (state / "output").open("wb") as output,
    ):
        owner: process.OwnedProcess | None = None
        closed = False
        try:
            with controller.defer():
                owner = process.start_owned_process(
                    process.ProcessCommand(
                        "Review Git query fixture",
                        (sys.executable, "-I", "-S", "-B", str(driver)),
                        root,
                        environment | {"TMPDIR": str(scopes)},
                        output,
                        output,
                    ),
                    deadline=deadline,
                    signal_controller=controller,
                )
            if query == "cancel":
                ready_deadline = time.monotonic() + 5
                while not pid_file.exists():
                    assert owner.process.poll() is None
                    assert time.monotonic() < ready_deadline
                    time.sleep(0.01)
                owner.process.send_signal(signal.SIGTERM)
            assert owner.process.wait(timeout=12) == 0, (state / "output").read_text()
            observed = json.loads(report.read_text())
            assert observed["error"] is not None, observed
            assert observed["alive"] is False, observed
            if query == "timeout":
                assert "deadline" in observed["error"]
            if query == "cancel":
                assert "RunnerSignalInterrupt" in observed["error"]
            assert process.wait_for_owned_tree_exit(owner, deadline=deadline)
            assert process.close_owner(owner, deadline=deadline)
            closed = True
            assert list(scopes.glob("h2h-process-scope-*")) == []
        finally:
            # Rescue happens only after the query's own post-cleanup assertions.
            if owner is not None and not closed:
                with controller.defer():
                    empty = process.terminate_owned_tree(owner, deadline=deadline)
                    released = process.close_owner(owner, deadline=deadline)
                    assert empty and released


@pytest.mark.parametrize("input_bytes", [None, b"binary\0input\xff\n"])
@pytest.mark.parametrize("seconds", [1, 10])
def test_owned_git_capture_preserves_bytes_without_owner_diagnostics(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    input_bytes: bytes | None,
    seconds: int,
) -> None:
    arguments: tuple[str, ...]
    if input_bytes is None:
        arguments = ("--version",)
        expected = subprocess.check_output(("git", *arguments))
    else:
        arguments = ("hash-object", "--stdin")
        expected = subprocess.check_output(("git", *arguments), input=input_bytes)
    assert (
        review_execution.git_output(
            tmp_path,
            *arguments,
            input_bytes=input_bytes,
            deadline=time.monotonic() + seconds,
            max_output_bytes=len(expected),
        )
        == expected
    )
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("proven", [False, True])
def test_owned_git_capture_never_accepts_an_ownership_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    proven: bool,
) -> None:
    failure = process.ProcessOwnershipError("injected query cleanup failure")
    failure.cleanup_proven = proven
    probes: list[IO[bytes]] = []
    open_path = Path.open

    def probe(path: Path, mode: str) -> IO[bytes]:
        assert path == Path(os.devnull) and mode == "rb"
        stream = open_path(path, "rb")
        probes.append(stream)
        return stream

    def fail(
        phase: process.ProcessCommand,
        *,
        hard_deadline: float | None,
        termination_reserve_seconds: float,
    ) -> int:
        # A successful probe does not grant any later cleanup proof. A race or
        # other ownership failure after admission must retain its exact status.
        assert len(probes) == (3 if os.name == "posix" else 0)
        assert all(stream.closed for stream in probes)
        raise failure

    monkeypatch.setattr(Path, "open", probe)
    monkeypatch.setattr(process, "run_command", fail)
    if proven:
        with pytest.raises(
            review_execution.ExecutionError, match="cleanup completed"
        ) as caught:
            review_execution.git_output(tmp_path, "status")
        assert caught.value.__cause__ is failure
    else:
        with pytest.raises(process.ProcessOwnershipError) as unproven:
            review_execution.git_output(tmp_path, "status")
        assert unproven.value is failure
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("active_is_earlier", [False, True])
def test_owned_git_query_keeps_the_earlier_absolute_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, active_is_earlier: bool
) -> None:
    early = time.monotonic() + 10
    late = early + 10
    observed = []

    def complete(
        phase: process.ProcessCommand,
        *,
        hard_deadline: float | None,
        termination_reserve_seconds: float,
    ) -> int:
        observed.append(hard_deadline)
        assert phase.stdout is not None and not isinstance(phase.stdout, int)
        phase.stdout.write(b"exact\0bytes")
        return 0

    monkeypatch.setattr(process, "run_command", complete)
    with review_execution.within_deadline(early if active_is_earlier else late):
        assert (
            review_execution.git_output(
                tmp_path, "status", deadline=late if active_is_earlier else early
            )
            == b"exact\0bytes"
        )
    assert observed == [early]


def test_capture_close_error_cannot_hide_unknown_process_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    failure = process.ProcessOwnershipError("unproven query tree")

    class FailedClose(io.BytesIO):
        def close(self) -> None:
            super().close()
            raise OSError("capture close failed")

    def fail(
        phase: process.ProcessCommand,
        *,
        hard_deadline: float | None,
        termination_reserve_seconds: float,
    ) -> int:
        raise failure

    monkeypatch.setattr(tempfile, "TemporaryFile", FailedClose)
    monkeypatch.setattr(process, "run_command", fail)
    with pytest.raises(process.ProcessOwnershipError) as caught:
        review_execution.git_output(tmp_path, "status")
    assert caught.value is failure
    assert failure.cleanup_proven is False
    assert any("capture close failed" in note for note in failure.__notes__)
    assert capsys.readouterr() == ("", "")


def test_capture_closure_cannot_extend_the_query_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlowClose(io.BytesIO):
        def close(self) -> None:
            if not self.closed:
                time.sleep(0.02)
            super().close()

    def complete(
        phase: process.ProcessCommand,
        *,
        hard_deadline: float | None,
        termination_reserve_seconds: float,
    ) -> int:
        return 0

    monkeypatch.setattr(tempfile, "TemporaryFile", SlowClose)
    monkeypatch.setattr(process, "run_command", complete)
    with pytest.raises(review_execution.ExecutionError, match="closure exceeded"):
        review_execution.git_output(
            tmp_path, "status", deadline=time.monotonic() + 0.03
        )


@pytest.mark.skipif(os.name != "posix", reason="Signal at the fixture launch boundary")
@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_git_query_fixture_closes_owner_when_launch_handoff_is_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    signum: int,
) -> None:
    original = process.start_owned_process
    close = process.close_owner
    owners: list[tuple[process.OwnedProcess, float]] = []
    closed: list[tuple[process.OwnedProcess, float, bool]] = []

    def interrupt(
        phase: process.ProcessCommand,
        *,
        deadline: float,
        signal_controller: process.SignalController | None,
    ) -> process.OwnedProcess:
        owner = original(phase, deadline=deadline, signal_controller=signal_controller)
        owners.append((owner, deadline))
        os.kill(os.getpid(), signum)
        return owner

    def record_close(
        owner: process.OwnedProcess,
        *,
        deadline: float,
        diagnostic: Callable[[str], None] | None = None,
    ) -> bool:
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
            test_review_git_query_rejects_fsmonitor_descendant_before_return(
                tmp_path, "reviewer-fsmonitor"
            )
        assert len(owners) == 1
        owner, deadline = owners[0]
        assert closed == [(owner, deadline, True)]
        assert owner.process.poll() is not None
        assert owner.posix_scope is not None
        assert not owner.posix_scope.capability.directory.exists()
        with pytest.raises(ProcessLookupError):
            os.killpg(owner.process.pid, 0)
    finally:
        # A regressed fixture must fail its own close assertion before this
        # control rescues its owner, within the original launch deadline.
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
                        f"Launch control rescue also failed: {rescue_error}"
                    )
