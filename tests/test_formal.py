from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import urllib.request
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

from h2h_dev_tools import formal

_PAYLOAD = b"fixture TLC jar"


def toolchain(root: Path) -> tuple[Path, Path]:
    verification = root / "verification"
    (verification / "tla").mkdir(parents=True)
    lock = verification / "tools.lock.toml"
    lock.write_text(
        '[tla_plus.tlc]\nversion="1.7.4"\nfilename="tlc.jar"\n'
        'url="https://example.invalid/tlc.jar"\n'
        f'sha256="{hashlib.sha256(_PAYLOAD).hexdigest()}"\n'
        f'runtime_image="example:17@sha256:{"a" * 64}"\n'
    )
    jar = root / "tlc.jar"
    jar.write_bytes(_PAYLOAD)
    (verification / "tla/Example.tla").write_text("model fixture")
    (verification / "tla/ExampleSmall.cfg").write_text("small fixture")
    (verification / "tla/ExampleDeep.cfg").write_text("deep fixture")
    return lock, jar


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", "1.8.0"),
        ("filename", "../tlc.jar"),
        ("filename", ".."),
        ("url", "http://example.invalid/tlc.jar"),
        ("sha256", "broken"),
        ("runtime_image", "mutable:tag"),
    ],
)
def test_toolchain_rejects_invalid_lock(tmp_path: Path, field: str, value: str) -> None:
    lock, _ = toolchain(tmp_path)
    lines = [
        f'{field}="{value}"' if line.startswith(field + "=") else line
        for line in lock.read_text().splitlines()
    ]
    lock.write_text("\n".join(lines))
    with pytest.raises(RuntimeError):
        formal.load_tlc_lock(lock, expected_version="1.7.4")


def test_explicit_fetch_verifies_content_before_replacing_and_reuses_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    windows_download_handles: dict[Path, Callable[[], bool]],
) -> None:
    path, _ = toolchain(tmp_path)
    lock = formal.load_tlc_lock(path, expected_version="1.7.4")
    output = tmp_path / "downloads"
    output.mkdir()
    destination = output / "tlc.jar"
    destination.write_bytes(b"previous artifact")
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: BytesIO(b"corrupted download"),
    )
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        formal.fetch_tlc(lock, output)
    assert destination.read_bytes() == b"previous artifact"
    assert list(output.iterdir()) == [destination]
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *_args, **_kwargs: BytesIO(_PAYLOAD)
    )
    assert formal.fetch_tlc(lock, output) == destination
    assert destination.read_bytes() == _PAYLOAD
    assert list(output.iterdir()) == [destination]
    assert len(windows_download_handles) == 2
    assert all(closed() for closed in windows_download_handles.values())

    def forbidden(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("verified cache must never download")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    assert formal.fetch_tlc(lock, output) == destination


@pytest.fixture
def windows_download_handles(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[Path, Callable[[], bool]]:
    """Use real temp files but reject Windows-incompatible open-handle unlink."""
    handles: dict[Path, Callable[[], bool]] = {}
    create = tempfile.NamedTemporaryFile
    unlink = Path.unlink

    def tracked_temporary(**kwargs: Any) -> Any:
        temporary = create(**kwargs)
        handles[Path(temporary.name)] = lambda: bool(temporary.closed)
        return temporary

    def windows_unlink(path: Path, missing_ok: bool = False) -> None:
        closed = handles.get(path)
        if closed is not None and not closed():
            raise PermissionError("Windows cannot unlink the open download handle")
        unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", tracked_temporary)
    monkeypatch.setattr(Path, "unlink", windows_unlink)
    return handles


class DownloadCancelled(BaseException):
    """Cancellation must close and remove a download just like an I/O error."""


@pytest.mark.parametrize("phase", ["urlopen", "copy"])
@pytest.mark.parametrize("error_type", [OSError, DownloadCancelled])
def test_failed_download_closes_before_unlink_and_preserves_original_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    windows_download_handles: dict[Path, Callable[[], bool]],
    phase: str,
    error_type: type[BaseException],
) -> None:
    path, _ = toolchain(tmp_path)
    lock = formal.load_tlc_lock(path, expected_version="1.7.4")
    output = tmp_path / "downloads"
    output.mkdir()
    destination = output / lock.filename
    destination.write_bytes(b"previous artifact")
    original_failure = error_type("interrupted fixture download")
    response = BytesIO(_PAYLOAD)

    def open_response(*_args: object, **_kwargs: object) -> BytesIO:
        if phase == "urlopen":
            raise original_failure
        return response

    def failed_copy(source: Any, destination_stream: Any) -> None:
        destination_stream.write(source.read(3))
        raise original_failure

    monkeypatch.setattr(urllib.request, "urlopen", open_response)
    monkeypatch.setattr(shutil, "copyfileobj", failed_copy)
    with pytest.raises(error_type) as caught:
        formal.fetch_tlc(lock, output)
    assert caught.value is original_failure
    assert destination.read_bytes() == b"previous artifact"
    assert len(windows_download_handles) == 1
    assert all(closed() for closed in windows_download_handles.values())
    assert list(output.iterdir()) == [destination]
    if phase == "copy":
        assert response.closed


def test_failed_replacement_closes_and_removes_verified_temporary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    windows_download_handles: dict[Path, Callable[[], bool]],
) -> None:
    path, _ = toolchain(tmp_path)
    lock = formal.load_tlc_lock(path, expected_version="1.7.4")
    output = tmp_path / "downloads"
    output.mkdir()
    destination = output / lock.filename
    destination.write_bytes(b"previous artifact")
    original_failure = OSError("fixture replace failure")
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *_args, **_kwargs: BytesIO(_PAYLOAD)
    )

    def fail_replace(source: Path, target: Path) -> None:
        assert source.read_bytes() == _PAYLOAD
        assert windows_download_handles[source]()
        assert target == destination
        raise original_failure

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError) as caught:
        formal.fetch_tlc(lock, output)
    assert caught.value is original_failure
    assert destination.read_bytes() == b"previous artifact"
    assert len(windows_download_handles) == 1
    assert all(closed() for closed in windows_download_handles.values())
    assert list(output.iterdir()) == [destination]


@pytest.mark.parametrize("runtime", ["host", "docker"])
def test_verification_is_offline_and_only_runs_selected_finite_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime: formal.TlaRuntime
) -> None:
    _, jar = toolchain(tmp_path)
    calls: list[tuple[list[str], Path]] = []
    monkeypatch.setattr(formal, "host_java_available", lambda: True)
    monkeypatch.setattr(shutil, "which", lambda _name: "/fixture/executable")
    monkeypatch.setattr(
        formal, "_run", lambda command, *, cwd: calls.append((command, cwd))
    )
    formal.verify_tla(
        tmp_path, jar, profile="Small", runtime=runtime, expected_version="1.7.4"
    )
    assert len(calls) == 1
    command, cwd = calls[0]
    assert command[-2:] == ["ExampleSmall.cfg", "Example.tla"]
    if runtime == "host":
        assert cwd == tmp_path / "verification/tla"
        state = Path(command[command.index("-metadir") + 1])
        assert not state.parent.exists()
    else:
        assert "--pull=never" in command
        assert command[command.index("--network") + 1] == "none"
        assert "--read-only" in command
        assert cwd == tmp_path
    jar.write_bytes(b"corrupt local jar")
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        formal.verify_tla(
            tmp_path, jar, profile="Small", runtime=runtime, expected_version="1.7.4"
        )
    assert len(calls) == 1
