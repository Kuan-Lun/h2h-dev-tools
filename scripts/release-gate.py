#!/usr/bin/env python3
"""Check the real staged merge candidate and reject changes during the gate."""

from __future__ import annotations

import subprocess
import sys


def git(*arguments: str) -> str:
    return subprocess.run(
        ("git", *arguments), check=True, capture_output=True, text=True
    ).stdout.strip()


def candidate() -> tuple[str, str, str]:
    return git("rev-parse", "HEAD"), git("rev-parse", "MERGE_HEAD"), git("write-tree")


def main() -> None:
    before = candidate()
    if git("diff", "--name-only"):
        raise ValueError("merge candidate has unstaged changes")
    subprocess.run((sys.executable, "scripts/check-version.py", "--index"), check=True)
    profile = subprocess.run(
        (sys.executable, "scripts/check_change_scope.py", "--index", "--base", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    command: tuple[str, ...]
    if profile == "documentation":
        command = (sys.executable, "scripts/check-docs.py", "--index", "--base", "HEAD")
    elif profile == "full":
        command = ("scripts/check-full.sh",)
    else:
        raise ValueError(f"unrecognized candidate profile: {profile}")
    subprocess.run(command, check=True)
    if candidate() != before or git("diff", "--name-only"):
        raise ValueError("merge candidate changed during the gate")
    print(f"Verified {profile} merge candidate {before[2]}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        print(f"release-gate: {error}", file=sys.stderr)
        raise SystemExit(1) from error
