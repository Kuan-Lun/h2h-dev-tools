from __future__ import annotations

import os
from pathlib import Path

import pytest

from h2h_dev_tools import atomic_files


def test_publish_complete_bytes_and_explicit_replacement(tmp_path: Path) -> None:
    destination = tmp_path / "report"
    atomic_files.publish_bytes(destination, b"\x00\xfforiginal")
    assert destination.read_bytes() == b"\x00\xfforiginal"
    atomic_files.publish_bytes(destination, b"replacement", overwrite=True)
    assert destination.read_bytes() == b"replacement"
    assert list(tmp_path.iterdir()) == [destination]


@pytest.mark.parametrize("operation", ["fsync", "replace"])
@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt])
def test_interrupted_publication_preserves_previous_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    failure: type[BaseException],
) -> None:
    destination = tmp_path / "report"
    destination.write_bytes(b"previous")

    def fail(*_args: object) -> None:
        raise failure("interrupted publication")

    monkeypatch.setattr(os, operation, fail)
    with pytest.raises(failure, match="interrupted publication"):
        atomic_files.publish_bytes(destination, b"candidate", overwrite=True)
    assert destination.read_bytes() == b"previous"
    assert list(tmp_path.iterdir()) == [destination]


def test_no_replace_preserves_a_writer_racing_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "report"
    original_link = os.link

    def race(source: Path, target: Path) -> None:
        target.write_bytes(b"other writer")
        original_link(source, target)

    monkeypatch.setattr(os, "link", race)
    with pytest.raises(FileExistsError):
        atomic_files.publish_bytes(destination, b"candidate")
    assert destination.read_bytes() == b"other writer"
    assert list(tmp_path.iterdir()) == [destination]


def test_no_replace_preserves_dangling_symlink(tmp_path: Path) -> None:
    destination = tmp_path / "report"
    target = tmp_path / "missing"
    destination.symlink_to(target)
    with pytest.raises(FileExistsError):
        atomic_files.publish_bytes(destination, b"candidate")
    assert destination.is_symlink()
    assert not target.exists()
    assert list(tmp_path.iterdir()) == [destination]


def test_parent_creation_belongs_to_caller(tmp_path: Path) -> None:
    directory = tmp_path / "not-created"
    with pytest.raises(FileNotFoundError):
        atomic_files.publish_bytes(directory / "report", b"payload")
    assert not directory.exists()
