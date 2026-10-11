from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from h2h_dev_tools.dependency_manifest import dependency_manifest

_ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), _ROOT / "scripts" / name
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", "-C", str(repository), *arguments),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit(repository: Path, message: str) -> str:
    git(repository, "add", ".")
    git(repository, "commit", "-m", message)
    return git(repository, "rev-parse", "HEAD")


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    git(root, "init", "-b", "master")
    git(root, "config", "user.name", "Release Test")
    git(root, "config", "user.email", "release@example.invalid")
    git(root, "config", "core.hooksPath", "/dev/null")
    git(root, "commit", "--allow-empty", "-m", "chore: initialize repository")
    git(root, "switch", "-c", "task")
    monkeypatch.chdir(root)
    return root


def write_project(repository: Path, version: str) -> None:
    text = f'''[project]
name = "release-fixture"
version = "{version}"
dependencies = []
[tool.h2h.version]
release-paths = ["src/**"]
'''
    (repository / "pyproject.toml").write_text(text)
    (repository / "package.json").write_text('{"devDependencies": {}}')
    receipt: dict[str, Any] = {
        "schema": "h2h.dependency-audit.v1",
        "project_version": version,
        "manifest_sha256": hashlib.sha256(
            dependency_manifest(tomllib.loads(text), {"devDependencies": {}})
        ).hexdigest(),
        "python": [],
        "node": [],
        "review": {"status": "reviewed", "note": "No external fixture dependencies"},
    }
    directory = repository / ".release"
    directory.mkdir(exist_ok=True)
    (directory / "dependency-audit.json").write_text(json.dumps(receipt))


def test_initial_release_requires_version_bound_candidate_audit(
    repository: Path,
) -> None:
    checker = load_script("check-version.py")
    base = git(repository, "rev-parse", "master")
    write_project(repository, "0.1.0")
    task = commit(repository, "feat: bootstrap development tools")
    checker.validate(base, task, task)
    (repository / ".release/dependency-audit.json").unlink()
    invalid = commit(repository, "test: remove required evidence")
    with pytest.raises(ValueError, match="dependency audit"):
        checker.validate(base, invalid, invalid)


@pytest.mark.parametrize("version", ["0.0.1", "1.0.0", "0.1.0.1", "00.1.0"])
def test_initial_release_rejects_another_version(
    repository: Path, version: str
) -> None:
    write_project(repository, version)
    task = commit(repository, "feat: bootstrap development tools")
    with pytest.raises(ValueError):
        load_script("check-version.py").validate("master", task, task)


def test_initial_release_does_not_apply_to_nonempty_missing_metadata(
    repository: Path,
) -> None:
    (repository / "README.md").write_text("Not an empty baseline\n")
    base = commit(repository, "docs: seed content")
    write_project(repository, "0.1.0")
    task = commit(repository, "feat: add package")
    with pytest.raises(ValueError, match="empty baseline"):
        load_script("check-version.py").validate(base, task, task)


def test_candidate_audit_rejects_stale_dependency_hash(repository: Path) -> None:
    write_project(repository, "0.1.0")
    (repository / "package.json").write_text(
        '{"devDependencies": {"markdownlint-cli2": ">=0.23.2"}}'
    )
    task = commit(repository, "feat: bootstrap development tools")
    with pytest.raises(ValueError, match="manifest mismatch"):
        load_script("check-version.py").validate("master", task, task)


def test_compatible_tool_release_has_one_dedicated_version_transition(
    repository: Path,
) -> None:
    checker = load_script("check-version.py")
    write_project(repository, "0.1.0")
    base = commit(repository, "feat: bootstrap development tools")
    (repository / "src").mkdir()
    (repository / "src/tool.py").write_text("value = 1\n")
    commit(repository, "feat: add tool behavior")
    write_project(repository, "0.1.1")
    task = commit(repository, "chore(release): bump version to 0.1.1")
    checker.validate(base, task, task)
    write_project(repository, "0.1.2")
    task = commit(repository, "chore(release): bump version to 0.1.2")
    with pytest.raises(ValueError, match=r"exactly one|dedicated release commit"):
        checker.validate(base, task, task)


def test_development_breaking_signal_does_not_change_tool_lane(
    repository: Path,
) -> None:
    checker = load_script("check-version.py")
    write_project(repository, "0.1.0")
    base = commit(repository, "feat: bootstrap development tools")
    (repository / "README.md").write_text("Changed contributor instructions\n")
    commit(repository, "docs!: change contributor workflow")
    (repository / "src").mkdir()
    (repository / "src/tool.py").write_text("value = 1\n")
    commit(repository, "fix: correct tool result")
    write_project(repository, "0.1.1")
    task = commit(repository, "chore(release): bump version to 0.1.1")
    checker.validate(base, task, task)


def test_runtime_change_without_version_and_unknown_paths_fail(
    repository: Path,
) -> None:
    checker = load_script("check-version.py")
    write_project(repository, "0.1.0")
    base = commit(repository, "feat: bootstrap development tools")
    (repository / "unknown").write_text("unclassified\n")
    task = commit(repository, "test: add unknown file")
    with pytest.raises(ValueError, match="unclassified"):
        checker.validate(base, task, task)
    (repository / "unknown").unlink()
    (repository / "src").mkdir()
    (repository / "src/tool.py").write_text("value = 1\n")
    task = commit(repository, "feat: add behavior without bump")
    with pytest.raises(ValueError, match="version transition"):
        checker.validate(base, task, task)


def test_candidate_audit_requires_the_full_dependency_inventory(
    repository: Path,
) -> None:
    write_project(repository, "0.1.0")
    manifest = repository / "pyproject.toml"
    manifest.write_text(
        manifest.read_text().replace(
            "dependencies = []", 'dependencies = ["packaging>=26.0"]'
        )
    )
    receipt_path = repository / ".release/dependency-audit.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["manifest_sha256"] = hashlib.sha256(
        dependency_manifest(
            tomllib.loads(manifest.read_text()), {"devDependencies": {}}
        )
    ).hexdigest()
    receipt_path.write_text(json.dumps(receipt))
    task = commit(repository, "feat: bootstrap development tools")
    with pytest.raises(ValueError, match="Python inventory is incomplete"):
        load_script("check-version.py").validate("master", task, task)


def test_release_commit_cannot_mix_runtime_work_with_version(repository: Path) -> None:
    write_project(repository, "0.1.0")
    base = commit(repository, "feat: bootstrap development tools")
    write_project(repository, "0.1.1")
    (repository / "src").mkdir()
    (repository / "src/tool.py").write_text("value = 1\n")
    task = commit(repository, "chore(release): bump version to 0.1.1")
    with pytest.raises(ValueError, match="may only change version and audit"):
        load_script("check-version.py").validate(base, task, task)
