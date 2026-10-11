"""Publish complete byte payloads through an atomic filesystem name operation."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def publish_bytes(path: Path, content: bytes, *, overwrite: bool = False) -> None:
    """Publish in an existing parent, preserving other writers unless requested.

    The temporary file is flushed and fsynced before publication. This provides
    atomic visibility, not a directory-fsync or power-loss durability contract.
    Callers own parent creation, serialization, target policy and permissions.
    """
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
