from __future__ import annotations

import gzip
import io
import json
import subprocess
import tarfile
import tempfile
import time
import zipfile
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

from h2h_dev_tools import code_review, process, review_execution, review_test_response
from h2h_dev_tools import review_coverage as coverage
from h2h_dev_tools import review_plan as planning


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(("git", *args), cwd=root).decode().strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "Review fixture")
    _git(tmp_path, "config", "user.email", "fixture@example.invalid")
    (tmp_path / "AGENTS.md").write_text("Review all source.\n")
    (tmp_path / "decode.py").write_text("value = 1\n")
    return tmp_path


def _commit(root: Path) -> str:
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "test: immutable fixture")
    return _git(root, "rev-parse", "HEAD")


def _plan(root: Path, *, base: str | None = None) -> planning.Plan:
    commit = _commit(root)
    target = {
        "tree": _git(root, "rev-parse", f"{commit}^{{tree}}"),
        "parents": [base or commit],
        "policy_digest": "fixture",
    }
    return planning.build_plan(
        root,
        target,
        {"scope": "repository" if base is None else "incremental", "baseline": None},
        deadline=time.monotonic() + 10,
    )


def _declare(
    root: Path,
    *,
    path: str = "data.bin",
    role: str = "fixture",
    kind: str = "opaque",
    sampled: list[str] | None = None,
    bound: int = 1048576,
) -> None:
    (root / "pyproject.toml").write_text(
        "[[tool.h2h-dev-tools.review.artifacts]]\n"
        f"path = {json.dumps(path)}\nrole = {json.dumps(role)}\nformat = {json.dumps(kind)}\n"
        'related-paths = ["decode.py"]\nreason = "Examine decoder and source correspondence."\n'
        f"max-expanded-bytes = {bound}\nmax-members = {16 if kind == 'tar-gzip' else 1}\n"
        + (
            f"sampled-data-members = {json.dumps(sampled)}\n"
            if sampled is not None
            else ""
        )
    )


def _tar(members: dict[str, bytes], *, link: bool = False) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            if link:
                info.type = tarfile.SYMTYPE
                info.linkname = "outside"
                info.size = 0
            archive.addfile(info, io.BytesIO(data))
    return output.getvalue()


def test_text_ranges_and_shards_cover_exact_utf8_bytes(repository: Path) -> None:
    data = ('normal = "測試"\n' * 50000).encode()
    (repository / "large.py").write_bytes(data)
    plan = _plan(repository)
    planning.validate_plan(plan.document())
    selected = [unit for unit in plan.units if unit.path == "large.py"]
    assert len(selected) > 1
    assert "".join(plan.payloads[unit.id] for unit in selected).encode() == data
    assert len(plan.shards) > 1
    assert all(
        len(planning.canonical(plan.assignment(i))) < planning.SHARD_BYTES + 4096
        for i in range(len(plan.shards))
    )


@pytest.mark.parametrize("rename", [False, True])
def test_deleted_binary_uses_parent_declaration_and_related_source(
    repository: Path, rename: bool
) -> None:
    _declare(repository)
    (repository / "data.bin").write_bytes(b"\0original")
    base = _commit(repository)
    if rename:
        (repository / "data.bin").rename(repository / "renamed.bin")
        _declare(repository, path="renamed.bin")
    else:
        (repository / "data.bin").unlink()
        (repository / "pyproject.toml").unlink()
        (repository / "decode.py").unlink()
    plan = _plan(repository, base=base)
    old = next(unit for unit in plan.units if unit.path == "data.bin")
    assert old.origin == base
    assert old.related_paths == ("decode.py",)
    assert plan.historical_related[0]["blob"] == _git(
        repository, "rev-parse", f"{base}:decode.py"
    )
    if rename:
        new = next(unit for unit in plan.units if unit.path == "renamed.bin")
        assert old.blob == new.blob and old.origin != new.origin


def test_only_explicit_historical_data_is_sampled_and_source_is_full(
    repository: Path,
) -> None:
    report = json.dumps({"samples": list(range(25000))}).encode()
    source = b"value = 42\n" * 10000
    _declare(
        repository, kind="tar-gzip", role="historical-evidence", sampled=["report.json"]
    )
    (repository / "data.bin").write_bytes(
        _tar({"report.json": report, "config.json": report, "runner.py": source})
    )
    plan = _plan(repository)
    assert {unit.kind for unit in plan.units if unit.member == "report.json"} == {
        "data-samples"
    }
    for member, expected in [("config.json", report), ("runner.py", source)]:
        selected = [unit for unit in plan.units if unit.member == member]
        assert {unit.kind for unit in selected} == {"archive-text"}
        assert "".join(plan.payloads[unit.id] for unit in selected).encode() == expected


@pytest.mark.parametrize(
    "scenario",
    [
        "undeclared",
        "oversize",
        "gzip-bomb",
        "link",
        "traversal",
        "unknown-member",
        "fixture-sampling",
        "invalid-json",
        "unknown-binary",
    ],
)
def test_unsafe_artifact_fails_closed(repository: Path, scenario: str) -> None:
    _declare(repository)
    data = b"\0binary"
    if scenario == "undeclared":
        (repository / "pyproject.toml").unlink()
    elif scenario == "oversize":
        _declare(repository, bound=1)
    elif scenario == "gzip-bomb":
        _declare(repository, kind="gzip", bound=100)
        data = gzip.compress(b"x" * 1000)
    else:
        if scenario in {
            "link",
            "traversal",
            "unknown-member",
            "fixture-sampling",
            "invalid-json",
            "unknown-binary",
        }:
            sampled = (
                ["report.json"]
                if scenario in {"unknown-member", "fixture-sampling", "invalid-json"}
                else []
            )
            _declare(
                repository,
                kind="tar-gzip",
                role="fixture"
                if scenario == "fixture-sampling"
                else "historical-evidence",
                sampled=sampled,
            )
            member = (
                "../bad.py"
                if scenario == "traversal"
                else "report.json"
                if scenario in {"fixture-sampling", "invalid-json"}
                else "file.py"
            )
            data = _tar(
                {member: b"\0binary" if scenario == "unknown-binary" else b"not json"},
                link=scenario == "link",
            )
    (repository / "data.bin").write_bytes(data)
    with pytest.raises(planning.PlanError):
        _plan(repository)


@pytest.mark.parametrize("members", [1, 4097])
def test_zip_artifact_declaration_is_rejected_before_directory_parsing(
    repository: Path, monkeypatch: pytest.MonkeyPatch, members: int
) -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for index in range(members):
            archive.writestr(f"source-{index}.py", b"")
    _declare(repository, kind="zip")
    (repository / "data.bin").write_bytes(output.getvalue())

    def unexpected_zip_reader(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Unsupported ZIP must be rejected before metadata parsing")

    monkeypatch.setattr(zipfile, "ZipFile", unexpected_zip_reader)
    with pytest.raises(planning.PlanError, match="Invalid artifact declaration"):
        _plan(repository)


def _passing(plan: planning.Plan, index: int) -> dict[str, Any]:
    return {
        "verdict": "pass",
        "summary": "Synthetic attestation, not semantic proof.",
        "findings": [],
        "plan_digest": plan.fingerprint,
        "shard_id": f"shard-{index:04d}",
        "unit_reviews": [
            {
                "unit_id": key,
                "method": "Fixture method.",
                "related_paths": list(
                    next(unit for unit in plan.units if unit.id == key).related_paths
                ),
            }
            for key in plan.shards[index]
        ],
        "integration_reviews": [
            {
                "obligation": key,
                "analysis": "Fixture analysis.",
                "source_paths": ["AGENTS.md"],
            }
            for key in planning.INTEGRATION_OBLIGATIONS
        ]
        if len(plan.shards) == 1
        else [],
        "contracts": [],
    }


@pytest.mark.parametrize(
    "corruption",
    [
        "missing",
        "duplicate",
        "wrong-unit",
        "wrong-plan",
        "missing-integration",
        "oversize-contract",
        "bad-range",
        "gap-range",
        "missing-stage",
        "finding",
        "unproven-owner",
        "target-drift",
    ],
)
def test_aggregate_rejects_incomplete_or_inconsistent_attestations(
    repository: Path, corruption: str
) -> None:
    plan = _plan(repository)
    result = _passing(plan, 0)
    stage = {
        "id": "shard-0000",
        "review": result,
        "review_sha256": "",
        "owned_tree_empty": True,
    }
    aggregate: dict[str, Any] = {
        "plan": deepcopy(plan.document()),
        "plan_digest": plan.fingerprint,
        "stages": [stage],
    }
    if corruption == "missing":
        result["unit_reviews"].pop()
    elif corruption == "duplicate":
        result["unit_reviews"].append(result["unit_reviews"][0])
    elif corruption == "wrong-unit":
        result["unit_reviews"][0]["unit_id"] = "unknown"
    elif corruption == "wrong-plan":
        result["plan_digest"] = "wrong"
    elif corruption == "missing-integration":
        result["integration_reviews"].pop()
    elif corruption == "oversize-contract":
        result["contracts"] = ["x" * 1025]
    elif corruption in {"bad-range", "gap-range"}:
        aggregate["plan"]["units"][0]["start"] = -1 if corruption == "bad-range" else 1
    elif corruption == "missing-stage":
        aggregate["stages"] = []
    elif corruption == "finding":
        result["findings"] = [{"priority": 2}]
    elif corruption == "unproven-owner":
        stage["owned_tree_empty"] = False
    elif corruption == "target-drift":
        aggregate["plan"]["target"]["tree"] = "other"
    stage["review_sha256"] = planning.digest(planning.canonical(result))
    with pytest.raises(planning.PlanError):
        coverage.validate_aggregate(aggregate, plan)


def test_duplicate_and_oversize_json_cannot_be_accepted() -> None:
    for data in [
        b'{"verdict":"pass","verdict":"incomplete"}',
        b" " * (planning.MAX_RESULT_BYTES + 1),
    ]:
        with pytest.raises(planning.PlanError):
            coverage.strict_json(data)


@pytest.mark.parametrize("batch_limit", [planning.BLOB_BATCH_BYTES, 512])
def test_git_blob_batches_preserve_exact_bytes_and_avoid_per_file_children(
    repository: Path, monkeypatch: pytest.MonkeyPatch, batch_limit: int
) -> None:
    expected = {
        f"source-{index}.py": f"# {index}\nvalue = '測試'\n".encode()
        for index in range(80)
    }
    for path, data in expected.items():
        (repository / path).write_bytes(data)
    original = review_execution.git_output
    calls = []

    def observe(*args: Any, **kwargs: Any) -> Any:
        if args[1:] == ("cat-file", "--batch"):
            calls.append(kwargs["input_bytes"])
        return original(*args, **kwargs)

    monkeypatch.setattr(review_execution, "git_output", observe)
    monkeypatch.setattr(planning, "BLOB_BATCH_BYTES", batch_limit)
    plan = _plan(repository)
    assert (
        (len(calls) == 1) if batch_limit == 8 * 1024 * 1024 else (1 < len(calls) < 82)
    )
    assert sum(len(call.splitlines()) for call in calls) == 82
    sizes = {entry["blob"]: entry["size"] for entry in plan.inventory}
    assert all(
        sum(sizes[blob.decode()] for blob in call.splitlines()) <= batch_limit
        for call in calls
    )
    for unit in plan.units:
        if unit.path in expected:
            assert plan.payloads[unit.id].encode() == expected[unit.path]


@pytest.mark.parametrize(
    "corruption", ["valid", "oid", "kind", "size", "truncated", "trailing"]
)
def test_git_blob_batch_rejects_wrong_identity_or_framing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corruption: str
) -> None:
    blob = "a" * 40
    valid = f"{blob} blob 5\n".encode() + b"a\n\0b!\n"
    output = {
        "valid": valid,
        "oid": valid.replace(blob.encode(), b"b" * 40),
        "kind": valid.replace(b" blob ", b" tree "),
        "size": valid.replace(b" 5\n", b" 6\n"),
        "truncated": valid[:-1],
        "trailing": valid + b"extra",
    }[corruption]
    cutoff = time.monotonic() + 10
    calls = 0

    def read(
        repository: Path,
        *args: str,
        deadline: float,
        input_bytes: bytes | None = None,
        max_output_bytes: int,
    ) -> bytes:
        nonlocal calls
        calls += 1
        assert repository == tmp_path
        assert args == ("cat-file", "--batch")
        assert deadline == cutoff
        assert input_bytes == f"{blob}\n".encode()
        assert max_output_bytes == len(valid)
        return output

    monkeypatch.setattr(review_execution, "git_output", read)
    if corruption == "valid":
        assert planning._blobs(tmp_path, [{"blob": blob, "size": 5}], cutoff) == {
            blob: b"a\n\0b!"
        }
    else:
        expected = {
            "oid": "Git blob batch identity/type/size mismatch",
            "kind": "Git blob batch identity/type/size mismatch",
            "size": "Git blob batch identity/type/size mismatch",
            "truncated": "Truncated Git blob batch payload",
            "trailing": "Unexpected trailing Git blob batch data",
        }[corruption]
        with pytest.raises(planning.PlanError, match=f"^{expected}$"):
            planning._blobs(tmp_path, [{"blob": blob, "size": 5}], cutoff)
    assert calls == 1


def _large_plan(
    *, shards: int, width: int = 13, path_padding: int = 0
) -> planning.Plan:
    units = tuple(
        planning.Unit(
            f"u{index:06d}",
            f"{'nested/' * path_padding}source-{index}.py",
            "a" * 40,
            "100644",
            "b" * 40,
            None,
            "text",
            0,
            1,
            1,
            planning.digest(b"x"),
            "Read source",
            (),
        )
        for index in range(shards * width)
    )
    return planning.Plan(
        {"tree": "b" * 40, "parents": ["c" * 40]},
        {"scope": "repository", "baseline": None},
        tuple(
            {"path": unit.path, "blob": unit.blob, "mode": unit.mode, "size": 1}
            for unit in units
        ),
        units,
        tuple(
            tuple(unit.id for unit in units[index * width : (index + 1) * width])
            for index in range(shards)
        ),
        {unit.id: "x" for unit in units},
        (
            {
                "path": "historical.py",
                "blob": "d" * 40,
                "mode": "100644",
                "size": 1,
                "origin": "e" * 40,
            },
        ),
    )


def _fill_handoff(budget: int, *, escaped: bool) -> list[str]:
    summaries: list[str] = []
    character = "\\" if escaped else "界"
    for _ in range(8):
        value = ""
        while (
            len((value + character).encode()) <= 1024
            and len(planning.wire([*summaries, value + character])) <= budget
        ):
            value += character
        while (
            len((value + "x").encode()) <= 1024
            and len(planning.wire([*summaries, value + "x"])) <= budget
        ):
            value += "x"
        if value:
            summaries.append(value)
    return summaries


@pytest.mark.parametrize("shards", [53, 64])
@pytest.mark.parametrize("escaped", [False, True])
def test_large_plan_preflights_full_handoff_wire_budget_and_coverage_map(
    shards: int, escaped: bool
) -> None:
    plan = _large_plan(shards=shards)
    planning.validate_plan(plan.document())
    budget = plan.handoff_budget
    assert planning.MIN_HANDOFF_BYTES <= budget < planning.MAX_HANDOFF_BYTES
    contracts = _fill_handoff(budget, escaped=escaped)
    assert len(planning.wire(contracts)) == budget
    result = _passing(plan, 0)
    result["contracts"] = contracts
    coverage.validate_attestation(result, plan, 0)
    assignment = plan.integration_assignment([contracts for _ in plan.shards])
    assert (
        len(planning.wire(assignment))
        == plan.integration_max_bytes
        <= planning.MAX_INTEGRATION_BYTES
    )
    assert len(assignment["coverage_map"]) == len(plan.units)
    for unit, entry in zip(plan.units, assignment["coverage_map"], strict=True):
        assert entry == {
            key: getattr(unit, key)
            for key in ("id", "path", "origin", "member", "kind", "start", "end")
        }
    assert assignment["historical_related"] == list(plan.historical_related)
    assert [handoff["contracts"] for handoff in assignment["handoffs"]] == [
        contracts
    ] * shards
    prefix = "根目錄審查政策\n" * 100
    plan.validate_prompt_capacity(prefix)
    assert (
        len(prefix.encode()) + len(planning.wire(assignment))
        <= planning.MAX_PROMPT_BYTES
    )
    too_large = contracts.copy()
    too_large[-1] += "x"
    result["contracts"] = too_large
    with pytest.raises(planning.PlanError, match="bounded"):
        coverage.validate_attestation(result, plan, 0)
    with pytest.raises(planning.PlanError, match="preflight"):
        plan.integration_assignment([too_large for _ in plan.shards])


def test_inventory_and_policy_overflow_fail_before_review_admission() -> None:
    plan = _large_plan(shards=64, path_padding=100)
    with pytest.raises(planning.PlanError, match="insufficient"):
        plan.document()
    plan = _large_plan(shards=64)
    with pytest.raises(planning.PlanError, match="before review"):
        plan.validate_prompt_capacity("界" * (planning.MAX_PROMPT_BYTES // 3))


def test_expired_planning_deadline_is_an_execution_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(time, "monotonic", lambda: 120.0)
    with pytest.raises(review_execution.ExecutionError, match="deadline"):
        planning.check_deadline(120.0)


def test_late_git_batch_cannot_return_usable_plan_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def late(*args: Any, **kwargs: Any) -> Any:
        time.sleep(0.02)
        return b"a blob 1\nx\n"

    monkeypatch.setattr(review_execution, "git_output", late)
    with pytest.raises(review_execution.ExecutionError, match="deadline"):
        planning._blobs(tmp_path, [{"blob": "a", "size": 1}], time.monotonic() + 0.01)


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        (
            "# UTF-8 原始碼\n" * 9000 + "danger_in_middle = True\n" + "# tail\n" * 9000
        ).encode(),
        b'{"configuration": true}\n' * 5000,
    ],
    ids=["empty", "source-multi-range", "configuration"],
)
def test_gzip_text_payload_is_fully_assigned_not_sampled(
    repository: Path, payload: bytes
) -> None:
    _declare(repository, kind="gzip")
    compressed = gzip.compress(payload)
    (repository / "data.bin").write_bytes(compressed)
    plan = _plan(repository)
    units = [
        unit
        for unit in plan.units
        if unit.path == "data.bin" and unit.kind == "gzip-text"
    ]
    assert units
    assert "".join(plan.payloads[unit.id] for unit in units).encode() == payload
    inventory = next(
        unit
        for unit in plan.units
        if unit.path == "data.bin" and unit.kind == "binary-inventory"
    )
    examination = json.loads(plan.payloads[inventory.id])
    assert examination["compressed"]["sha256"] == planning.digest(compressed)
    assert examination["payload"]["sha256"] == planning.digest(payload)
    planning.validate_plan(plan.document())


def test_gzip_binary_fixture_retains_explicit_decoder_contract(
    repository: Path,
) -> None:
    _declare(repository, kind="gzip")
    (repository / "data.bin").write_bytes(gzip.compress(b"\0opaque fixture"))
    plan = _plan(repository)
    units = [unit for unit in plan.units if unit.path == "data.bin"]
    assert {unit.kind for unit in units} == {"binary-contract"}
    assert all(unit.related_paths == ("decode.py",) for unit in units)


@pytest.mark.parametrize(
    "change", ["remove", "lower-bound", "absent-member", "text-declaration"]
)
def test_incremental_declaration_changes_revalidate_unchanged_payload(
    repository: Path, change: str
) -> None:
    _declare(
        repository, kind="tar-gzip", role="historical-evidence", sampled=["report.json"]
    )
    (repository / "data.bin").write_bytes(
        _tar({"report.json": b'{"numeric": [1, 2, 3]}'})
    )
    base = _commit(repository)
    if change == "remove":
        (repository / "pyproject.toml").write_text("[project]\n")
    elif change == "lower-bound":
        _declare(
            repository,
            kind="tar-gzip",
            role="historical-evidence",
            sampled=["report.json"],
            bound=1,
        )
    elif change == "absent-member":
        _declare(
            repository,
            kind="tar-gzip",
            role="historical-evidence",
            sampled=["missing.json"],
        )
    else:
        _declare(
            repository,
            kind="tar-gzip",
            role="historical-evidence",
            sampled=["report.json"],
        )
        project = repository / "pyproject.toml"
        previous = project.read_text()
        _declare(repository, path="AGENTS.md")
        project.write_text(previous + project.read_text())
    with pytest.raises(planning.PlanError):
        _plan(repository, base=base)


def test_removing_sampling_assigns_all_unchanged_member_bytes(repository: Path) -> None:
    report = json.dumps({"numeric": list(range(25000))}).encode()
    _declare(
        repository, kind="tar-gzip", role="historical-evidence", sampled=["report.json"]
    )
    (repository / "data.bin").write_bytes(_tar({"report.json": report}))
    base = _commit(repository)
    _declare(repository, kind="tar-gzip", role="historical-evidence", sampled=[])
    plan = _plan(repository, base=base)
    units = [unit for unit in plan.units if unit.member == "report.json"]
    assert units and {unit.kind for unit in units} == {"archive-text"}
    assert "".join(plan.payloads[unit.id] for unit in units).encode() == report


def test_changed_related_source_reexamines_unchanged_binary(repository: Path) -> None:
    _declare(repository)
    (repository / "data.bin").write_bytes(b"\0opaque")
    base = _commit(repository)
    (repository / "decode.py").write_text("value = 2\n")
    plan = _plan(repository, base=base)
    assert {unit.path for unit in plan.units} == {"data.bin", "decode.py"}
    assert next(
        unit for unit in plan.units if unit.path == "data.bin"
    ).related_paths == ("decode.py",)


def test_unrelated_project_metadata_does_not_reassign_unchanged_binary(
    repository: Path,
) -> None:
    _declare(repository)
    (repository / "data.bin").write_bytes(b"\0opaque")
    base = _commit(repository)
    project = repository / "pyproject.toml"
    project.write_text("[project]\nname = 'fixture'\n" + project.read_text())
    plan = _plan(repository, base=base)
    assert {unit.path for unit in plan.units} == {"pyproject.toml"}


def test_related_artifact_changes_follow_transitive_contracts_once(
    repository: Path,
) -> None:
    _declare(repository)
    project = repository / "pyproject.toml"
    first = project.read_text()
    _declare(repository, path="derived.bin")
    second = project.read_text().replace(
        'related-paths = ["decode.py"]', 'related-paths = ["data.bin"]'
    )
    project.write_text(first + second)
    (repository / "data.bin").write_bytes(b"\0opaque")
    (repository / "derived.bin").write_bytes(b"\0derived")
    base = _commit(repository)
    (repository / "decode.py").write_text("value = 2\n")
    plan = _plan(repository, base=base)
    assert {unit.path for unit in plan.units} == {
        "data.bin",
        "derived.bin",
        "decode.py",
    }
    assert len(plan.units) == 3


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("middle", [False, True])
def test_sampled_json_rejects_non_json_constants_everywhere(
    repository: Path, constant: str, middle: bool
) -> None:
    payload = (f"[0,1,2,3,{constant},5,6,7,8]" if middle else constant).encode()
    _declare(
        repository, kind="tar-gzip", role="historical-evidence", sampled=["report.json"]
    )
    (repository / "data.bin").write_bytes(_tar({"report.json": payload}))
    with pytest.raises(
        planning.PlanError, match="Cannot safely inspect archived JSON evidence"
    ):
        _plan(repository)


@pytest.mark.parametrize(
    "number", ["1e999", "-1e999", "1.123456789012345678901234567890", "9" * 5000]
)
def test_sampled_json_preserves_bounded_numeric_lexemes(
    repository: Path, number: str
) -> None:
    _declare(
        repository, kind="tar-gzip", role="historical-evidence", sampled=["report.json"]
    )
    (repository / "data.bin").write_bytes(_tar({"report.json": number.encode()}))
    plan = _plan(repository)
    unit = next(unit for unit in plan.units if unit.kind == "data-samples")
    view = json.loads(plan.payloads[unit.id])
    assert view["structure_and_value_samples"] == {
        "type": "number",
        "length": len(number),
        "sample": number[:160],
    }
    # The coordinator's wire representation must itself be strict JSON.
    assert json.dumps(view, allow_nan=False)


def _raw_tar(members: dict[str, bytes], *, format: int = tarfile.PAX_FORMAT) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:", format=format) as archive:
        for name, data in members.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return output.getvalue()


@pytest.mark.parametrize(
    "framing",
    [
        "trailing-data",
        "concatenated-tar",
        "concatenated-gzip",
        "broken-next-header",
        "truncated-next-header",
        "missing-terminator",
        "one-zero-block",
        "short-second-zero-block",
        "unaligned-padding",
    ],
)
def test_tar_rejects_uninventoried_or_incomplete_framing(
    repository: Path, framing: str
) -> None:
    first = _raw_tar({"first.py": b"first = 1\n"})
    second = _raw_tar({"hidden.py": b"hidden = 2\n"})
    if framing == "trailing-data":
        raw = first + b"hidden = 2\n"
    elif framing == "concatenated-tar":
        raw = first + second
    elif framing == "concatenated-gzip":
        raw = first
    elif framing == "broken-next-header":
        raw = first[:1024] + b"bad header".ljust(512, b"!") + second
    elif framing == "truncated-next-header":
        raw = first[:1024] + b"bad partial header"
    else:
        padding = {
            "missing-terminator": 0,
            "one-zero-block": 512,
            "short-second-zero-block": 1023,
            "unaligned-padding": 1025,
        }[framing]
        raw = first[:1024] + bytes(padding)
    compressed = gzip.compress(raw)
    if framing == "concatenated-gzip":
        compressed += gzip.compress(second)
    _declare(repository, kind="tar-gzip")
    (repository / "data.bin").write_bytes(compressed)
    with pytest.raises(planning.PlanError, match="Tar framing"):
        _plan(repository)


@pytest.mark.parametrize(
    "format", [tarfile.USTAR_FORMAT, tarfile.GNU_FORMAT, tarfile.PAX_FORMAT]
)
@pytest.mark.parametrize("padding_blocks", [2, 20])
def test_tar_complete_inventory_supports_standard_extended_headers_and_padding(
    repository: Path, format: int, padding_blocks: int
) -> None:
    name = (
        "runner.py" if format == tarfile.USTAR_FORMAT else "nested/" * 30 + "runner.py"
    )
    contents = {name: b"value = 42\n", "other.py": b"other = 7\n"}
    raw = _raw_tar(contents, format=format)
    # The independent stdlib reader locates the last payload boundary, including
    # PAX/GNU extension records; only the legal end padding is replaced.
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        members = archive.getmembers()
        last = members[-1]
        end = last.offset_data + ((last.size + 511) // 512) * 512
    _declare(repository, kind="tar-gzip")
    (repository / "data.bin").write_bytes(
        gzip.compress(raw[:end] + bytes(512 * padding_blocks))
    )
    plan = _plan(repository)
    inventory = next(unit for unit in plan.units if unit.kind == "archive-inventory")
    manifest = json.loads(plan.payloads[inventory.id])["members"]
    assert {member["path"]: member["size"] for member in manifest} == {
        path: len(data) for path, data in contents.items()
    }
    for path, content in contents.items():
        units = [unit for unit in plan.units if unit.member == path]
        assert units and {unit.kind for unit in units} == {"archive-text"}
        assert "".join(plan.payloads[unit.id] for unit in units).encode() == content


def test_tar_empty_complete_archive_has_explicit_empty_inventory(
    repository: Path,
) -> None:
    _declare(repository, kind="tar-gzip")
    (repository / "data.bin").write_bytes(gzip.compress(bytes(1024)))
    plan = _plan(repository)
    unit = next(unit for unit in plan.units if unit.kind == "archive-inventory")
    assert json.loads(plan.payloads[unit.id])["members"] == []


@pytest.mark.parametrize("excess", [False, True])
def test_tar_member_budget_remains_enforced(repository: Path, excess: bool) -> None:
    _declare(repository, kind="tar-gzip")
    (repository / "data.bin").write_bytes(
        _tar({f"source-{i}.py": b"" for i in range(16 + excess)})
    )
    if excess:
        with pytest.raises(planning.PlanError, match="safety budget"):
            _plan(repository)
    else:
        plan = _plan(repository)
        unit = next(unit for unit in plan.units if unit.kind == "archive-inventory")
        assert len(json.loads(plan.payloads[unit.id])["members"]) == 16


@pytest.mark.parametrize("excess", [False, True])
def test_tar_expanded_payload_budget_remains_enforced(
    repository: Path, excess: bool
) -> None:
    _declare(repository, kind="tar-gzip", bound=64)
    (repository / "data.bin").write_bytes(_tar({"source.py": b"#" * (64 + excess)}))
    if excess:
        with pytest.raises(planning.PlanError, match="safety budget"):
            _plan(repository)
    else:
        assert _plan(repository)


def test_tar_zero_padding_cannot_exceed_stream_envelope(repository: Path) -> None:
    _declare(repository, kind="tar-gzip", bound=1)
    (repository / "data.bin").write_bytes(
        gzip.compress(bytes(planning.MAX_MEMBERS * 1024 + 512))
    )
    with pytest.raises(planning.PlanError, match="bounded envelope"):
        _plan(repository)


@pytest.mark.parametrize("duplicate", [False, True])
def test_raw_aggregate_rejects_before_reading_any_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, duplicate: bool
) -> None:
    entries = [
        {"blob": "a" * 40, "size": 6},
        {"blob": ("a" if duplicate else "b") * 40, "size": 6},
    ]
    monkeypatch.setattr(planning, "MAX_TOTAL_BLOB_BYTES", 11, raising=False)
    monkeypatch.setattr(planning, "BLOB_BATCH_BYTES", 6)

    def reject_read(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Over-budget inventory reached Git blob reading")

    monkeypatch.setattr(review_execution, "git_output", reject_read)
    with pytest.raises(planning.PlanError, match="aggregate review byte budget"):
        planning._blobs(tmp_path, entries, time.monotonic() + 10)


def test_raw_aggregate_exact_boundary_deduplicates_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blob = "a" * 40
    entries = [{"blob": blob, "size": 5}] * 2
    monkeypatch.setattr(planning, "MAX_TOTAL_BLOB_BYTES", 10, raising=False)
    calls = []

    def read(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs["input_bytes"])
        return f"{blob} blob 5\nhello\n".encode()

    monkeypatch.setattr(review_execution, "git_output", read)
    assert planning._blobs(tmp_path, entries, time.monotonic() + 10) == {blob: b"hello"}
    assert calls == [(blob + "\n").encode()]


def test_deleted_paths_are_charged_to_aggregate_raw_budget(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("old-a.py", "old-b.py"):
        (repository / name).write_bytes(b"# old")
    base = _commit(repository)
    for name in ("old-a.py", "old-b.py"):
        (repository / name).unlink()
    monkeypatch.setattr(planning, "MAX_TOTAL_BLOB_BYTES", 9, raising=False)
    with pytest.raises(planning.PlanError, match="aggregate review byte budget"):
        _plan(repository, base=base)


@pytest.mark.parametrize("over", [False, True])
def test_git_metadata_is_bounded_before_returning_bytes(
    repository: Path, monkeypatch: pytest.MonkeyPatch, over: bool
) -> None:
    commit = _commit(repository)
    data = (repository / "AGENTS.md").read_bytes()
    monkeypatch.setattr(
        planning, "MAX_GIT_METADATA_BYTES", len(data) - int(over), raising=False
    )
    if over:
        with pytest.raises(review_execution.ExecutionError, match="Git output"):
            planning._git(
                repository,
                "show",
                commit + ":AGENTS.md",
                deadline=time.monotonic() + 10,
            )
    else:
        assert (
            planning._git(
                repository,
                "show",
                commit + ":AGENTS.md",
                deadline=time.monotonic() + 10,
            )
            == data
        )


def test_git_metadata_timeout_preserves_deadline_and_closes_spool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    streams = []

    def timeout(
        phase: process.ProcessCommand,
        *,
        hard_deadline: float | None,
        termination_reserve_seconds: float,
    ) -> int:
        assert phase.stdout is not None and not isinstance(phase.stdout, int)
        assert phase.stderr is not None and not isinstance(phase.stderr, int)
        streams.extend([phase.stdout, phase.stderr])
        assert hard_deadline is not None
        assert 0 < hard_deadline - time.monotonic() <= 10
        return process.TIMEOUT_EXIT_CODE

    monkeypatch.setattr(process, "run_command", timeout)
    with pytest.raises(review_execution.ExecutionError, match="Git query deadline"):
        planning._git(tmp_path, "ls-tree", "HEAD", deadline=time.monotonic() + 10)
    assert len(streams) == 2 and all(stream.closed for stream in streams)


@pytest.mark.parametrize("over", [False, True])
def test_inventory_entry_budget_boundary(
    repository: Path, monkeypatch: pytest.MonkeyPatch, over: bool
) -> None:
    commit = _commit(repository)
    monkeypatch.setattr(planning, "MAX_INVENTORY_ENTRIES", 2 - int(over), raising=False)
    if over:
        with pytest.raises(planning.PlanError, match="entry budget"):
            planning._inventory(repository, commit, time.monotonic() + 10)
    else:
        assert len(planning._inventory(repository, commit, time.monotonic() + 10)) == 2


@pytest.mark.parametrize("kind", ["view", "unit", "metadata"])
def test_retained_plan_budget_exact_boundary_and_over_one(
    repository: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    (repository / "long.py").write_bytes(b"# one long view\n" * 10000)
    baseline = _plan(repository)
    limit, amount = {
        "view": (
            "MAX_TOTAL_VIEW_BYTES",
            sum(len(value.encode()) for value in baseline.payloads.values()),
        ),
        "unit": ("MAX_REVIEW_UNITS", len(baseline.units)),
        "metadata": (
            "MAX_UNIT_METADATA_BYTES",
            sum(len(planning.canonical(asdict(unit))) for unit in baseline.units),
        ),
    }[kind]
    monkeypatch.setattr(planning, limit, amount, raising=False)
    target = baseline.target
    deadline = time.monotonic() + 10
    exact = planning.build_plan(repository, target, baseline.scope, deadline=deadline)
    assert exact.units == baseline.units and exact.payloads == baseline.payloads
    monkeypatch.setattr(planning, limit, amount - 1)
    with pytest.raises(planning.PlanError, match="aggregate"):
        planning.build_plan(repository, target, baseline.scope, deadline=deadline)


def test_each_examination_view_is_hashed_once_for_all_units(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = b"# long view\n" * 20000
    (repository / "large.py").write_bytes(data)
    original = planning.digest
    calls = 0

    def observe(value: bytes) -> str:
        nonlocal calls
        if value == data:
            calls += 1
        return original(value)

    monkeypatch.setattr(planning, "digest", observe)
    plan = _plan(repository)
    units = [unit for unit in plan.units if unit.path == "large.py"]
    assert len(units) > 1 and calls == 1
    assert all(unit.sha256 == original(data) for unit in units)
    assert "".join(plan.payloads[unit.id] for unit in units).encode() == data


@pytest.mark.parametrize("kind", ["gzip", "tar-gzip"])
def test_actual_expansion_budget_is_shared_across_artifacts(
    repository: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    data = b"print('bounded')\n"
    raw = gzip.compress(data) if kind == "gzip" else _tar({"source.py": data})
    expanded = gzip.decompress(raw)
    declarations = []
    for name in ("a.bin", "b.bin"):
        _declare(repository, path=name, kind=kind, bound=1024 * 1024)
        declarations.append((repository / "pyproject.toml").read_text())
        (repository / name).write_bytes(raw)
    (repository / "pyproject.toml").write_text("\n".join(declarations))
    monkeypatch.setattr(
        planning, "MAX_TOTAL_EXPANDED_BYTES", 2 * len(expanded), raising=False
    )
    baseline = _plan(repository)
    assert {unit.path for unit in baseline.units} >= {"a.bin", "b.bin"}
    monkeypatch.setattr(planning, "MAX_TOTAL_EXPANDED_BYTES", 2 * len(expanded) - 1)
    with pytest.raises(planning.PlanError, match=r"Expanded artifacts.*aggregate"):
        planning.build_plan(
            repository, baseline.target, baseline.scope, deadline=time.monotonic() + 10
        )


@pytest.mark.parametrize(
    ("limit", "value", "data"),
    [
        ("MAX_SAMPLED_JSON_BYTES", 6, b'["abcd"]'),
        ("MAX_JSON_SLOTS", 3, b"[0,1,2]"),
        ("MAX_JSON_DEPTH", 2, b"[[[0]]]"),
    ],
)
def test_sampled_json_budget_rejects_before_dom_parsing(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit: str,
    value: int,
    data: bytes,
) -> None:
    _declare(
        repository, kind="tar-gzip", role="historical-evidence", sampled=["report.json"]
    )
    (repository / "data.bin").write_bytes(_tar({"report.json": data}))
    monkeypatch.setattr(planning, limit, value, raising=False)
    original = json.loads

    def observe(value: Any, *args: Any, **kwargs: Any) -> Any:
        if value == data:
            pytest.fail("Over-budget sampled JSON reached DOM parser")
        return original(value, *args, **kwargs)

    monkeypatch.setattr(json, "loads", observe)
    with pytest.raises(planning.PlanError, match="Sampled JSON"):
        _plan(repository)


@pytest.mark.parametrize(
    "data", [b"[0,1,2]", b'{"a":0}', b"[[0]]", b'["[{:}]"]', b'["\\"[", "\\\\"]']
)
def test_json_budget_keeps_quotes_escapes_and_exact_boundaries(
    monkeypatch: pytest.MonkeyPatch, data: bytes
) -> None:
    monkeypatch.setattr(planning, "MAX_SAMPLED_JSON_BYTES", len(data))
    monkeypatch.setattr(planning, "MAX_JSON_SLOTS", 4)
    monkeypatch.setattr(planning, "MAX_JSON_DEPTH", 2)
    sampled = planning._json_sample(data, deadline=time.monotonic() + 10)
    assert json.loads(sampled)["sha256"] == planning.digest(data)


@pytest.mark.parametrize(
    "data", [b"[0,]", b'{"a":}', b'"unterminated', b"[NaN]", b"[] []"]
)
def test_json_budget_does_not_replace_full_syntax_validation(data: bytes) -> None:
    with pytest.raises(planning.PlanError, match="safely inspect"):
        planning._json_sample(data, deadline=time.monotonic() + 10)


def test_selected_entry_budget_rejects_before_deduplicating_or_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(planning, "MAX_INVENTORY_ENTRIES", 1)

    def reject_read(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Over-budget selected paths reached Git")

    monkeypatch.setattr(review_execution, "git_output", reject_read)
    with pytest.raises(planning.PlanError, match="Selected inventory"):
        planning._blobs(tmp_path, [{"blob": "a", "size": 0}] * 2, time.monotonic() + 10)


def test_expansion_read_uses_remaining_aggregate_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(planning, "MAX_TOTAL_EXPANDED_BYTES", 5)
    requests = []
    streams = []

    class ObservedStream(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            requests.append(size)
            return super().read(size)

    def stream(*args: Any, **kwargs: Any) -> ObservedStream:
        value = ObservedStream(b"abc")
        streams.append(value)
        return value

    monkeypatch.setattr(gzip, "GzipFile", stream)
    budget = planning._PlanBudget()
    assert budget.expand(b"compressed", 100) == b"abc"
    with pytest.raises(planning.PlanError, match="Expanded artifacts"):
        budget.expand(b"compressed", 100)
    assert requests == [6, 3]
    assert budget.expanded_bytes == 3
    assert all(value.closed for value in streams)


def test_views_from_multiple_archives_share_one_retention_budget(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    declarations = []
    for name in ("a.bin", "b.bin"):
        _declare(repository, path=name, kind="tar-gzip")
        declarations.append((repository / "pyproject.toml").read_text())
        (repository / name).write_bytes(_tar({"source.py": b"# full source\n" * 100}))
    (repository / "pyproject.toml").write_text("\n".join(declarations))
    baseline = _plan(repository)
    retained = sum(len(content.encode()) for content in baseline.payloads.values())
    monkeypatch.setattr(planning, "MAX_TOTAL_VIEW_BYTES", retained)
    exact = planning.build_plan(
        repository, baseline.target, baseline.scope, deadline=time.monotonic() + 10
    )
    assert exact.units == baseline.units and exact.payloads == baseline.payloads
    monkeypatch.setattr(planning, "MAX_TOTAL_VIEW_BYTES", retained - 1)
    with pytest.raises(planning.PlanError, match="Examination views"):
        planning.build_plan(
            repository, baseline.target, baseline.scope, deadline=time.monotonic() + 10
        )


def test_json_scan_checks_same_deadline_before_dom_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(planning, "UNIT_BYTES", 4)
    deadline = time.monotonic() + 10
    calls = []

    def expired(value: float) -> float:
        assert value == deadline
        calls.append(value)
        if len(calls) == 2:
            raise review_execution.ExecutionError("Aggregate review deadline expired")
        return 10.0

    def reject_parse(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Expired JSON scan reached DOM parser")

    monkeypatch.setattr(planning, "check_deadline", expired)
    monkeypatch.setattr(json, "loads", reject_parse)
    with pytest.raises(review_execution.ExecutionError, match="deadline"):
        planning._json_sample(b'["a long string"]', deadline=deadline)
    assert len(calls) == 2


@pytest.mark.parametrize(
    "encoding", ["utf-8-sig", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"]
)
@pytest.mark.parametrize("over", [False, True])
def test_json_budget_preserves_standard_encoding_and_escape_semantics(
    monkeypatch: pytest.MonkeyPatch, encoding: str, over: bool
) -> None:
    value = {"escaped": '\\" [ { : ,', "nested": [[0]]}
    data = json.dumps(value).encode(encoding)
    monkeypatch.setattr(planning, "MAX_JSON_DEPTH", 3 - int(over))
    if over:
        with pytest.raises(planning.PlanError, match="structure budget"):
            planning._json_sample(data, deadline=time.monotonic() + 10)
    else:
        sampled = json.loads(
            planning._json_sample(data, deadline=time.monotonic() + 10)
        )
        assert sampled["structure_and_value_samples"] == planning._data_shape(
            json.loads(data, parse_int=planning._JSONNumber)
        )
        assert sampled["sha256"] == planning.digest(data)


def test_git_metadata_size_is_checked_before_materializing_spool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(planning, "MAX_GIT_METADATA_BYTES", 4)

    class Spool(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            pytest.fail("Oversized Git stdout was read into planner memory")

    stream = Spool()

    def write(
        phase: process.ProcessCommand,
        *,
        hard_deadline: float | None,
        termination_reserve_seconds: float,
    ) -> int:
        assert phase.stdout is stream
        stream.write(b"12345")
        stream.seek(0)
        return 0

    monkeypatch.setattr(tempfile, "TemporaryFile", lambda: stream)
    monkeypatch.setattr(process, "run_command", write)
    with pytest.raises(review_execution.ExecutionError, match="Git output"):
        planning._git(tmp_path, "ls-tree", "HEAD", deadline=time.monotonic() + 10)
    assert stream.closed


def _maximum_response(plan: planning.Plan, index: int | None) -> dict[str, Any]:
    """Exercise the entire promised semantic reserve, not abbreviated coverage."""
    limits = plan.response_limits(index)
    selected = set() if index is None else set(plan.shards[index])
    return {
        "verdict": "pass",
        "summary": "\x01" * limits["summary_utf8_bytes"],
        "findings": [],
        "plan_digest": plan.fingerprint,
        "shard_id": "integration" if index is None else f"shard-{index:04d}",
        "unit_reviews": [
            {
                "unit_id": unit.id,
                "method": "\x01" * limits["unit_method_utf8_bytes"],
                "related_paths": list(unit.related_paths),
            }
            for unit in plan.units
            if unit.id in selected
        ],
        "integration_reviews": [
            {
                "obligation": obligation,
                "analysis": "\x01" * limits["integration_analysis_utf8_bytes"],
                "source_paths": list(plan.source_paths),
            }
            for obligation in planning.INTEGRATION_OBLIGATIONS
        ]
        if index is None or len(plan.shards) == 1
        else [],
        "contracts": _fill_handoff(limits["contracts_wire_bytes"], escaped=True),
    }


def _many_related_sources(repository: Path, *, escaped: bool) -> list[str]:
    related = []
    (repository / "related").mkdir()
    for index in range(200):
        # Keep real filenames portable; escaped control paths are exercised below.
        suffix = "界" * 20 if escaped else "a" * 50
        path = f"related/source_{index:04d}_{suffix}.py"
        (repository / path).write_text("value = 1\n")
        related.append(path)
    _declare(repository, kind="tar-gzip", role="generated")
    config = repository / "pyproject.toml"
    config.write_text(
        config.read_text()
        .replace('["decode.py"]', json.dumps(related, ensure_ascii=False))
        .replace("max-members = 16", "max-members = 64")
    )
    (repository / "data.bin").write_bytes(
        _tar({f"generated_{index:04d}.py": b"" for index in range(30)})
    )
    return related


@pytest.mark.parametrize("escaped", [False, True])
def test_response_planning_reserves_all_repeated_artifact_sources_and_text(
    repository: Path, escaped: bool
) -> None:
    related = _many_related_sources(repository, escaped=escaped)
    plan = _plan(repository)
    stages: list[int | None] = [*range(len(plan.shards)), None]
    artifact_units = {unit.id for unit in plan.units if unit.path == "data.bin"}
    assert len(plan.shards) > 1
    # The former input-only planner assigned 24 such units to its first shard.
    assert max(len(set(shard) & artifact_units) for shard in plan.shards) < 24
    assert len(artifact_units) == 31  # Archive inventory and all 30 embedded sources.
    # The old input-only first shard contained 24 artifact units. Their mandatory
    # path lists alone exceeded its 256 KiB raw output cap, even without prose.
    assert len(planning.wire([related] * 24)) > 256 * 1024
    assert {key for shard in plan.shards for key in shard} == {
        unit.id for unit in plan.units
    }
    maximum_wire = 0
    for index in stages:
        result = _maximum_response(plan, index)
        raw = planning.wire(result)
        maximum_wire = max(maximum_wire, len(raw))
        limit = plan.response_limits(index)["wire_bytes"]
        assert len(raw) == plan.response_capacity(index) <= limit
        coverage.validate_attestation(
            coverage.strict_json(raw, maximum=limit), plan, index
        )
        for review in result["unit_reviews"]:
            if review["unit_id"] in artifact_units:
                assert review["related_paths"] == related
        assignment = (
            plan.integration_assignment([[] for _ in plan.shards])
            if index is None
            else plan.assignment(index)
        )
        assert assignment["response_limits"] == plan.response_limits(index)
        assignment["integration_obligations"] = (
            list(planning.INTEGRATION_OBLIGATIONS) if index is None else []
        )
        synthetic = review_test_response.synthetic_review_response(
            review_test_response.ASSIGNMENT_MARKER
            + planning.wire(assignment).decode("utf-8")
        )
        coverage.validate_attestation(
            coverage.strict_json(planning.wire(synthetic), maximum=limit),
            plan,
            index,
        )
    assert maximum_wire > 256 * 1024  # Reproduces the old hard output cap failure.


def test_response_capacity_counts_exact_escaped_source_paths() -> None:
    baseline = _large_plan(shards=2, width=4)
    paths = tuple(f'source/{index}_界"\n\\.py' for index in range(8))
    units = tuple(
        replace(unit, path=path, related_paths=paths)
        for unit, path in zip(baseline.units, paths, strict=True)
    )
    plan = replace(
        baseline,
        units=units,
        inventory=tuple(
            {**entry, "path": path}
            for entry, path in zip(baseline.inventory, paths, strict=True)
        ),
    )
    plan.validate_prompt_capacity("")
    for index in (0, 1, None):
        result = _maximum_response(plan, index)
        raw = planning.wire(result)
        assert b"\\n" in raw and b'\\"' in raw and b"\\\\" in raw
        assert "界".encode() in raw
        assert len(raw) == plan.response_capacity(index)
        coverage.validate_attestation(
            coverage.strict_json(
                raw, maximum=plan.response_limits(index)["wire_bytes"]
            ),
            plan,
            index,
        )


def test_single_unit_without_full_response_capacity_fails_before_review(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _many_related_sources(repository, escaped=False)
    # A smaller declared cap isolates the one-unit boundary without huge fixtures.
    monkeypatch.setattr(planning, "SHARD_RESULT_BYTES", 80 * 1024)
    with pytest.raises(planning.PlanError, match="single review unit"):
        _plan(repository)


def test_single_shard_is_split_when_complete_response_needs_integration_stage(
    repository: Path,
) -> None:
    paths = []
    for source_index in range(700):
        path = f"source_{source_index:04d}_" + "a" * 90 + ".py"
        (repository / path).write_text("value = 1\n")
        paths.append(path)
    base = _commit(repository)
    for path in paths[:30]:
        (repository / path).write_text("value = 2\n")
    plan = _plan(repository, base=base)
    assert len(plan.units) == 30
    assert len(plan.shards) == 2
    for index in (0, 1, None):
        result = _maximum_response(plan, index)
        assert len(planning.wire(result)) == plan.response_capacity(index)
        assert (
            plan.response_capacity(index) <= plan.response_limits(index)["wire_bytes"]
        )
        coverage.validate_attestation(result, plan, index)


def test_integration_cannot_silently_reduce_its_source_or_analysis_reserve() -> None:
    plan = _large_plan(shards=40, width=25, path_padding=16)
    assert plan.integration_max_bytes <= planning.MAX_INTEGRATION_BYTES
    assert plan.response_capacity(None) > planning.MAX_RESULT_BYTES
    with pytest.raises(planning.PlanError, match="response wire budget before review"):
        plan.validate_prompt_capacity("")


@pytest.mark.parametrize("changed", [1, 33])
def test_complete_integration_overflow_fails_closed_for_single_and_many_shards(
    repository: Path, changed: int
) -> None:
    paths = []
    for index in range(1000):
        path = f"source_{index:04d}_" + "a" * 120 + ".py"
        (repository / path).write_text("value = 1\n")
        paths.append(path)
    base = _commit(repository)
    for path in paths[:changed]:
        (repository / path).write_text("value = 2\n")
    message = (
        "single complete review"
        if changed == 1
        else "response wire budget before review"
    )
    with pytest.raises(planning.PlanError, match=message):
        _plan(repository, base=base)


@pytest.mark.parametrize("field", ["summary", "method", "analysis"])
def test_semantic_text_cap_counts_decoded_utf8_bytes(
    repository: Path, field: str
) -> None:
    plan = _plan(repository)
    result = _passing(plan, 0)
    limit = {
        "summary": planning.SUMMARY_UTF8_BYTES,
        "method": planning.UNIT_METHOD_UTF8_BYTES,
        "analysis": planning.INTEGRATION_ANALYSIS_UTF8_BYTES,
    }[field]
    entry = (
        result
        if field == "summary"
        else result["unit_reviews" if field == "method" else "integration_reviews"][0]
    )
    entry[field] = "界" * (limit // 3) + "x" * (limit % 3)
    coverage.validate_attestation(result, plan, 0)
    entry[field] += "x"
    with pytest.raises(planning.PlanError):
        coverage.validate_attestation(result, plan, 0)
    if field == "summary":
        with pytest.raises(code_review.ReviewError, match="bounded"):
            code_review._result(result)


@pytest.mark.parametrize("integration", [False, True])
def test_per_assignment_raw_wire_bound_includes_whitespace(integration: bool) -> None:
    maximum = planning.response_limits(integration=integration)["wire_bytes"]
    raw = b"{}" + b" " * (maximum - 2)
    assert coverage.strict_json(raw, maximum=maximum) == {}
    with pytest.raises(planning.PlanError, match="bounded output"):
        coverage.strict_json(raw + b" ", maximum=maximum)


@pytest.mark.parametrize("maximum", [True, 0, planning.MAX_RESULT_BYTES + 1])
def test_parser_rejects_invalid_assignment_wire_limit(maximum: int) -> None:
    with pytest.raises(planning.PlanError, match="Invalid review response"):
        coverage.strict_json(b"{}", maximum=maximum)


def test_offline_attestation_cannot_bypass_wire_cap(repository: Path) -> None:
    plan = _plan(repository)
    result = _passing(plan, 0)
    # No individual finding text reserve is promised; the global wire cap applies.
    result["findings"] = [{"body": "x" * (planning.MAX_RESULT_BYTES + 1)}]
    with pytest.raises(planning.PlanError, match="response wire budget"):
        coverage.validate_attestation(result, plan, 0)


@pytest.mark.parametrize("integration", [False, True])
def test_duplicate_source_references_are_rejected(
    repository: Path, integration: bool
) -> None:
    plan = _plan(repository)
    result = _passing(plan, 0)
    if integration:
        result["integration_reviews"][0]["source_paths"] *= 2
    else:
        result["unit_reviews"][0]["related_paths"] = ["decode.py", "decode.py"]
    with pytest.raises(planning.PlanError, match="Malformed"):
        coverage.validate_attestation(result, plan, 0)
