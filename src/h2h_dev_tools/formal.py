"""Checksum-pinned TLC acquisition and offline host/container execution."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

TlaRuntime = Literal["auto", "host", "docker"]


@dataclass(frozen=True)
class TlcLock:
    filename: str
    url: str
    sha256: str
    runtime_image: str


def load_tlc_lock(path: Path, *, expected_version: str) -> TlcLock:
    with path.open("rb") as stream:
        document = tomllib.load(stream)
    value = document.get("tla_plus", {}).get("tlc")
    if not isinstance(value, dict):
        raise RuntimeError(f"Missing [tla_plus.tlc] in {path}")
    if value.get("version") != expected_version:
        raise RuntimeError(f"Unsupported tla_plus.tlc.version in {path}")
    for field in ("filename", "url", "sha256", "runtime_image"):
        if not isinstance(value.get(field), str) or not value[field]:
            raise RuntimeError(f"Invalid tla_plus.tlc.{field} in {path}")
    filename, url, digest, image = (
        str(value[field]) for field in ("filename", "url", "sha256", "runtime_image")
    )
    if Path(filename).name != filename or filename in {".", ".."}:
        raise RuntimeError(f"tla_plus.tlc.filename in {path} must be a basename")
    if not url.startswith("https://"):
        raise RuntimeError(f"tla_plus.tlc.url in {path} must use HTTPS")
    if re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None:
        raise RuntimeError(f"tla_plus.tlc.sha256 in {path} is invalid")
    if re.fullmatch(r"[^@]+@sha256:[0-9a-f]{64}", image) is None:
        raise RuntimeError(
            f"tla_plus.tlc.runtime_image in {path} must be digest-pinned"
        )
    return TlcLock(filename, url, digest.lower(), image)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_jar(path: Path, expected: str) -> None:
    actual = sha256(path)
    if actual != expected:
        raise RuntimeError(
            f"TLC checksum mismatch: expected={expected} actual={actual}"
        )


def fetch_tlc(lock: TlcLock, output_directory: Path) -> Path:
    """Explicit online acquisition; verification never calls this function."""
    output_directory.mkdir(parents=True, exist_ok=True)
    destination = output_directory / lock.filename
    if destination.is_file() and sha256(destination) == lock.sha256:
        return destination
    with tempfile.NamedTemporaryFile(
        dir=output_directory, prefix=".tlc-", delete=False
    ) as temporary:
        temporary_path = Path(temporary.name)
        try:
            with urllib.request.urlopen(lock.url, timeout=60) as response:
                shutil.copyfileobj(response, temporary)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise
    try:
        verify_jar(temporary_path, lock.sha256)
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)
    return destination


def fetch_main(repository_root: Path, *, expected_version: str) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-directory", type=Path, default=repository_root / ".formal-tools"
    )
    arguments = parser.parse_args()
    lock = load_tlc_lock(
        repository_root / "verification/tools.lock.toml",
        expected_version=expected_version,
    )
    print(fetch_tlc(lock, arguments.output_directory))


def host_java_available() -> bool:
    java = shutil.which("java")
    if java is None:
        return False
    result = subprocess.run(
        [java, "-version"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def _run(command: list[str], *, cwd: Path) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def verify_tla(
    repository_root: Path,
    tla_jar: Path,
    *,
    profile: str,
    runtime: TlaRuntime,
    expected_version: str,
) -> None:
    """Execute the explicitly selected finite model profile, without downloading."""
    repository_root = repository_root.resolve()
    if not tla_jar.is_file():
        raise FileNotFoundError(f"TLC JAR does not exist: {tla_jar}")
    lock = load_tlc_lock(
        repository_root / "verification/tools.lock.toml",
        expected_version=expected_version,
    )
    verify_jar(tla_jar, lock.sha256)
    configs = sorted((repository_root / "verification/tla").glob(f"*{profile}.cfg"))
    if not configs:
        raise RuntimeError("No TLA+ model configurations were found")
    use_host_java = runtime == "host" or (runtime == "auto" and host_java_available())
    if runtime == "host" and not host_java_available():
        raise RuntimeError("The requested host Java runtime is not available")
    if not use_host_java and shutil.which("docker") is None:
        raise RuntimeError("Neither a working Java runtime nor Docker is available")
    for config in configs:
        module_stem = config.name.removesuffix(f"{profile}.cfg")
        if not module_stem:
            raise RuntimeError(f"Cannot infer a TLA+ module from {config.name}")
        module = config.with_name(f"{module_stem}.tla")
        if not module.is_file():
            raise RuntimeError(f"Missing TLA+ module for {config.name}")
        with tempfile.TemporaryDirectory(prefix="h2h-tlc-") as state_directory:
            if use_host_java:
                command = [
                    "java",
                    "-XX:+UseParallelGC",
                    "-Xmx4g",
                    "-cp",
                    str(tla_jar.resolve()),
                    "tlc2.TLC",
                    "-workers",
                    "auto",
                    "-metadir",
                    str(Path(state_directory) / config.stem),
                    "-config",
                    config.name,
                    module.name,
                ]
                _run(command, cwd=config.parent)
            else:
                command = [
                    "docker",
                    "run",
                    "--rm",
                    "--pull=never",
                    "--network",
                    "none",
                    "--read-only",
                    "--tmpfs",
                    "/tmp:rw,nosuid,nodev,size=512m",
                    "--mount",
                    f"type=bind,src={repository_root},dst=/workspace,readonly",
                    "--mount",
                    f"type=bind,src={tla_jar.resolve()},dst=/tools/tla2tools.jar,readonly",
                    "--workdir",
                    "/workspace/verification/tla",
                    lock.runtime_image,
                    "java",
                    "-XX:+UseParallelGC",
                    "-Xmx4g",
                    "-cp",
                    "/tools/tla2tools.jar",
                    "tlc2.TLC",
                    "-workers",
                    "auto",
                    "-metadir",
                    f"/tmp/tlc/{config.stem}",
                    "-config",
                    config.name,
                    module.name,
                ]
                _run(command, cwd=repository_root)
