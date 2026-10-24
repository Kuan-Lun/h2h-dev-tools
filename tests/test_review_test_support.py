from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from h2h_dev_tools import (
    bundle,
    bundle_verify,
    review_coverage,
    review_plan,
    review_test_response,
    review_test_support,
)


def _assignment() -> dict[str, Any]:
    return {
        "plan_digest": "a" * 64,
        "shard_id": "shard-0000",
        "response_limits": review_plan.response_limits(integration=True),
        "units": [{"id": "unit-a", "related_paths": ["decode.py"]}],
        "integration_obligations": ["cross-module-contract"],
    }


def _prompt(assignment: dict[str, Any]) -> str:
    return (
        "Synthetic policy.\n"
        + review_test_response.ASSIGNMENT_MARKER
        + json.dumps(assignment)
    )


def test_response_preserves_the_assignment_and_marks_synthetic_output() -> None:
    assignment = _assignment()
    result = review_test_response.synthetic_review_response(_prompt(assignment))
    assert result == {
        "verdict": "pass",
        "summary": "Synthetic fixture response; no code review performed.",
        "findings": [],
        "plan_digest": "a" * 64,
        "shard_id": "shard-0000",
        "unit_reviews": [
            {
                "unit_id": "unit-a",
                "method": "Synthetic fixture response; no code review performed.",
                "related_paths": ["decode.py"],
            }
        ],
        "integration_reviews": [
            {
                "obligation": "cross-module-contract",
                "analysis": "Synthetic fixture response; no code review performed.",
                "source_paths": ["AGENTS.md"],
            }
        ],
        "contracts": [],
    }
    assert assignment == _assignment()


@pytest.mark.parametrize("partitioned", [False, True])
@pytest.mark.parametrize("source_path", ["feature.py", " ", "\t", "\n"])
def test_responses_satisfy_real_coverage_but_faults_remain_rejected(
    partitioned: bool,
    source_path: str,
) -> None:
    units = tuple(
        review_plan.Unit(
            id=f"unit-{index}",
            path=path,
            blob="b" * 40,
            mode="100644",
            origin="candidate",
            member=None,
            kind="text",
            start=0,
            end=1,
            size=1,
            sha256="c" * 64,
            examination="text",
            related_paths=(source_path,) if index == 0 else (),
        )
        for index, path in enumerate(("AGENTS.md", source_path))
    )
    plan = review_plan.Plan(
        target={},
        scope={},
        inventory=tuple({"path": unit.path} for unit in units),
        units=units,
        shards=(("unit-0",), ("unit-1",)) if partitioned else (("unit-0", "unit-1"),),
        payloads={unit.id: "x" for unit in units},
    )
    for index in range(len(plan.shards)):
        assignment = plan.assignment(index)
        assignment["integration_obligations"] = (
            [] if partitioned else list(review_plan.INTEGRATION_OBLIGATIONS)
        )
        result = review_test_response.synthetic_review_response(_prompt(assignment))
        review_coverage.validate_attestation(result, plan, index)
        with pytest.raises(review_plan.PlanError, match="different plan"):
            review_coverage.validate_attestation(
                {**result, "plan_digest": "f" * 64}, plan, index
            )
        with pytest.raises(review_plan.PlanError, match="coverage"):
            review_coverage.validate_attestation(
                {**result, "unit_reviews": []}, plan, index
            )
    if partitioned:
        assignment = plan.integration_assignment([[], []])
        result = review_test_response.synthetic_review_response(_prompt(assignment))
        review_coverage.validate_attestation(result, plan, None)
        with pytest.raises(review_plan.PlanError, match="integration obligations"):
            review_coverage.validate_attestation(
                {**result, "integration_reviews": []}, plan, None
            )


@pytest.mark.parametrize(
    "change",
    [
        {"plan_digest": None},
        {"plan_digest": "wrong"},
        {"shard_id": "old"},
        {"units": None},
        {"units": []},
        {"units": ["unit"]},
        {"units": [{"id": "a", "related_paths": [1]}]},
        {"units": [{"id": "a", "related_paths": ["source.py", "source.py"]}]},
        {"units": [{"id": "a", "related_paths": []}] * 2},
        {"integration_obligations": None},
        {"integration_obligations": [" "]},
        {"units": [{"id": "a", "related_paths": [""]}]},
        {"integration_obligations": ["a", "a"]},
        {"shard_id": "integration"},
        {"response_limits": None},
        {"response_limits": {"wire_bytes": 1024}},
        {
            "response_limits": {
                **review_plan.response_limits(integration=True),
                "wire_bytes": True,
            }
        },
        {
            "response_limits": {
                **review_plan.response_limits(integration=True),
                "wire_bytes": 1,
            }
        },
        {
            "response_limits": {
                **review_plan.response_limits(integration=True),
                "summary_utf8_bytes": 1,
            }
        },
    ],
)
def test_malformed_assignment_is_not_implicit_pass(change: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        review_test_response.synthetic_review_response(
            _prompt({**_assignment(), **change})
        )


@pytest.mark.parametrize(
    "prompt",
    [
        "Review this old V2 prompt.",
        review_test_response.ASSIGNMENT_MARKER + "[]",
        review_test_response.ASSIGNMENT_MARKER + "not JSON",
        review_test_response.ASSIGNMENT_MARKER + '{"units":[],"units":[]}',
    ],
)
def test_legacy_malformed_and_duplicate_json_are_rejected(prompt: str) -> None:
    with pytest.raises(ValueError):
        review_test_response.synthetic_review_response(prompt)


def test_component_is_explicit_and_not_in_production_review_closure() -> None:
    support = {"review_test_response.py", "review_test_support.py"}
    assert bundle_verify.component_files(["review-test-support"]) == (
        bundle_verify.BASE_FILES | support
    )
    assert not support & bundle_verify.component_files(["code-review", "git-flow"])


def _package(root: Path, implementation: str) -> tuple[Path, str]:
    if implementation == "snapshot":
        (root / "pyproject.toml").write_text(
            '[tool.h2h-dev-tools]\ncomponents=["review-test-support"]\n'
        )
        bundle.sync_bundle(root)
        bundle_verify.verify_bundle(root)
        return root / bundle_verify.BUNDLE_DIRECTORY, "_shared.h2h_dev_tools"
    target = root / "src/h2h_dev_tools"
    target.mkdir(parents=True)
    source = Path(review_test_response.__file__).parent
    for name in bundle_verify.component_files(["review-test-support"]):
        shutil.copy2(source / name, target / name)
    return target, "h2h_dev_tools"


@pytest.mark.parametrize("implementation", ["source", "snapshot"])
@pytest.mark.parametrize("mutate", [False, True])
def test_export_uses_verified_bytes_and_fake_child_has_no_package_dependency(
    tmp_path: Path,
    implementation: str,
    mutate: bool,
) -> None:
    package, namespace = _package(tmp_path, implementation)
    script = tmp_path / "fake-cli.py"
    bootstrap = """
import importlib, runpy, sys
from pathlib import Path
package, namespace, output, mutate = sys.argv[1:]
package = Path(package)
if namespace == '_shared.h2h_dev_tools':
    sys.path.insert(0, str(package.parents[1]))
runpy.run_path(str(package / 'source_loader.py'))['load_package'](package, namespace)
support = importlib.import_module(namespace + '.review_test_support')
if mutate == 'yes':
    with (package / 'review_test_response.py').open('a') as stream:
        stream.write('\\n# changed after verified import\\n')
source = support.synthetic_review_response_source()
Path(output).write_text(source + '\\nimport sys\\nprint(json.dumps(synthetic_review_response(sys.stdin.read()), ensure_ascii=False, separators=(",", ":")))\\n')
"""
    exported = subprocess.run(
        (
            sys.executable,
            "-I",
            "-B",
            "-c",
            bootstrap,
            str(package),
            namespace,
            str(script),
            "yes" if mutate else "no",
        ),
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if mutate:
        assert exported.returncode != 0
        assert "Source changed after bootstrap" in exported.stderr
        assert not script.exists()
        return
    assert exported.returncode == 0, exported.stderr
    expected = review_test_support.synthetic_review_response_source()
    assert script.read_text().startswith(expected)
    # No package remains available: the actual fake CLI runs using stdlib only.
    shutil.rmtree(package)
    prompt = _prompt(_assignment())
    result = subprocess.run(
        (sys.executable, "-I", "-S", "-B", str(script)),
        input=prompt,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == review_test_response.synthetic_review_response(
        prompt
    )


def test_export_rejects_an_ordinary_unverified_import(tmp_path: Path) -> None:
    package, _ = _package(tmp_path, "source")
    result = subprocess.run(
        (
            sys.executable,
            "-I",
            "-B",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from h2h_dev_tools.review_test_support import synthetic_review_response_source; "
            "synthetic_review_response_source()",
            str(package.parent),
        ),
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode != 0
    assert "requires verified source loading" in result.stderr
