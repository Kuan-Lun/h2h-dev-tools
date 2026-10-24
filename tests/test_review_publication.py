"""Real signals at the review evidence transaction's completion boundary."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

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
def interrupt_unlink(self, *args, **kwargs):
    if self == receipt and self.exists():
        for _ in range(2):
            rollback_signals.append(number)
            os.kill(os.getpid(), number)
    return original_unlink(self, *args, **kwargs)
if scenario == 'failed-rollback':
    Path.unlink = interrupt_unlink
try:
    with code_review._publication(receipt) as publication:
        receipt.write_text('validated fixture evidence')
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
