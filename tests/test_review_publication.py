"""Real signals at the review evidence transaction's completion boundary."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from h2h_dev_tools import code_review, review_execution

PUBLICATION_PROBE = r"""
import os
import runpy
import signal
import sys
import time
from pathlib import Path

package, receipt = map(Path, sys.argv[1:3])
scenario, signal_name = sys.argv[3:]
runpy.run_path(str(package / 'source_loader.py'))['load_package'](package, 'h2h_dev_tools')
from h2h_dev_tools import code_review, process

number = getattr(signal, signal_name)
previous = {number: signal.getsignal(number) for number in
            (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
original_unlink = Path.unlink
original_failure = OSError('original publication failure')
rollback_signals = []
staging = None
def interrupt_unlink(self, *args, **kwargs):
    if self == staging and self.exists():
        for _ in range(2):
            rollback_signals.append(number)
            os.kill(os.getpid(), number)
    return original_unlink(self, *args, **kwargs)
if scenario == 'failed-rollback':
    Path.unlink = interrupt_unlink
try:
    with code_review._publication(receipt) as publication:
        staging = publication.staging
        staging.write_text('validated fixture evidence')
        if scenario == 'failed-rollback':
            raise original_failure
        publication.complete(time.monotonic() + 10)
        if scenario == 'before-commit':
            os.kill(os.getpid(), number)
except OSError as error:
    assert scenario == 'failed-rollback' and error is original_failure
    assert rollback_signals == [number, number]
except (KeyboardInterrupt, process.RunnerSignalInterrupt) as error:
    assert scenario == 'before-commit'
    if isinstance(error, process.RunnerSignalInterrupt):
        assert error.signal_number == number
else:
    assert scenario == 'after-commit'
assert all(signal.getsignal(number) is handler for number, handler in previous.items())
if scenario == 'after-commit':
    assert receipt.read_text() == 'validated fixture evidence'
    # All transaction work has completed and the original handlers are restored.
    # Cancellation here does not invalidate the already committed evidence.
    os.kill(os.getpid(), number)
else:
    assert not receipt.exists()
    print('original failure and prior handlers preserved; receipt revoked')
"""


def _publication_probe(
    tmp_path: Path, scenario: str, signal_name: str
) -> tuple[subprocess.CompletedProcess[str], Path]:
    if os.name != "posix":
        pytest.skip("Online review publication and these OS signals require POSIX")
    package = Path(__file__).resolve().parents[1] / "src/h2h_dev_tools"
    receipt = tmp_path / "receipt.json"
    result = subprocess.run(
        (
            sys.executable,
            "-I",
            "-c",
            PUBLICATION_PROBE,
            str(package),
            str(receipt),
            scenario,
            signal_name,
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return result, receipt


@pytest.mark.parametrize("scenario", ["failed-rollback", "before-commit"])
@pytest.mark.parametrize("signal_name", ["SIGINT", "SIGTERM", "SIGHUP"])
def test_precommit_signal_revokes_evidence_and_preserves_original_failure(
    tmp_path: Path, scenario: str, signal_name: str
) -> None:
    result, receipt = _publication_probe(tmp_path, scenario, signal_name)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "receipt revoked" in result.stdout
    assert not receipt.exists()


def test_postcommit_signal_preserves_completed_evidence(tmp_path: Path) -> None:
    result, receipt = _publication_probe(tmp_path, "after-commit", "SIGTERM")
    assert result.returncode == -signal.SIGTERM, result.stdout + result.stderr
    assert receipt.read_text() == "validated fixture evidence"


@pytest.mark.parametrize(
    "boundary",
    [
        "expired-before-commit",
        "rename-failed",
        "renamed-then-error",
        "renamed-then-late-return",
    ],
)
def test_terminal_publication_has_one_canonical_commitpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    receipt = tmp_path / "receipt.json"
    original_replace = os.replace
    clock = time.monotonic
    offset = 0.0
    monkeypatch.setattr(time, "monotonic", lambda: clock() + offset)
    deadline = time.monotonic() + 10
    replacements: list[Path] = []
    failure = OSError("injected terminal rename failure")

    def replace(source: Path, destination: Path) -> None:
        nonlocal offset
        assert not receipt.exists(), "Canonical evidence must be absent before commit"
        assert destination == receipt and source.parent == receipt.parent
        assert time.monotonic() < deadline
        if boundary == "rename-failed":
            raise failure
        original_replace(source, destination)
        replacements.append(destination)
        if boundary == "renamed-then-error":
            raise failure
        offset = 1000.0

    monkeypatch.setattr(os, "replace", replace)
    caught: BaseException | None = None
    try:
        with code_review._publication(receipt) as publication:
            publication.staging.write_text("validated evidence")
            assert not receipt.exists()
            publication.complete(deadline)
            if boundary == "expired-before-commit":
                offset = 1000.0
    except (OSError, review_execution.ExecutionError) as error:
        caught = error
    if boundary == "expired-before-commit":
        assert isinstance(caught, review_execution.ExecutionError)
        assert not replacements and not receipt.exists()
    elif boundary == "rename-failed":
        assert caught is failure
        assert not replacements and not receipt.exists()
    else:
        assert replacements == [receipt]
        assert receipt.read_text() == "validated evidence"
        assert caught is (failure if boundary == "renamed-then-error" else None)
        if boundary == "renamed-then-late-return":
            # The last syscall began in budget and committed. Its delayed return
            # is not a new fallible validation/rollback phase.
            assert time.monotonic() > deadline
    assert list(tmp_path.iterdir()) == ([receipt] if receipt.exists() else [])


def test_failed_old_receipt_withdrawal_cannot_admit_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = tmp_path / "receipt.json"
    receipt.write_text("previous completed evidence")
    failure = OSError("previous evidence cannot be withdrawn")
    original_unlink = Path.unlink
    admitted = False

    def unlink(path: Path, *, missing_ok: bool = False) -> None:
        if path == receipt:
            raise failure
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(OSError) as raised:
        with code_review._publication(receipt):
            admitted = True
    assert raised.value is failure
    assert not admitted
    assert receipt.read_text() == "previous completed evidence"
    assert list(tmp_path.iterdir()) == [receipt]
