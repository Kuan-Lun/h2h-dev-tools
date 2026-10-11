"""Source updates never adopt unknown or modified consumer files."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import zipfile
from importlib import metadata
from pathlib import Path

import pytest

from h2h_dev_tools import bundle, bundle_verify
from h2h_dev_tools.__main__ import main
from h2h_dev_tools.bundle_verify import (
    BUNDLE_DIRECTORY,
    MANIFEST_NAME,
    BundleError,
    BundleManifest,
    component_files,
    payload_digest,
    verify_bundle,
)

COMPONENTS = ["change-scope", "dependencies", "documentation", "publish-version"]
BUNDLE_FILES = component_files(COMPONENTS)


def configure(root: Path, components: list[str] = COMPONENTS) -> None:
    (root / "pyproject.toml").write_text(
        "[tool.h2h-dev-tools]\ncomponents = " + json.dumps(components) + "\n"
    )


def test_component_selection_removes_only_verified_generated_files(
    consumer: Path,
) -> None:
    configure(consumer, ["documentation"])
    with pytest.raises(BundleError, match="selection is stale"):
        verify_bundle(consumer)
    assert bundle.sync_bundle(consumer)
    manifest = verify_bundle(consumer)
    assert set(manifest["files"]) == {
        "__init__.py",
        "bundle_verify.py",
        "documentation.py",
        "change_scope.py",
    }
    assert not (consumer / BUNDLE_DIRECTORY / "publish_version.py").exists()


@pytest.mark.parametrize(
    "components", [["unknown"], ["documentation", "documentation"]]
)
def test_invalid_component_selection_never_rewrites_existing_bundle(
    consumer: Path, components: list[str]
) -> None:
    configure(consumer, components)
    before = _snapshot(consumer)
    with pytest.raises(BundleError):
        bundle.sync_bundle(consumer)
    assert _snapshot(consumer) == before


def test_missing_explicit_selection_fails_before_initial_write(tmp_path: Path) -> None:
    with pytest.raises(BundleError):
        bundle.sync_bundle(tmp_path)
    assert not any(tmp_path.iterdir())


def test_v1_conversion_is_explicit_and_preserves_unmanaged_content(
    consumer: Path,
) -> None:
    from h2h_dev_tools.migrate_v1 import migrate_v1

    path = consumer / BUNDLE_DIRECTORY / MANIFEST_NAME
    legacy = json.loads(path.read_bytes())
    legacy["schema_version"] = 1
    del legacy["components"]
    path.write_text(json.dumps(legacy))
    local = consumer / "scripts/private.py"
    local.write_text("keep this unrelated tool\n")
    configure(consumer, ["documentation"])
    with pytest.raises(BundleError, match="schema"):
        bundle.sync_bundle(consumer)
    assert migrate_v1(consumer)
    assert verify_bundle(consumer)["components"] == ["documentation"]
    assert local.read_text() == "keep this unrelated tool\n"
    with pytest.raises(BundleError, match="schema-1"):
        migrate_v1(consumer)


def test_v1_conversion_rejects_modified_source_without_writing(consumer: Path) -> None:
    from h2h_dev_tools.migrate_v1 import migrate_v1

    path = consumer / BUNDLE_DIRECTORY / MANIFEST_NAME
    legacy = json.loads(path.read_bytes())
    legacy["schema_version"] = 1
    del legacy["components"]
    path.write_text(json.dumps(legacy))
    (path.parent / "dependency_manifest.py").write_text("private edits\n")
    before = _snapshot(consumer)
    with pytest.raises(BundleError, match="modified"):
        migrate_v1(consumer)
    assert _snapshot(consumer) == before


@pytest.fixture
def source_payload(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    source = Path(bundle.__file__).parent
    payload = dict.fromkeys(BUNDLE_FILES, b'"""Generated fixture module."""\n')
    payload["bundle_verify.py"] = (source / "bundle_verify.py").read_bytes()

    def load(components: list[str]) -> tuple[dict[str, bytes], BundleManifest]:
        selected = {name: payload[name] for name in component_files(components)}
        files = {
            name: hashlib.sha256(content).hexdigest()
            for name, content in selected.items()
        }
        manifest: BundleManifest = {
            "schema_version": 2,
            "components": components,
            "toolkit_name": "h2h-dev-tools",
            "toolkit_version": "0.1.0",
            "payload_sha256": payload_digest(files),
            "files": files,
        }
        return selected, manifest

    monkeypatch.setattr(bundle, "_source_payload", load)
    return payload


@pytest.fixture
def consumer(tmp_path: Path, source_payload: dict[str, bytes]) -> Path:
    del source_payload
    configure(tmp_path)
    assert bundle.sync_bundle(tmp_path)
    return tmp_path


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def test_initial_sync_preserves_unmanaged_files_and_is_idempotent(
    consumer: Path,
) -> None:
    script = consumer / "scripts" / "local.py"
    script.write_text("untouched\n")
    before = _snapshot(consumer)
    timestamps = {path: path.stat().st_mtime_ns for path in consumer.rglob("*.py")}
    assert not bundle.sync_bundle(consumer)
    assert _snapshot(consumer) == before
    assert {path: path.stat().st_mtime_ns for path in timestamps} == timestamps
    manifest = verify_bundle(consumer)
    assert manifest["toolkit_version"] == "0.1.0"
    assert set(manifest["files"]) == BUNDLE_FILES


def test_update_accepts_verified_older_bundle(
    consumer: Path, source_payload: dict[str, bytes]
) -> None:
    directory = consumer / BUNDLE_DIRECTORY
    manifest_path = directory / MANIFEST_NAME
    old = json.loads(manifest_path.read_bytes())
    old["toolkit_version"] = "0.0.1"
    manifest_path.write_text(json.dumps(old))
    source_payload["dependency_manifest.py"] = b'"""Updated implementation."""\n'
    assert bundle.sync_bundle(consumer)
    current = verify_bundle(consumer)
    assert current["toolkit_version"] == "0.1.0"
    assert current["payload_sha256"] != old["payload_sha256"]
    assert (directory / "dependency_manifest.py").read_bytes() == source_payload[
        "dependency_manifest.py"
    ]


def _write_previous_wheel(root: Path, files: dict[str, bytes]) -> Path:
    wheel = root / "h2h_dev_tools-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in files.items():
            archive.writestr(f"h2h_dev_tools/{name}", content)
        archive.writestr(
            "h2h_dev_tools-0.1.0.dist-info/METADATA",
            "Metadata-Version: 2.4\nName: h2h-dev-tools\nVersion: 0.1.0\n",
        )
    return wheel


@pytest.fixture
def previous_wheel(
    consumer: Path,
    source_payload: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    wheel = _write_previous_wheel(consumer, source_payload)
    monkeypatch.setitem(
        bundle_verify.COMPONENT_FILES,
        "documentation",
        bundle_verify.COMPONENT_FILES["documentation"] | {"new_helper.py"},
    )
    source_payload["new_helper.py"] = b'"""New dependency."""\n'
    return wheel


def test_closure_upgrade_requires_explicit_previous_source_authority(
    consumer: Path, previous_wheel: Path
) -> None:
    unrelated = consumer / "scripts/private.py"
    unrelated.write_text("keep private edits\n")
    before = _snapshot(consumer)
    for operation in (verify_bundle, bundle.sync_bundle):
        with pytest.raises(BundleError, match="complete source file set"):
            operation(consumer)
        assert _snapshot(consumer) == before
    assert bundle.sync_bundle(consumer, previous_wheel=previous_wheel)
    manifest = verify_bundle(consumer)
    assert "new_helper.py" in manifest["files"]
    assert unrelated.read_text() == "keep private edits\n"
    assert not bundle.sync_bundle(consumer)


@pytest.mark.parametrize(
    "corruption",
    [
        "changed-source",
        "self-rehashed-source",
        "extra-file",
        "adopted-private-file",
        "missing-file",
        "omitted-existing-file",
        "source-symlink",
        "manifest-symlink",
        "wrong-version",
        "wrong-wheel-source",
        "missing-base",
    ],
)
def test_previous_wheel_upgrade_refuses_unproven_ownership_without_writing(
    consumer: Path, previous_wheel: Path, corruption: str
) -> None:
    directory = consumer / BUNDLE_DIRECTORY
    path = directory / MANIFEST_NAME
    manifest = json.loads(path.read_bytes())
    target = directory / "dependency_manifest.py"
    if corruption in {"changed-source", "self-rehashed-source"}:
        target.write_text("keep private changes\n")
        if corruption == "self-rehashed-source":
            manifest["files"][target.name] = hashlib.sha256(
                target.read_bytes()
            ).hexdigest()
    elif corruption in {"extra-file", "adopted-private-file"}:
        private = directory / "private.py"
        private.write_text("keep private module\n")
        if corruption == "adopted-private-file":
            manifest["files"][private.name] = hashlib.sha256(
                private.read_bytes()
            ).hexdigest()
    elif corruption == "missing-file":
        target.unlink()
    elif corruption == "omitted-existing-file":
        del manifest["files"][target.name]
    elif corruption == "source-symlink":
        target.unlink()
        target.symlink_to(directory / "__init__.py")
    elif corruption == "manifest-symlink":
        saved = consumer / "saved-manifest.json"
        saved.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(saved)
    elif corruption == "wrong-version":
        manifest["toolkit_version"] = "0.0.1"
    elif corruption == "wrong-wheel-source":
        files = {name: (directory / name).read_bytes() for name in manifest["files"]}
        files[target.name] = b"source from another build\n"
        _write_previous_wheel(consumer, files)
    else:
        del manifest["files"]["__init__.py"]
        (directory / "__init__.py").unlink()
    if corruption != "manifest-symlink":
        manifest["payload_sha256"] = payload_digest(manifest["files"])
        path.write_text(json.dumps(manifest))
    before = _snapshot(consumer)
    with pytest.raises(BundleError):
        bundle.sync_bundle(consumer, previous_wheel=previous_wheel)
    assert _snapshot(consumer) == before


def test_explicit_source_upgrade_can_repair_an_omitted_absent_dependency(
    consumer: Path, previous_wheel: Path
) -> None:
    directory = consumer / BUNDLE_DIRECTORY
    path = directory / MANIFEST_NAME
    manifest = json.loads(path.read_bytes())
    del manifest["files"]["dependency_manifest.py"]
    (directory / "dependency_manifest.py").unlink()
    manifest["payload_sha256"] = payload_digest(manifest["files"])
    path.write_text(json.dumps(manifest))
    with pytest.raises(BundleError, match="complete source file set"):
        verify_bundle(consumer)
    assert bundle.sync_bundle(consumer, previous_wheel=previous_wheel)
    assert "dependency_manifest.py" in verify_bundle(consumer)["files"]


def test_previous_wheel_is_data_and_is_never_imported(
    consumer: Path,
    previous_wheel: Path,
    source_payload: dict[str, bytes],
) -> None:
    directory = consumer / BUNDLE_DIRECTORY
    path = directory / MANIFEST_NAME
    manifest = json.loads(path.read_bytes())
    bad_python = b"raise RuntimeError('previous code must not run')\n"
    (directory / "bundle_verify.py").write_bytes(bad_python)
    manifest["files"]["bundle_verify.py"] = hashlib.sha256(bad_python).hexdigest()
    manifest["payload_sha256"] = payload_digest(manifest["files"])
    path.write_text(json.dumps(manifest))
    old_files = {name: (directory / name).read_bytes() for name in manifest["files"]}
    _write_previous_wheel(consumer, old_files)
    assert bundle.sync_bundle(consumer, previous_wheel=previous_wheel)
    assert (directory / "bundle_verify.py").read_bytes() == source_payload[
        "bundle_verify.py"
    ]
    verify_bundle(consumer)


def test_previous_source_is_rechecked_after_staging(
    consumer: Path, previous_wheel: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_write = Path.write_bytes
    target = consumer / BUNDLE_DIRECTORY / "dependency_manifest.py"

    def change_source(path: Path, content: bytes) -> int:
        count = original_write(path, content)
        if path.name == MANIFEST_NAME:
            original_write(target, b"concurrent private change\n")
        return count

    monkeypatch.setattr(Path, "write_bytes", change_source)
    with pytest.raises(BundleError, match="modified"):
        bundle.sync_bundle(consumer, previous_wheel=previous_wheel)
    assert target.read_bytes() == b"concurrent private change\n"


@pytest.mark.parametrize(
    "damage",
    [
        "invalid-archive",
        "wrong-name",
        "extra-metadata",
        "duplicate-entry",
        "../private.py",
        "nested/private.py",
        "C:private.py",
        "private.py.",
        "source-symlink",
    ],
)
def test_previous_wheel_archive_must_have_unambiguous_safe_sources(
    consumer: Path, previous_wheel: Path, damage: str
) -> None:
    if damage == "invalid-archive":
        previous_wheel.write_bytes(b"not a wheel archive")
    elif damage == "wrong-name":
        replacement = consumer / "other-0.1.0-py3-none-any.whl"
        previous_wheel.rename(replacement)
        previous_wheel = replacement
    else:
        with zipfile.ZipFile(previous_wheel, "a") as archive:
            if damage == "extra-metadata":
                archive.writestr("other-0.1.0.dist-info/METADATA", "Name: other\n")
            elif damage == "duplicate-entry":
                with pytest.warns(UserWarning, match="Duplicate name"):
                    archive.writestr("h2h_dev_tools/__init__.py", "duplicate\n")
            elif damage == "source-symlink":
                entry = zipfile.ZipInfo("h2h_dev_tools/symlink.py")
                entry.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(entry, "../private.py")
            else:
                archive.writestr(f"h2h_dev_tools/{damage}", "unsafe path\n")
    before = _snapshot(consumer)
    with pytest.raises(BundleError):
        bundle.sync_bundle(consumer, previous_wheel=previous_wheel)
    assert _snapshot(consumer) == before


def test_cli_accepts_previous_wheel_only_for_explicit_sync(
    consumer: Path, previous_wheel: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(
            [
                "sync",
                "--repository",
                str(consumer),
                "--previous-wheel",
                str(previous_wheel),
            ]
        )
        == 0
    )
    assert "synchronized" in capsys.readouterr().out
    assert main(["verify", "--repository", str(consumer)]) == 0
    with pytest.raises(SystemExit):
        main(
            [
                "verify",
                "--repository",
                str(consumer),
                "--previous-wheel",
                str(previous_wheel),
            ]
        )


@pytest.mark.parametrize("operation", [bundle.sync_bundle, verify_bundle])
@pytest.mark.parametrize(
    "corruption",
    ["modified", "missing", "extra", "directory", "symlink", "manifest-symlink"],
)
def test_corrupt_existing_bundle_fails_without_replacing_files(
    consumer: Path, operation: object, corruption: str
) -> None:
    directory = consumer / BUNDLE_DIRECTORY
    target = directory / "dependency_manifest.py"
    if corruption == "modified":
        target.write_text("private changes\n")
    elif corruption == "missing":
        target.unlink()
    elif corruption == "extra":
        (directory / "private.py").write_text("user-owned\n")
    elif corruption == "directory":
        target.unlink()
        target.mkdir()
    elif corruption == "symlink":
        target.unlink()
        target.symlink_to(directory / "__init__.py")
    else:
        manifest = directory / MANIFEST_NAME
        saved = consumer / "saved-manifest.json"
        saved.write_bytes(manifest.read_bytes())
        manifest.unlink()
        manifest.symlink_to(saved)
    before = _snapshot(consumer)
    assert callable(operation)
    with pytest.raises(BundleError):
        operation(consumer)
    assert _snapshot(consumer) == before


@pytest.mark.parametrize("component", ["scripts", "_shared", "h2h_dev_tools"])
def test_sync_refuses_symlink_managed_ancestor(
    tmp_path: Path, source_payload: dict[str, bytes], component: str
) -> None:
    del source_payload
    external = tmp_path / "elsewhere"
    external.mkdir()
    root = tmp_path / "consumer"
    root.mkdir()
    path = root
    for name in BUNDLE_DIRECTORY.parts:
        path /= name
        if name == component:
            path.symlink_to(external, target_is_directory=True)
            break
        path.mkdir()
    with pytest.raises(BundleError, match="real directory"):
        bundle.sync_bundle(root)
    assert not any(external.iterdir())


def test_sync_refuses_symlink_repository(
    tmp_path: Path, source_payload: dict[str, bytes]
) -> None:
    del source_payload
    root = tmp_path / "real"
    root.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(root, target_is_directory=True)
    with pytest.raises(BundleError, match="real directory"):
        bundle.sync_bundle(linked)
    assert not any(root.iterdir())


@pytest.mark.parametrize(
    "filename", ["../private.py", "/tmp/private.py", "nested/private.py"]
)
def test_manifest_path_escape_is_rejected(consumer: Path, filename: str) -> None:
    manifest_path = consumer / BUNDLE_DIRECTORY / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_bytes())
    manifest["files"][filename] = manifest["files"].pop("dependency_manifest.py")
    manifest["payload_sha256"] = payload_digest(manifest["files"])
    manifest_path.write_text(json.dumps(manifest))
    before = _snapshot(consumer)
    with pytest.raises(BundleError, match="complete source file set"):
        bundle.sync_bundle(consumer)
    assert _snapshot(consumer) == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("schema_version", 3),
        ("toolkit_name", "other"),
        ("toolkit_version", "0.1.0.dev1"),
        ("payload_sha256", "0" * 64),
        ("extra", "unknown"),
    ],
)
def test_invalid_manifest_is_rejected(
    consumer: Path, field: str, value: object
) -> None:
    path = consumer / BUNDLE_DIRECTORY / MANIFEST_NAME
    manifest = json.loads(path.read_bytes())
    manifest[field] = value
    path.write_text(json.dumps(manifest))
    with pytest.raises(BundleError):
        verify_bundle(consumer)


def test_duplicate_manifest_keys_are_rejected(consumer: Path) -> None:
    path = consumer / BUNDLE_DIRECTORY / MANIFEST_NAME
    path.write_text(
        path.read_text().replace(
            '"schema_version": 2', '"schema_version": 2, "schema_version": 2'
        )
    )
    with pytest.raises(BundleError, match="Duplicate"):
        verify_bundle(consumer)


def test_standalone_verifier_needs_no_installed_toolkit(consumer: Path) -> None:
    script = consumer / BUNDLE_DIRECTORY / "bundle_verify.py"
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(script), "--repository", str(consumer)],
        cwd=consumer,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Verified h2h-dev-tools 0.1.0" in result.stdout
    assert {path.name for path in script.parent.iterdir()} == BUNDLE_FILES | {
        MANIFEST_NAME
    }


def test_cli_reports_sync_noop_and_verification(
    consumer: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["sync", "--repository", str(consumer)]) == 0
    assert "unchanged" in capsys.readouterr().out
    assert main(["verify", "--repository", str(consumer)]) == 0
    assert "Verified" in capsys.readouterr().out
    (consumer / BUNDLE_DIRECTORY / "unknown").touch()
    assert main(["verify", "--repository", str(consumer)]) == 1
    assert "closure mismatch" in capsys.readouterr().err


def test_initial_empty_directory_is_accepted(
    tmp_path: Path, source_payload: dict[str, bytes]
) -> None:
    del source_payload
    (tmp_path / BUNDLE_DIRECTORY).mkdir(parents=True)
    configure(tmp_path)
    assert bundle.sync_bundle(tmp_path)
    verify_bundle(tmp_path)


def test_initial_unknown_contents_are_not_adopted(
    tmp_path: Path, source_payload: dict[str, bytes]
) -> None:
    del source_payload
    directory = tmp_path / BUNDLE_DIRECTORY
    directory.mkdir(parents=True)
    (directory / "__init__.py").write_text("private code\n")
    before = _snapshot(tmp_path)
    with pytest.raises(BundleError):
        bundle.sync_bundle(tmp_path)
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("version", ["0.1.0", "bad", None])
def test_source_payload_uses_installed_metadata_and_validates_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str | None
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for name in BUNDLE_FILES:
        (source / name).write_bytes(b'"""Source fixture."""\n')
    monkeypatch.setattr(bundle, "__file__", str(source / "bundle.py"))

    def installed_version(distribution: str) -> str:
        assert distribution == "h2h-dev-tools"
        if version is None:
            raise metadata.PackageNotFoundError(distribution)
        return version

    monkeypatch.setattr(metadata, "version", installed_version)
    repository = tmp_path / "consumer"
    repository.mkdir()
    configure(repository)
    before = _snapshot(repository)
    if version == "0.1.0":
        assert bundle.sync_bundle(repository)
        assert verify_bundle(repository)["toolkit_version"] == version
    else:
        with pytest.raises(BundleError):
            bundle.sync_bundle(repository)
        assert _snapshot(repository) == before


def test_changed_destination_during_staging_is_rejected(
    consumer: Path, source_payload: dict[str, bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    source_payload["dependency_manifest.py"] = b'"""New source."""\n'
    original_write = Path.write_bytes
    victim = consumer / BUNDLE_DIRECTORY / "dependency_manifest.py"

    def changing_write(path: Path, content: bytes) -> int:
        count = original_write(path, content)
        if path.name == MANIFEST_NAME:
            original_write(victim, b"concurrent private edit\n")
        return count

    monkeypatch.setattr(Path, "write_bytes", changing_write)
    with pytest.raises(BundleError, match="modified"):
        bundle.sync_bundle(consumer)
    assert victim.read_bytes() == b"concurrent private edit\n"


def test_normal_import_caches_are_allowed_and_removed_on_source_update(
    consumer: Path, source_payload: dict[str, bytes]
) -> None:
    directory = consumer / BUNDLE_DIRECTORY
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
            "import h2h_dev_tools.dependency_manifest; "
            "from h2h_dev_tools.bundle_verify import verify_bundle; "
            "verify_bundle(Path(sys.argv[2]))",
            str(directory.parent),
            str(consumer),
        ],
        cwd=consumer,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    cache = directory / "__pycache__"
    assert tuple(cache.glob("*.pyc"))
    verify_bundle(consumer)
    before = _snapshot(consumer)
    assert not bundle.sync_bundle(consumer)
    assert _snapshot(consumer) == before
    source_payload["dependency_manifest.py"] = b'"""Changed implementation."""\n'
    assert bundle.sync_bundle(consumer)
    assert not cache.exists()
    verify_bundle(consumer)


def test_interrupted_update_fails_closed_until_generated_files_are_restored(
    consumer: Path, source_payload: dict[str, bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = consumer / BUNDLE_DIRECTORY
    previous = {path.name: path.read_bytes() for path in directory.iterdir()}
    local = consumer / "scripts/local.py"
    local.write_text("keep this local tool\n")
    source_payload["__init__.py"] = b'"""Updated source."""\n'
    original_replace = os.replace

    def fail_second_replace(source: Path, destination: Path) -> None:
        if destination.name == "bundle_verify.py":
            raise OSError("injected replacement failure")
        original_replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail_second_replace)
        with pytest.raises(BundleError, match="injected replacement failure"):
            bundle.sync_bundle(consumer)
    assert (directory / "__init__.py").read_bytes() != previous["__init__.py"]
    for operation in (verify_bundle, bundle.sync_bundle):
        with pytest.raises(BundleError, match="modified"):
            operation(consumer)
    for name, content in previous.items():
        (directory / name).write_bytes(content)
    assert bundle.sync_bundle(consumer)
    verify_bundle(consumer)
    assert local.read_text() == "keep this local tool\n"


@pytest.mark.parametrize(
    "corruption",
    [
        "directory-symlink",
        "file-symlink",
        "cache-file",
        "unknown-module",
        "nested-directory",
        "other-file",
    ],
)
@pytest.mark.parametrize("operation", [bundle.sync_bundle, verify_bundle])
def test_unrecognized_cache_entries_are_rejected_without_modification(
    consumer: Path, corruption: str, operation: object
) -> None:
    directory = consumer / BUNDLE_DIRECTORY
    cache = directory / "__pycache__"
    if corruption == "directory-symlink":
        cache.symlink_to(consumer, target_is_directory=True)
    elif corruption == "cache-file":
        cache.write_text("unknown private file\n")
    else:
        cache.mkdir()
        if corruption == "file-symlink":
            (cache / "dependency_manifest.cpython-314.pyc").symlink_to(
                directory / "__init__.py"
            )
        elif corruption == "unknown-module":
            (cache / "unknown.cpython-314.pyc").write_bytes(b"unknown")
        elif corruption == "nested-directory":
            (cache / "dependency_manifest.cpython-314.pyc").mkdir()
        else:
            (cache / "private.py").write_text("private source\n")
    before = _snapshot(consumer)
    assert callable(operation)
    with pytest.raises(BundleError, match="cache"):
        operation(consumer)
    assert _snapshot(consumer) == before
