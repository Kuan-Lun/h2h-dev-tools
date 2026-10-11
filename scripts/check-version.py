#!/usr/bin/env python3
"""Validate one task-level version transition and candidate audit evidence."""

from __future__ import annotations

import argparse
import copy
import fnmatch
import hashlib
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from h2h_dev_tools.dependency_manifest import dependency_manifest

_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_DEVELOPMENT = (
    ".github/**",
    ".githooks/**",
    ".gitignore",
    ".markdownlint-cli2.jsonc",
    ".release/**",
    ".vscode/**",
    "AGENTS.md",
    "README.md",
    "docs/**",
    "package.json",
    "scripts/**",
    "tests/**",
)


def git(*arguments: str) -> str:
    return subprocess.run(
        ("git", *arguments), check=True, capture_output=True, text=True
    ).stdout.strip()


def tree_file(tree: str, path: str) -> bytes | None:
    if not git("ls-tree", tree, "--", path):
        return None
    return subprocess.run(
        ("git", "show", f"{tree}:{path}"), check=True, capture_output=True
    ).stdout


def document_at(tree: str) -> dict[str, Any] | None:
    raw = tree_file(tree, "pyproject.toml")
    return None if raw is None else tomllib.loads(raw.decode("utf-8"))


def parse_version(value: str) -> tuple[int, int, int]:
    match = _VERSION.fullmatch(value)
    if match is None:
        raise ValueError(f"project version must use canonical X.Y.Z: {value}")
    return int(match[1]), int(match[2]), int(match[3])


def release_metadata(document: dict[str, Any]) -> dict[str, Any]:
    project = copy.deepcopy(document.get("project", {}))
    project.pop("version", None)
    project.get("optional-dependencies", {}).pop("dev", None)
    return {
        "project": project,
        "build-system": document.get("build-system", {}),
        "distribution": document.get("tool", {}).get("hatch", {}).get("build", {}),
    }


def matches(path: str, patterns: list[str] | tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def release_changed(base: str, candidate: str) -> bool:
    old = document_at(base)
    new = document_at(candidate)
    if new is None:
        raise ValueError("candidate must contain pyproject.toml")
    paths = git("diff", "--name-only", "--no-renames", base, candidate).splitlines()
    patterns = (
        new.get("tool", {}).get("h2h", {}).get("version", {}).get("release-paths", [])
    )
    unknown = [
        path
        for path in paths
        if path != "pyproject.toml"
        and not matches(path, patterns)
        and not matches(path, _DEVELOPMENT)
    ]
    if unknown:
        raise ValueError("unclassified version impact: " + ", ".join(unknown))
    return (
        old is None
        or release_metadata(old) != release_metadata(new)
        or any(matches(path, patterns) for path in paths)
    )


def validate_audit(tree: str, document: dict[str, Any]) -> None:
    raw = tree_file(tree, ".release/dependency-audit.json")
    package_raw = tree_file(tree, "package.json")
    if raw is None or package_raw is None:
        raise ValueError("release requires candidate dependency audit and package.json")
    receipt = json.loads(raw)
    package = json.loads(package_raw)
    digest = hashlib.sha256(dependency_manifest(document, package)).hexdigest()
    if receipt.get("schema") != "h2h.dependency-audit.v1":
        raise ValueError("unsupported dependency audit schema")
    if receipt.get("project_version") != document["project"]["version"]:
        raise ValueError("dependency audit version mismatch")
    if receipt.get("manifest_sha256") != digest:
        raise ValueError("dependency audit manifest mismatch")
    review = receipt.get("review", {})
    note = review.get("note")
    if (
        review.get("status") != "reviewed"
        or not isinstance(note, str)
        or not note.strip()
    ):
        raise ValueError("dependency audit requires a nonempty compatibility review")
    if not isinstance(receipt.get("python"), list) or not isinstance(
        receipt.get("node"), list
    ):
        raise ValueError("dependency audit must contain Python and Node evidence")
    project = document.get("project", {})
    groups = {
        "build": document.get("build-system", {}).get("requires", []),
        "runtime": project.get("dependencies", []),
        **{
            f"optional:{name}": values
            for name, values in project.get("optional-dependencies", {}).items()
        },
        **{
            f"group:{name}": values
            for name, values in document.get("dependency-groups", {}).items()
        },
    }
    expected_python = sorted(
        (group, requirement)
        for group, requirements in groups.items()
        for requirement in requirements
        if isinstance(requirement, str)
    )
    actual_python = sorted(
        (entry["group"], entry["declared"]) for entry in receipt["python"]
    )
    if actual_python != expected_python:
        raise ValueError("dependency audit Python inventory is incomplete")
    expected_node = sorted(package.get("devDependencies", {}).items())
    actual_node = sorted(
        (entry["name"], entry["declared"]) for entry in receipt["node"]
    )
    if actual_node != expected_node:
        raise ValueError("dependency audit Node inventory is incomplete")


def validate(base: str, candidate: str, task: str) -> None:
    old = document_at(base)
    new = document_at(candidate)
    if new is None:
        raise ValueError("candidate must contain pyproject.toml")
    current_text = str(new["project"]["version"])
    current = parse_version(current_text)
    changed = release_changed(base, candidate)
    if old is None:
        if git("ls-tree", "-r", "--name-only", base):
            raise ValueError("initial release requires the explicit empty baseline")
        if current != (0, 1, 0):
            raise ValueError("initial release must use 0.1.0")
        validate_audit(candidate, new)
        return
    previous_text = str(old["project"]["version"])
    previous = parse_version(previous_text)
    if changed != (previous != current):
        raise ValueError("release-surface changes and version transition must agree")
    if previous == current:
        return

    breaking = False
    feature = False
    transitions = 0
    commits = git(
        "rev-list", "--reverse", "--no-merges", f"{base}..{task}"
    ).splitlines()
    for commit in commits:
        parent = git("rev-parse", f"{commit}^")
        before = document_at(parent)
        after = document_at(commit)
        if before is None or after is None:
            raise ValueError("existing project metadata cannot disappear within a task")
        if before["project"]["version"] != after["project"]["version"]:
            transitions += 1
            expected_subject = f"chore(release): bump version to {current_text}"
            if git("show", "-s", "--format=%s", commit) != expected_subject:
                raise ValueError(
                    "version transition requires its dedicated release commit"
                )
            version_files = set(git("diff", "--name-only", parent, commit).splitlines())
            if not version_files <= {
                "pyproject.toml",
                ".release/dependency-audit.json",
            }:
                raise ValueError("release commit may only change version and audit")
            before_metadata = copy.deepcopy(before)
            after_metadata = copy.deepcopy(after)
            before_metadata["project"].pop("version")
            after_metadata["project"].pop("version")
            if before_metadata != after_metadata:
                raise ValueError(
                    "release commit may only change project.version in metadata"
                )
        if release_changed(parent, commit):
            message = git("show", "-s", "--format=%B", commit)
            breaking |= bool(
                re.search(
                    r"^[a-z]+(?:\([^\n)]+\))?!:|^BREAKING CHANGE:",
                    message,
                    re.MULTILINE,
                )
            )
            feature |= bool(re.search(r"^feat(?:\([^\n)]+\))?:", message, re.MULTILINE))
    if transitions != 1:
        raise ValueError("task must contain exactly one project-version transition")
    major, minor, patch = previous
    if breaking:
        expected = (0, minor + 1, 0) if major == 0 else (major + 1, 0, 0)
    elif feature and major > 0:
        expected = (major, minor + 1, 0)
    else:
        expected = (major, minor, patch + 1)
    if current != expected:
        raise ValueError(
            f"expected version {'.'.join(map(str, expected))}, got {current_text}"
        )
    validate_audit(candidate, new)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", action="store_true")
    arguments = parser.parse_args()
    if arguments.index:
        base = git("rev-parse", "HEAD")
        task = git("rev-parse", "MERGE_HEAD")
        candidate = git("write-tree")
    else:
        primary = subprocess.run(
            ("scripts/detect-primary-branch.sh",),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        base = git("rev-parse", primary)
        task = git("rev-parse", "HEAD")
        candidate = git("merge-tree", "--write-tree", base, task).splitlines()[0]
    validate(base, candidate, task)
    print("Task-level version and dependency audit verified")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, OSError, subprocess.CalledProcessError, ValueError) as error:
        print(f"check-version: {error}", file=sys.stderr)
        raise SystemExit(1) from error
