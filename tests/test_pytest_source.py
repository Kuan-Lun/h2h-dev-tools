"""The source pytest runner carries candidate authority into real local workers."""

from __future__ import annotations

import json
import os
import py_compile
import runpy
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from h2h_dev_tools import bundle, pytest_source

_SOURCE = Path(__file__).resolve().parents[1] / "src/h2h_dev_tools"
_ENVIRONMENT_KEYS = ("PYTHONPYCACHEPREFIX", "PYTHONDONTWRITEBYTECODE", "PYTHONPATH")


@dataclass(frozen=True)
class _Candidate:
    root: Path
    package: Path
    namespace: str
    import_root: Path


def _candidate(tmp_path: Path, layout: str) -> _Candidate:
    root = tmp_path / "candidate with spaces"
    root.mkdir()
    if layout == "snapshot":
        (root / "pyproject.toml").write_text(
            '[tool.h2h-dev-tools]\ncomponents = ["pytest", "version-policy"]\n'
        )
        bundle.sync_bundle(root)
        (root / "scripts/_shared/__init__.py").write_text("")
        package = root / "scripts/_shared/h2h_dev_tools"
        namespace = "_shared.h2h_dev_tools"
        import_root = root / "scripts"
    else:
        package = root / "src/h2h_dev_tools"
        shutil.copytree(_SOURCE, package, ignore=shutil.ignore_patterns("__pycache__"))
        namespace = "h2h_dev_tools"
        import_root = root / "src"
    bootstrap = (
        "import runpy, sys\nfrom pathlib import Path\n"
        f"sys.path.append({str(import_root)!r})\n"
        f"package = Path({str(package)!r})\n"
        "runpy.run_path(str(package / 'source_loader.py'))['load_package'](\n"
        f"    package, {namespace!r})\n"
    )
    (root / "entry.py").write_text(
        bootstrap + f"from {namespace}.pytest_source import main\nmain()\n"
    )
    (root / "parent_only.py").write_text(
        bootstrap + "runpy.run_module('pytest', run_name='__main__', alter_sys=True)\n"
    )
    (root / "conftest.py").write_text(
        "import json, os, sys\nfrom pathlib import Path\n"
        f"from {namespace} import version_policy as candidate\n"
        "def pytest_sessionstart(session):\n"
        "    worker = os.environ.get('PYTEST_XDIST_WORKER', 'master')\n"
        "    report = {\n"
        "        'source': str(Path(candidate.__file__).resolve()),\n"
        "        'prefix': sys.pycache_prefix,\n"
        "        'dont_write': sys.dont_write_bytecode,\n"
        "        'environment_prefix': os.environ.get('PYTHONPYCACHEPREFIX'),\n"
        "        'pythonpath': os.environ.get('PYTHONPATH'),\n"
        "    }\n"
        "    (Path(__file__).parent / (worker + '.json')).write_text(json.dumps(report))\n"
    )
    (root / "test_candidate.py").write_text(
        f"from {namespace} import version_policy as candidate\n"
        "from pathlib import Path\nimport pytest\n"
        "@pytest.mark.parametrize('value', range(4))\n"
        "def test_candidate(value):\n"
        f"    assert Path(candidate.__file__).resolve() == Path({str(package / 'version_policy.py')!r})\n"
        "    assert candidate._parse_version('1.2.3') == (1, 2, 3)\n"
        "def test_deliberate_failure():\n"
        "    assert False, 'deliberate selection oracle'\n"
    )
    return _Candidate(root, package, namespace, import_root)


def _environment(candidate: _Candidate) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTHON", "PYTEST_"))
    }
    environment.update(
        PYTHONPATH=str(candidate.import_root), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1"
    )
    return environment


def _run(
    candidate: _Candidate, environment: dict[str, str], *arguments: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *arguments],
        cwd=candidate.root,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=45,
    )


def _poison_module_cache(candidate: _Candidate, tmp_path: Path) -> None:
    module = candidate.package / "version_policy.py"
    original = module.read_bytes()
    previous = b"raise RuntimeError('STALE MODULE CACHE')\n"
    stale = tmp_path / "previous_version_policy.py"
    stale.write_bytes(previous + b"#" * (len(original) - len(previous)))
    timestamp = module.stat().st_mtime_ns
    os.utime(stale, ns=(timestamp, timestamp))
    cache = (
        module.parent
        / "__pycache__"
        / (f"{module.stem}.{sys.implementation.cache_tag}.pyc")
    )
    py_compile.compile(
        str(stale),
        cfile=str(cache),
        doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP,
    )
    assert module.read_bytes() == original
    assert stale.stat().st_size == module.stat().st_size
    assert stale.stat().st_mtime_ns == timestamp


def _poison_conftest_cache(candidate: _Candidate, environment: dict[str, str]) -> None:
    conftest = candidate.root / "conftest.py"
    current = conftest.read_bytes()
    previous = b"raise RuntimeError('STALE ASSERTION REWRITE CACHE')\n"
    conftest.write_bytes(previous + b"#" * (len(current) - len(previous)))
    timestamp = conftest.stat().st_mtime_ns
    warm = _run(candidate, environment, "-m", "pytest", "--collect-only")
    assert warm.returncode != 0
    assert "STALE ASSERTION REWRITE CACHE" in warm.stderr
    caches = list((candidate.root / "__pycache__").glob("conftest.*-pytest-*.pyc"))
    assert len(caches) == 1, warm.stdout + warm.stderr
    conftest.write_bytes(current)
    os.utime(conftest, ns=(timestamp, timestamp))


@pytest.mark.parametrize("layout", ["source", "snapshot"])
@pytest.mark.parametrize("hazard", ["old-sibling", "module-cache", "conftest-cache"])
def test_local_xdist_workers_use_candidate_source_and_fresh_caches(
    tmp_path: Path, layout: str, hazard: str
) -> None:
    candidate = _candidate(tmp_path, layout)
    environment = _environment(candidate)
    if hazard == "old-sibling":
        old_root = tmp_path / "old sibling"
        old_package = old_root.joinpath(*candidate.namespace.split("."))
        shutil.copytree(
            candidate.package, old_package, ignore=shutil.ignore_patterns("__pycache__")
        )
        if layout == "snapshot":
            (old_root / "_shared/__init__.py").write_text("")
        (old_package / "version_policy.py").write_text(
            "raise RuntimeError('OLD SIBLING EXECUTED')\n"
        )
        environment["PYTHONPATH"] = str(old_root)
        marker = "OLD SIBLING EXECUTED"
    elif hazard == "module-cache":
        _poison_module_cache(candidate, tmp_path)
        marker = "STALE MODULE CACHE"
    else:
        _poison_conftest_cache(candidate, environment)
        marker = "STALE ASSERTION REWRITE CACHE"
    arguments = (
        "-p",
        "no:cacheprovider",
        "-p",
        "xdist.plugin",
        "-n",
        "2",
        "-q",
        "test_candidate.py",
        "-k",
        "not deliberate_failure",
    )
    old = _run(candidate, environment, "-B", "parent_only.py", *arguments)
    assert old.returncode != 0, old.stdout + old.stderr
    assert marker in old.stdout + old.stderr
    for report in candidate.root.glob("*.json"):
        report.unlink()

    result = _run(candidate, environment, "-B", "entry.py", *arguments)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 passed" in result.stdout
    reports = {
        worker: json.loads((candidate.root / f"{worker}.json").read_text())
        for worker in ("master", "gw0", "gw1")
    }
    prefixes = {report["prefix"] for report in reports.values()}
    assert len(prefixes) == 1
    for report in reports.values():
        assert report["source"] == str(candidate.package / "version_policy.py")
        assert report["dont_write"] is True
        assert report["environment_prefix"] == report["prefix"]
        assert report["pythonpath"].split(os.pathsep)[0] == str(candidate.import_root)
        assert not Path(report["prefix"]).exists()


@pytest.mark.parametrize("layout", ["source", "snapshot"])
@pytest.mark.parametrize(
    "selection,expected",
    [("not deliberate_failure", 0), ("deliberate_failure", 1), ("no_matching_test", 5)],
)
def test_source_runner_preserves_pytest_selection_and_exit_status(
    tmp_path: Path, layout: str, selection: str, expected: int
) -> None:
    candidate = _candidate(tmp_path, layout)
    result = _run(
        candidate,
        _environment(candidate),
        "-B",
        "entry.py",
        "-p",
        "no:cacheprovider",
        "-q",
        "test_candidate.py",
        "-k",
        selection,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    report = json.loads((candidate.root / "master.json").read_text())
    assert not Path(report["prefix"]).exists()


@pytest.mark.parametrize("prior", [None, "", "prior-value"])
@pytest.mark.parametrize("failure", [False, True])
def test_source_runner_restores_process_state_even_when_pytest_raises(
    monkeypatch: pytest.MonkeyPatch, prior: str | None, failure: bool
) -> None:
    for key in _ENVIRONMENT_KEYS:
        if prior is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, prior)
    monkeypatch.setattr(sys, "argv", ["pytest-source.py", "-k", "selected"])
    monkeypatch.setattr(sys, "pycache_prefix", prior)
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    original_path = sys.path
    original_arguments = sys.argv
    observed: list[Path] = []

    def run_pytest(module: str, *, run_name: str, alter_sys: bool) -> None:
        assert (module, run_name, alter_sys) == ("pytest", "__main__", True)
        assert sys.argv is original_arguments
        assert sys.pycache_prefix is not None
        prefix = Path(sys.pycache_prefix)
        observed.append(prefix)
        assert prefix.is_dir() and not list(prefix.iterdir())
        assert sys.dont_write_bytecode
        assert sys.path[0] == str(_SOURCE.parent)
        assert sys.path[1:] == original_path
        assert os.environ["PYTHONPYCACHEPREFIX"] == str(prefix)
        assert os.environ["PYTHONDONTWRITEBYTECODE"] == "1"
        expected_path = str(_SOURCE.parent) + (
            os.pathsep + prior if prior is not None else ""
        )
        assert os.environ["PYTHONPATH"] == expected_path
        if failure:
            raise RuntimeError("pytest plugin failed")
        raise SystemExit(5)

    monkeypatch.setattr(runpy, "run_module", run_pytest)
    for _ in range(2):
        with pytest.raises(RuntimeError if failure else SystemExit) as caught:
            pytest_source.main()
        if not failure:
            assert isinstance(caught.value, SystemExit)
            assert caught.value.code == 5
        assert sys.path is original_path
        assert sys.argv is original_arguments
        assert sys.pycache_prefix == prior
        assert sys.dont_write_bytecode is False
        assert {key: os.environ.get(key) for key in _ENVIRONMENT_KEYS} == dict.fromkeys(
            _ENVIRONMENT_KEYS, prior
        )
        assert not observed[-1].exists()
    assert observed[0] != observed[1]


@pytest.mark.parametrize(
    "corruption", ["missing-manifest", "changed-manifest", "changed-source"]
)
def test_snapshot_runner_rejects_invalid_authority_before_loading_pytest(
    tmp_path: Path, corruption: str
) -> None:
    candidate = _candidate(tmp_path, "snapshot")
    manifest = candidate.package / "manifest.json"
    if corruption == "missing-manifest":
        manifest.unlink()
    elif corruption == "changed-manifest":
        document = json.loads(manifest.read_text())
        document["files"]["pytest_source.py"] = "0" * 64
        manifest.write_text(json.dumps(document))
    else:
        source = candidate.package / "pytest_source.py"
        source.write_bytes(source.read_bytes() + b"\n# unverified source change\n")
    result = _run(candidate, _environment(candidate), "-B", "entry.py", "-q")
    assert result.returncode != 0
    assert "BundleError" in result.stderr
    assert not (candidate.root / "master.json").exists()
