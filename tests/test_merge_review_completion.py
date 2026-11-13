"""Normal review completion must survive the managed child/merge boundary."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal
from unittest.mock import Mock

import pytest
from test_merge_workflow import (
    executable,
    fixture_repository,
    git,
    owned_fixture_command,
    record_workflow_receipts,
)
from test_merge_workflow import offline_codex as offline_codex

from h2h_dev_tools import merge_command, review_execution, review_test_support
from h2h_dev_tools.executable_fixture import write_python_executable

# The real workflow uses a local synthetic Codex executable, never the network.
pytestmark = pytest.mark.usefixtures("offline_codex")


def test_classifier_exit_125_with_managed_cleanup_remains_pre_admission_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    classifier = Mock(return_value=125)
    monkeypatch.setattr(merge_command, "_run_owned", classifier)
    receipt = tmp_path / "cleanup"
    status = merge_command._execute(
        tmp_path, ("review",), review=True, recovery=False, receipt=receipt
    )
    assert status == 1
    assert receipt.read_text() == "owned_tree_empty\n"
    assert not receipt.with_name("cleanup.failure.json").exists()
    classifier.assert_called_once()
    assert classifier.call_args.args[0].label == "Classify review candidate"


@pytest.mark.parametrize(
    "status,completion", [(0, "attested"), (1, "attested"), (1, "not-started")]
)
def test_managed_completion_is_bound_to_parent_target_source_and_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    completion: Literal["not-started", "attested"],
) -> None:
    destination = tmp_path / "completion.json"
    monkeypatch.setenv("H2H_REVIEW_PROCESS_OWNER", str(os.getpid()))
    monkeypatch.setenv("H2H_REVIEW_OWNER_SOURCE", "source-fingerprint")
    monkeypatch.setenv("H2H_REVIEW_COMPLETION_RECEIPT", str(destination))
    merge_command.write_review_completion(
        target_key="candidate-key", status=status, completion=completion
    )
    merge_command._require_review_completion(
        destination,
        target_key="candidate-key",
        status=status,
        owner_source="source-fingerprint",
    )
    with pytest.raises(FileExistsError):
        merge_command.write_review_completion(
            target_key="candidate-key", status=status, completion=completion
        )
    for key, value in (
        ("schema", True),
        ("target_key", "another-candidate"),
        ("owner_pid", os.getpid() + 1),
        ("owner_source", "another-source"),
        ("exit_status", status == 1),
        ("completion", "unknown"),
        ("extra", "unexpected"),
    ):
        document = json.loads(destination.read_bytes())
        changed = document | {key: value}
        destination.write_text(json.dumps(changed))
        with pytest.raises(review_execution.ReviewCompletionUnproven):
            merge_command._require_review_completion(
                destination,
                target_key="candidate-key",
                status=status,
                owner_source="source-fingerprint",
            )
        destination.write_text(json.dumps(document))


@pytest.mark.parametrize(
    "payload",
    [None, b"{", b"null", b"[]", b"x" * 4097, b'{"schema":1,"schema":1}'],
)
@pytest.mark.parametrize("status", [0, 1, 125, -9, 130])
def test_exit_status_and_malformed_completion_never_authorize_cleanup(
    tmp_path: Path, payload: bytes | None, status: int
) -> None:
    destination = tmp_path / "completion.json"
    if payload is not None:
        destination.write_bytes(payload)
    with pytest.raises(review_execution.ReviewCompletionUnproven):
        merge_command._require_review_completion(
            destination, target_key="candidate", status=status, owner_source="source"
        )


@pytest.mark.skipif(os.name != "posix", reason="POSIX online review workflow")
@pytest.mark.parametrize("exit_kind", ["zero", "one", "125", "killed", "term"])
def test_unexpected_reviewer_exit_retains_real_merge_and_admission(
    tmp_path: Path, exit_kind: str
) -> None:
    root = tmp_path / "repository"
    base = fixture_repository(root, succeeds=True)
    receipt_records = tmp_path / "phase-receipts"
    record_workflow_receipts(root, receipt_records)
    endings = {
        "zero": "raise SystemExit(0)\n",
        "one": "raise SystemExit(1)\n",
        "125": "raise SystemExit(125)\n",
        "killed": "os.kill(os.getpid(), signal.SIGKILL)\n",
        "term": "os.kill(os.getppid(), signal.SIGTERM)\nraise SystemExit(0)\n",
    }
    (root / "scripts/review-code.py").write_text(
        "import os, signal\n" + endings[exit_kind]
    )
    git(root, "add", ".")
    git(root, "commit", "-m", "test: exercise missing review completion")
    task = git(root, "rev-parse", "HEAD")
    result = owned_fixture_command(root, ("scripts/git-flow-merge.sh",))
    assert result.returncode != 0, result.stdout + result.stderr
    assert git(root, "rev-parse", "master") == base
    assert git(root, "rev-parse", "task") == task
    assert git(root, "branch", "--show-current") == "master"
    assert git(root, "rev-parse", "MERGE_HEAD") == task
    assert (root / ".git/h2h-merge-workflow.lock").is_dir()
    assert list((root / ".git/h2h-review/locks").glob("*.unproven.json"))
    assert list((root / ".git/h2h-review").glob("pending-*"))
    assert not (root / ".git/fixture-gate").exists()
    records = [line.split("\t") for line in receipt_records.read_text().splitlines()]
    review_receipts = [Path(path) for owner, path in records if owner == "review"]
    assert len(review_receipts) == 1
    assert not review_receipts[0].exists()
    diagnostic = json.loads(
        review_receipts[0]
        .with_name(review_receipts[0].name + ".failure.json")
        .read_bytes()
    )
    assert diagnostic["cleanup_state"] == "unknown"
    assert not list((root / ".git/h2h-review/receipts").glob("*.json"))


@pytest.mark.skipif(os.name != "posix", reason="POSIX online review workflow")
def test_managed_preflight_failure_before_model_admission_can_recover(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    base = fixture_repository(root, succeeds=True)
    executable(
        root / "scripts/review-merge-candidate.sh",
        "#!/bin/sh\nexec .venv/bin/python scripts/review-code.py run --index "
        "--min-shard-seconds 14401\n",
    )
    git(root, "add", ".")
    git(root, "commit", "-m", "test: reject before model admission")
    task = git(root, "rev-parse", "HEAD")
    result = owned_fixture_command(root, ("scripts/git-flow-merge.sh",))
    assert result.returncode != 0, result.stdout + result.stderr
    assert "--min-shard-seconds must be between" in result.stderr
    assert git(root, "rev-parse", "master") == base
    assert git(root, "rev-parse", "task") == task
    assert git(root, "branch", "--show-current") == "task"
    assert not (root / ".git/MERGE_HEAD").exists()
    assert not (root / ".git/h2h-merge-workflow.lock").exists()
    assert not list((root / ".git/h2h-review/locks").glob("*.unproven.json"))
    assert not list((root / ".git/h2h-review/receipts").glob("*.json"))


@pytest.mark.skipif(os.name != "posix", reason="POSIX online review workflow")
def test_model_exit_zero_without_work_attestation_retains_real_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository"
    base = fixture_repository(root, succeeds=True)
    binary = tmp_path / "incomplete-codex"
    binary.mkdir()
    write_python_executable(
        binary / "codex",
        "import json, sys\nfrom pathlib import Path\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('codex-cli missing-completion-fixture')\n"
        "    raise SystemExit(0)\n"
        "prompt = sys.stdin.read()\n"
        "output = Path(sys.argv[sys.argv.index('--output-last-message') + 1])\n"
        + review_test_support.synthetic_review_response_source()
        + "\nresult = synthetic_review_response(prompt)\n"
        "result.pop('work_completion')\n"
        "output.write_text(json.dumps(result))\n",
    )
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ.get("PATH", ""))
    task = git(root, "rev-parse", "HEAD")
    result = owned_fixture_command(root, ("scripts/git-flow-merge.sh",))
    assert result.returncode != 0, result.stdout + result.stderr
    assert git(root, "rev-parse", "master") == base
    assert git(root, "rev-parse", "task") == task
    assert git(root, "rev-parse", "MERGE_HEAD") == task
    assert (root / ".git/h2h-merge-workflow.lock").is_dir()
    assert list((root / ".git/h2h-review/locks").glob("*.unproven.json"))
    assert not list((root / ".git/h2h-review/receipts").glob("*.json"))
    assert not (root / ".git/fixture-gate").exists()
    results = list((root / ".git/h2h-review/runs").glob("*/shard-*/result.json"))
    assert results, "the successful CLI's raw result must remain available"
    assert all(
        "work_completion" not in json.loads(path.read_bytes()) for path in results
    )
