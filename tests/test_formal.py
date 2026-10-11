from __future__ import annotations

import hashlib
import shutil
import urllib.request
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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

    def forbidden(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("verified cache must never download")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    assert formal.fetch_tlc(lock, output) == destination


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
