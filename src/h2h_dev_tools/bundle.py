"""Export reviewed toolkit source into a consumer's committed Git tree."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from importlib import metadata
from pathlib import Path

from .bundle_verify import (
    BUNDLE_FILES,
    MANIFEST_NAME,
    BundleError,
    BundleManifest,
    bundle_directory,
    cache_files,
    payload_digest,
    regular_bytes,
    validate_manifest,
    verify_bundle,
)


def _source_payload() -> tuple[dict[str, bytes], BundleManifest]:
    try:
        version = metadata.version("h2h-dev-tools")
    except metadata.PackageNotFoundError as error:
        raise BundleError(
            "Install h2h-dev-tools before exporting its source bundle"
        ) from error
    source = Path(__file__).parent
    payload = {name: regular_bytes(source / name) for name in sorted(BUNDLE_FILES)}
    hashes = {
        name: hashlib.sha256(content).hexdigest() for name, content in payload.items()
    }
    manifest: BundleManifest = {
        "schema_version": 1,
        "toolkit_name": "h2h-dev-tools",
        "toolkit_version": version,
        "payload_sha256": payload_digest(hashes),
        "files": hashes,
    }
    return payload, validate_manifest(manifest)


def sync_bundle(repository: Path) -> bool:
    """Replace only a verified generated file set; return whether bytes changed."""
    directory = bundle_directory(repository)
    old_manifest: BundleManifest | None = None
    try:
        if directory.exists() and any(directory.iterdir()):
            old_manifest = verify_bundle(repository)
        payload, manifest = _source_payload()
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
                if verify_bundle(repository) != old_manifest:
                    raise BundleError("Bundle changed while preparing its update")
            elif any(directory.iterdir()):
                raise BundleError("Initial bundle directory is no longer empty")
            bundle_directory(repository)
            for cache in cache_files(directory):
                cache.unlink()
            cache_directory = directory / "__pycache__"
            if cache_directory.exists():
                cache_directory.rmdir()
            for name in sorted(BUNDLE_FILES):
                os.replace(staging / name, directory / name)
            os.replace(staging / MANIFEST_NAME, directory / MANIFEST_NAME)
        verify_bundle(repository)
    except OSError as error:
        raise BundleError(f"Cannot synchronize source bundle: {error}") from error
    return True
