"""One-time conversion of verified toolkit 0.1 source bundles to components.

Normal verification and synchronization accept only the current schema. This
explicit converter verifies all legacy hashes before replacing generated files;
it never executes the old payload or adopts local edits.
"""

from __future__ import annotations

import json
from pathlib import Path

from .bundle import replace_bundle
from .bundle_verify import (
    MANIFEST_NAME,
    BundleError,
    BundleManifest,
    _reject_duplicate_keys,
    bundle_directory,
    regular_bytes,
    validate_manifest,
    verify_payload,
)


def _legacy_manifest(repository: Path) -> BundleManifest:
    try:
        document = json.loads(
            regular_bytes(bundle_directory(repository) / MANIFEST_NAME),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise BundleError(f"Invalid legacy manifest: {error}") from error
    if (
        not isinstance(document, dict)
        or set(document)
        != {
            "schema_version",
            "toolkit_name",
            "toolkit_version",
            "payload_sha256",
            "files",
        }
        or type(document["schema_version"]) is not int
        or document["schema_version"] != 1
    ):
        raise BundleError("migrate-v1 requires an unmodified schema-1 bundle")
    document["schema_version"] = 2
    document["components"] = [
        "change-scope",
        "dependencies",
        "documentation",
        "publish-version",
    ]
    return verify_payload(repository, validate_manifest(document))


def migrate_v1(repository: Path) -> bool:
    """Convert once using explicit current pyproject component configuration."""
    _legacy_manifest(repository)
    return replace_bundle(repository, lambda: _legacy_manifest(repository))
