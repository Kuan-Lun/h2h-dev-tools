"""Export reviewed toolkit source into a consumer's committed Git tree."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from email.parser import BytesParser
from importlib import metadata
from pathlib import Path

from packaging.utils import (
    InvalidWheelFilename,
    canonicalize_name,
    parse_wheel_filename,
)

from .bundle_verify import (
    MANIFEST_NAME,
    BundleError,
    BundleManifest,
    _manifest_fields,
    _reject_duplicate_keys,
    bundle_directory,
    cache_files,
    component_files,
    payload_digest,
    regular_bytes,
    repository_components,
    validate_manifest,
    verify_bundle,
    verify_payload,
)

# Ownership anchors identify the historical snapshot format, not the current
# dependency closure: a new base helper must not make old wheel upgrades unsafe.
_OWNERSHIP_ANCHORS = frozenset({"__init__.py", "bundle_verify.py"})


def _source_payload(components: list[str]) -> tuple[dict[str, bytes], BundleManifest]:
    try:
        version = metadata.version("h2h-dev-tools")
    except metadata.PackageNotFoundError as error:
        raise BundleError(
            "Install h2h-dev-tools before exporting its source bundle"
        ) from error
    source = Path(__file__).parent
    payload = {
        name: regular_bytes(source / name)
        for name in sorted(component_files(components))
    }
    hashes = {
        name: hashlib.sha256(content).hexdigest() for name, content in payload.items()
    }
    manifest: BundleManifest = {
        "schema_version": 2,
        "components": components,
        "toolkit_name": "h2h-dev-tools",
        "toolkit_version": version,
        "payload_sha256": payload_digest(hashes),
        "files": hashes,
    }
    return payload, validate_manifest(manifest)


@dataclass(frozen=True)
class _PreviousSource:
    version: str
    files: dict[str, str]


def _previous_source(wheel: Path) -> _PreviousSource:
    """Read explicit wheel data without importing or executing its implementation."""
    try:
        name, version, _build, _tags = parse_wheel_filename(wheel.name)
        if name != "h2h-dev-tools":
            raise BundleError("Previous wheel must contain h2h-dev-tools")
        with zipfile.ZipFile(io.BytesIO(regular_bytes(wheel))) as archive:
            entries = archive.infolist()
            names = [item.filename for item in entries]
            if len(names) != len(set(names)):
                raise BundleError("Previous wheel has duplicate archive entries")
            metadata_name = f"h2h_dev_tools-{version}.dist-info/METADATA"
            if [item for item in names if item.endswith(".dist-info/METADATA")] != [
                metadata_name
            ]:
                raise BundleError("Previous wheel has ambiguous distribution metadata")
            document = BytesParser().parsebytes(archive.read(metadata_name))
            if (
                len(document.get_all("Name", [])) != 1
                or len(document.get_all("Version", [])) != 1
                or canonicalize_name(str(document["Name"])) != name
                or str(document["Version"]) != str(version)
            ):
                raise BundleError("Previous wheel distribution metadata does not match")
            files: dict[str, str] = {}
            for entry in entries:
                prefix = "h2h_dev_tools/"
                if not entry.filename.startswith(prefix) or entry.is_dir():
                    continue
                filename = entry.filename.removeprefix(prefix)
                if (
                    re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", filename) is None
                    or filename.endswith(".")
                    or filename == MANIFEST_NAME
                ):
                    raise BundleError(
                        "Previous wheel source paths must be flat filenames"
                    )
                mode = entry.external_attr >> 16
                if stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                    raise BundleError(
                        "Previous wheel source must contain regular files"
                    )
                files[filename] = hashlib.sha256(archive.read(entry)).hexdigest()
            if not _OWNERSHIP_ANCHORS <= files.keys():
                raise BundleError(
                    "Previous wheel is missing the source bundle base files"
                )
            return _PreviousSource(str(version), files)
    except BundleError:
        raise
    except (
        InvalidWheelFilename,
        OSError,
        ValueError,
        zipfile.BadZipFile,
        RuntimeError,
        NotImplementedError,
    ) as error:
        raise BundleError(f"Cannot read previous source wheel: {error}") from error


def _verify_previous_bundle(
    repository: Path, source: _PreviousSource
) -> BundleManifest:
    """Prove replacement ownership, without claiming an old dependency graph passed."""
    try:
        document = json.loads(
            regular_bytes(bundle_directory(repository) / MANIFEST_NAME),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise BundleError(f"Invalid previous manifest: {error}") from error
    manifest = _manifest_fields(document)
    if manifest["toolkit_version"] != source.version:
        raise BundleError("Previous wheel version does not match the existing bundle")
    if not _OWNERSHIP_ANCHORS <= manifest["files"].keys() or any(
        source.files.get(name) != digest for name, digest in manifest["files"].items()
    ):
        raise BundleError(
            "Existing manifest is not owned by the supplied previous wheel"
        )
    return verify_payload(repository, manifest)


def sync_bundle(repository: Path, *, previous_wheel: Path | None = None) -> bool:
    """Replace only a verified generated file set; return whether bytes changed."""
    if previous_wheel is None:
        return replace_bundle(
            repository, lambda: verify_bundle(repository, check_selection=False)
        )
    source = _previous_source(previous_wheel)
    return replace_bundle(
        repository, lambda: _verify_previous_bundle(repository, source)
    )


def replace_bundle(
    repository: Path, verify_existing: Callable[[], BundleManifest]
) -> bool:
    """Stage a replacement after verifying ownership, then recheck before writing."""
    directory = bundle_directory(repository)
    old_manifest: BundleManifest | None = None
    try:
        if directory.exists() and any(directory.iterdir()):
            old_manifest = verify_existing()
        payload, manifest = _source_payload(repository_components(repository))
        if old_manifest == manifest:
            return False
        payload[MANIFEST_NAME] = (
            json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".h2h-dev-tools-", dir=directory.parent
        ) as temporary:
            staging = Path(temporary)
            for name, content in payload.items():
                (staging / name).write_bytes(content)
            # Recheck existing authority after preparing the replacement payload.
            if old_manifest is not None:
                if verify_existing() != old_manifest:
                    raise BundleError("Bundle changed while preparing its update")
            elif any(directory.iterdir()):
                raise BundleError("Initial bundle directory is no longer empty")
            bundle_directory(repository)
            for cache in cache_files(
                directory,
                frozenset(old_manifest["files"]) if old_manifest else frozenset(),
            ):
                cache.unlink()
            cache_directory = directory / "__pycache__"
            if cache_directory.exists():
                cache_directory.rmdir()
            for name in sorted(manifest["files"]):
                os.replace(staging / name, directory / name)
            if old_manifest is not None:
                for name in sorted(set(old_manifest["files"]) - set(manifest["files"])):
                    (directory / name).unlink()
            os.replace(staging / MANIFEST_NAME, directory / MANIFEST_NAME)
        verify_bundle(repository)
    except OSError as error:
        raise BundleError(f"Cannot synchronize source bundle: {error}") from error
    return True
