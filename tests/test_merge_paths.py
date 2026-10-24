from __future__ import annotations

import os
import subprocess
from pathlib import Path, PureWindowsPath

import pytest

from h2h_dev_tools import merge_admission as admission


def git(root: Path, *arguments: str) -> bytes:
    return subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        timeout=10,
    ).stdout


@pytest.fixture
def tracked_repository(tmp_path: Path) -> admission.Workflow:
    root = tmp_path / "repository"
    directory = root / "src" / "nested"
    directory.mkdir(parents=True)
    (directory / "Original.py").write_bytes(b"tracked bytes\n")
    git(root, "init", "-b", "master")
    git(root, "config", "user.name", "Path protection fixture")
    git(root, "config", "user.email", "paths@example.invalid")
    git(root, "config", "core.autocrlf", "false")
    git(root, "add", "src")
    git(root, "commit", "-m", "test: establish nested tracked file")
    # This guard reads only the worktree. No admission/recovery state is used.
    return admission.Workflow("", "", 0, 0, os.getpid(), str(root), str(root))


def windows_path_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    encode = os.fsencode

    def native_encode(
        filename: str | bytes | os.PathLike[str] | os.PathLike[bytes],
    ) -> bytes:
        if isinstance(filename, Path):
            return encode(PureWindowsPath(filename))
        return encode(filename)

    # Exercise native Windows Path serialization on every host without replacing
    # actual filesystem operations. Native Windows cases need no emulation.
    monkeypatch.setattr(os, "fsencode", native_encode)


@pytest.mark.parametrize("windows_spelling", [False, True])
def test_checkout_guard_accepts_nested_tracked_files(
    tracked_repository: admission.Workflow,
    monkeypatch: pytest.MonkeyPatch,
    windows_spelling: bool,
) -> None:
    root = Path(tracked_repository.worktree)
    if windows_spelling:
        windows_path_encoding(monkeypatch)
    admission._protect_checkout_paths(tracked_repository, target_tree="HEAD")
    assert (root / "src/nested/Original.py").read_bytes() == b"tracked bytes\n"


@pytest.mark.parametrize("windows_spelling", [False, True])
def test_checkout_guard_accepts_nested_native_case_alias(
    tracked_repository: admission.Workflow,
    monkeypatch: pytest.MonkeyPatch,
    windows_spelling: bool,
) -> None:
    root = Path(tracked_repository.worktree)
    if not (root / "src/nested/original.py").exists():
        pytest.skip("Native filesystem is case-sensitive; no case alias exists")
    git(root, "mv", "src/nested/Original.py", "src/nested/intermediate.py")
    git(root, "mv", "src/nested/intermediate.py", "src/nested/original.py")
    git(root, "commit", "-m", "test: rename tracked case spelling")
    incoming = git(root, "rev-parse", "HEAD").decode("ascii").strip()
    git(root, "switch", "-c", "old-spelling", "HEAD~1")
    if windows_spelling:
        windows_path_encoding(monkeypatch)
    admission._protect_checkout_paths(tracked_repository, target_tree=incoming)
    assert (root / "src/nested/Original.py").read_bytes() == b"tracked bytes\n"


@pytest.mark.parametrize("windows_spelling", [False, True])
@pytest.mark.parametrize("hardlink", [False, True])
def test_checkout_guard_preserves_nested_untracked_destinations(
    tracked_repository: admission.Workflow,
    monkeypatch: pytest.MonkeyPatch,
    windows_spelling: bool,
    hardlink: bool,
) -> None:
    root = Path(tracked_repository.worktree)
    private = root / "src/nested/private.py"
    if hardlink:
        os.link(root / "src/nested/Original.py", private)
    else:
        private.write_bytes(b"private bytes\n")
    expected = private.read_bytes()
    identity = private.stat()
    git(root, "add", "src/nested/private.py")
    git(root, "commit", "-m", "test: record incoming destination")
    incoming = git(root, "rev-parse", "HEAD").decode("ascii").strip()
    git(root, "rm", "--cached", "src/nested/private.py")
    if windows_spelling:
        windows_path_encoding(monkeypatch)
    with pytest.raises(admission.AdmissionError, match="checkout obstruction"):
        admission._protect_checkout_paths(tracked_repository, target_tree=incoming)
    assert private.read_bytes() == expected
    assert (private.stat().st_dev, private.stat().st_ino) == (
        identity.st_dev,
        identity.st_ino,
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX literal filename byte contract")
def test_checkout_guard_preserves_literal_posix_backslash(
    tracked_repository: admission.Workflow,
) -> None:
    root = Path(tracked_repository.worktree)
    relative = Path("src/nested/literal\\name.py")
    (root / relative).write_bytes(b"unusual tracked bytes\n")
    git(root, "add", "src")
    git(root, "commit", "-m", "test: track literal POSIX filename bytes")
    assert os.fsencode(relative) in git(root, "ls-files", "-z").split(b"\0")
    admission._protect_checkout_paths(tracked_repository, target_tree="HEAD")
    assert (root / relative).read_bytes() == b"unusual tracked bytes\n"


@pytest.mark.skipif(os.name != "posix", reason="Raw non-UTF-8 POSIX path bytes")
def test_tracked_lookup_preserves_non_utf8_git_inventory(
    tracked_repository: admission.Workflow,
) -> None:
    root = Path(tracked_repository.worktree)
    raw = b"src/nested/non-utf8-\xff.py"
    blob = git(root, "rev-parse", "HEAD:src/nested/Original.py").removesuffix(b"\n")
    # Git can represent names that the host filesystem cannot materialize.
    # Exercise real raw index bytes, without claiming a native file was created.
    subprocess.run(
        ("git", "-C", str(root), "update-index", "-z", "--index-info"),
        input=b"100644 " + blob + b"\t" + raw + b"\0",
        capture_output=True,
        check=True,
        timeout=10,
    )
    tracked = set(git(root, "ls-files", "-z").split(b"\0"))
    assert raw in tracked
    assert admission._tracked_path(root, Path(os.fsdecode(raw)), tracked)
