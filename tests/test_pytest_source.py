"""The source pytest runner carries candidate authority into real local workers."""

from __future__ import annotations

import json
import os
import py_compile
import shlex
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
        bootstrap
        + f"from {namespace}.pytest_source import main\nmain(runtime_sources={{}})\n"
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
        "        'source_registered': getattr(candidate.__loader__, '_h2h_source_registration', None) is not None,\n"
        "        'prefix': sys.pycache_prefix,\n"
        "        'dont_write': sys.dont_write_bytecode,\n"
        "        'ignore_environment': sys.flags.ignore_environment,\n"
        "        'isolated': sys.flags.isolated,\n"
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


def _poison_pytest_cache(
    candidate: _Candidate, environment: dict[str, str], filename: str
) -> None:
    conftest = candidate.root / filename
    current = conftest.read_bytes()
    previous = b"raise RuntimeError('STALE ASSERTION REWRITE CACHE')\n"
    conftest.write_bytes(previous + b"#" * (len(current) - len(previous)))
    timestamp = conftest.stat().st_mtime_ns
    warm = _run(candidate, environment, "-m", "pytest", "--collect-only")
    assert warm.returncode != 0
    assert "STALE ASSERTION REWRITE CACHE" in warm.stdout + warm.stderr
    caches = list(
        (candidate.root / "__pycache__").glob(f"{conftest.stem}.*-pytest-*.pyc")
    )
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
        _poison_pytest_cache(candidate, environment, "conftest.py")
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
        assert report["source_registered"] is True
        assert report["dont_write"] is True
        assert report["environment_prefix"] == report["prefix"]
        assert report["pythonpath"].split(os.pathsep)[0] == str(candidate.import_root)
        assert not Path(report["prefix"]).exists()


@pytest.mark.parametrize("layout", ["source", "snapshot"])
@pytest.mark.parametrize("flag", ["-I", "-E"])
@pytest.mark.parametrize("filename", ["conftest.py", "test_candidate.py"])
def test_local_isolated_workers_ignore_stale_pytest_caches(
    tmp_path: Path, layout: str, flag: str, filename: str
) -> None:
    candidate = _candidate(tmp_path, layout)
    environment = _environment(candidate)
    _poison_pytest_cache(candidate, environment, filename)
    caches = {p: p.read_bytes() for p in candidate.root.rglob("*.pyc")}
    assert caches

    result = _run(
        candidate,
        environment,
        "-B",
        "entry.py",
        "-p",
        "no:cacheprovider",
        "-p",
        "xdist.plugin",
        "--dist=load",
        "--max-worker-restart=0",
        "--tx",
        "popen//python=" + shlex.join([sys.executable, flag]),
        "-q",
        "test_candidate.py",
        "-k",
        "not deliberate_failure",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 passed" in result.stdout
    controller = json.loads((candidate.root / "master.json").read_text())
    worker = json.loads((candidate.root / "gw0.json").read_text())
    assert controller["ignore_environment"] == 0
    assert worker["ignore_environment"] == 1
    assert worker["isolated"] == (flag == "-I")
    assert worker["source_registered"] is True
    assert worker["source"] == str(candidate.package / "version_policy.py")
    assert worker["prefix"] == controller["prefix"] == worker["environment_prefix"]
    assert worker["dont_write"] is True
    assert not Path(worker["prefix"]).exists()
    assert {p: p.read_bytes() for p in candidate.root.rglob("*.pyc")} == caches


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

    def run_pytest(*, plugins: list[object]) -> int:
        assert len(plugins) == 1
        assert hasattr(plugins[0], "pytest_xdist_newgateway")
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
        return 5

    monkeypatch.setattr(pytest, "main", run_pytest)
    for _ in range(2):
        with pytest.raises(RuntimeError if failure else SystemExit) as caught:
            pytest_source.main(runtime_sources={})
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


@pytest.mark.parametrize("corruption", ["missing-manifest", "changed-source"])
def test_local_worker_revalidates_snapshot_after_controller_bootstrap(
    tmp_path: Path, corruption: str
) -> None:
    candidate = _candidate(tmp_path, "snapshot")
    mutation = (
        f"Path({str(candidate.package / 'manifest.json')!r}).unlink()"
        if corruption == "missing-manifest"
        else f"Path({str(candidate.package / 'version_policy.py')!r}).write_text('raise RuntimeError(\\'unverified worker source\\')')"
    )
    conftest = candidate.root / "conftest.py"
    conftest.write_text(
        conftest.read_text()
        + "def pytest_configure(config):\n"
        + "    if not hasattr(config, 'workerinput'):\n"
        + f"        {mutation}\n"
    )

    result = _run(
        candidate,
        _environment(candidate),
        "entry.py",
        "-p",
        "no:cacheprovider",
        "-p",
        "xdist.plugin",
        "-n",
        "2",
        "-q",
        "test_candidate.py",
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert "BundleError" in result.stdout + result.stderr
    assert (candidate.root / "master.json").exists()
    assert not (candidate.root / "gw0.json").exists()
    assert not (candidate.root / "gw1.json").exists()


@pytest.mark.parametrize("layout", ["source", "snapshot"])
@pytest.mark.parametrize(
    "filename",
    ["version_policy.py", "source_loader.py", "bundle_verify.py", "__init__.py"],
)
def test_local_workers_reject_valid_source_replacement_after_controller_bootstrap(
    tmp_path: Path, layout: str, filename: str
) -> None:
    candidate = _candidate(tmp_path, layout)
    changed = candidate.package / filename
    executed = candidate.root / "replacement-initializer-executed"
    suffix = (
        f"\nfrom pathlib import Path\nPath({str(executed)!r}).touch()\n".encode()
        if filename in {"__init__.py", "bundle_verify.py"}
        else b"\n# a different, independently valid source generation\n"
    )
    conftest = candidate.root / "conftest.py"
    conftest.write_text(
        conftest.read_text()
        + "def pytest_configure(config):\n"
        + "    if not hasattr(config, 'workerinput'):\n"
        + "        import hashlib\n"
        + f"        changed = Path({str(changed)!r})\n"
        + f"        changed.write_bytes(changed.read_bytes() + {suffix!r})\n"
        + f"        manifest = Path({str(candidate.package / 'manifest.json')!r})\n"
        + "        if manifest.exists():\n"
        + "            document = json.loads(manifest.read_text())\n"
        + "            document['files'][changed.name] = hashlib.sha256(changed.read_bytes()).hexdigest()\n"
        + "            payload = json.dumps(document['files'], sort_keys=True, separators=(',', ':')).encode()\n"
        + "            document['payload_sha256'] = hashlib.sha256(payload).hexdigest()\n"
        + "            manifest.write_text(json.dumps(document))\n"
    )

    result = _run(
        candidate,
        _environment(candidate),
        "entry.py",
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

    assert result.returncode != 0, result.stdout + result.stderr
    assert "controller" in result.stdout + result.stderr
    assert (candidate.root / "master.json").exists()
    assert not (candidate.root / "gw0.json").exists()
    assert not (candidate.root / "gw1.json").exists()
    assert not executed.exists(), (
        "replacement package must not execute before rejection"
    )


def _runtime_candidate(candidate: _Candidate) -> tuple[Path, Path]:
    runtime = candidate.root / "owned runtime sources/owned_runtime"
    nested = runtime / "nested"
    nested.mkdir(parents=True)
    (runtime / "__init__.py").write_text("VALUE = 'candidate root'\n")
    (nested / "__init__.py").write_text("")
    (nested / "logic.py").write_text("VALUE = 'candidate nested'\n")
    foreign = candidate.root / "foreign/owned_runtime"
    (foreign / "nested").mkdir(parents=True)
    marker = candidate.root / "foreign-runtime-executed"
    (foreign / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
        "VALUE = 'foreign root'\n"
    )
    (foreign / "nested/__init__.py").write_text("")
    (foreign / "nested/logic.py").write_text("VALUE = 'foreign nested'\n")
    entry = candidate.root / "entry.py"
    entry.write_text(
        entry.read_text().replace(
            "main(runtime_sources={})",
            f"main(runtime_sources={{'owned_runtime': Path({str(runtime)!r})}})",
        )
    )
    conftest = candidate.root / "conftest.py"
    conftest.write_text(
        "import owned_runtime\nfrom owned_runtime.nested import logic\n"
        + conftest.read_text().replace(
            "    report = {\n",
            "    report = {\n"
            "        'runtime_source': owned_runtime.__file__,\n"
            "        'runtime_module_source': logic.__file__,\n",
        )
    )
    (candidate.root / "test_candidate.py").write_text(
        "import pytest, owned_runtime\n"
        "from owned_runtime.nested import logic\n"
        "from packaging.version import Version\n"
        "@pytest.mark.parametrize('value', range(4))\n"
        "def test_owned_runtime(value):\n"
        "    assert owned_runtime.VALUE == 'candidate root'\n"
        "    assert logic.VALUE == 'candidate nested'\n"
        "    assert Version('1.2.3').release == (1, 2, 3)\n"
    )
    return runtime, foreign


@pytest.mark.parametrize("layout", ["source", "snapshot"])
@pytest.mark.parametrize("worker_flag", ["", "-I"])
@pytest.mark.parametrize("cache_mode", ["TIMESTAMP", "UNCHECKED_HASH"])
def test_controller_and_workers_bind_owned_runtime_before_plugin_imports(
    tmp_path: Path, layout: str, worker_flag: str, cache_mode: str
) -> None:
    candidate = _candidate(tmp_path, layout)
    runtime, foreign = _runtime_candidate(candidate)
    environment = _environment(candidate)
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(foreign.parent), str(candidate.import_root))
    )
    module = runtime / "nested/logic.py"
    stale = b"raise RuntimeError('STALE RUNTIME BYTECODE')\n"
    current = module.read_bytes().ljust(len(stale), b" ")
    module.write_bytes(stale)
    timestamp = module.stat().st_mtime_ns
    cache = py_compile.compile(
        str(module),
        doraise=True,
        invalidation_mode=getattr(py_compile.PycInvalidationMode, cache_mode),
    )
    assert cache is not None
    module.write_bytes(current)
    os.utime(module, ns=(timestamp, timestamp))
    cached = Path(cache).read_bytes()
    # The ordinary invocation really selects the foreign runtime. Merely
    # loading the toolkit source and suppressing bytecode writes cannot fix it.
    ordinary = _run(
        candidate, environment, "-B", "parent_only.py", "-p", "no:cacheprovider", "-q"
    )
    assert ordinary.returncode != 0, ordinary.stdout + ordinary.stderr
    marker = candidate.root / "foreign-runtime-executed"
    assert marker.exists()
    assert json.loads((candidate.root / "master.json").read_text())[
        "runtime_source"
    ] == str(foreign / "__init__.py")
    marker.unlink()
    (candidate.root / "master.json").unlink()
    # Normal assertion registration must not bypass runtime source authority.
    # An explicit plugin imports runtime before conftest collection starts.
    (candidate.root / "runtime_probe.py").write_text(
        "import pytest\npytest.register_assert_rewrite('owned_runtime')\n"
        "import owned_runtime\nfrom owned_runtime.nested import logic\n"
        "assert owned_runtime.VALUE == 'candidate root'\n"
        "assert logic.VALUE == 'candidate nested'\n"
    )
    arguments = (
        "entry.py",
        "-p",
        "no:cacheprovider",
        "-p",
        "runtime_probe",
        "-p",
        "xdist.plugin",
        "--max-worker-restart=0",
        "--dist=load",
        "--tx",
        "popen//python="
        + shlex.join([sys.executable, *([worker_flag] if worker_flag else [])]),
        "-q",
        "test_candidate.py",
    )
    result = _run(candidate, environment, *arguments)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 passed" in result.stdout
    for worker in ("master", "gw0"):
        report = json.loads((candidate.root / f"{worker}.json").read_text())
        assert report["runtime_source"] == str(runtime / "__init__.py")
        assert report["runtime_module_source"] == str(module)
        assert report["dont_write"] is True
    assert not marker.exists()
    assert Path(cache).read_bytes() == cached


@pytest.mark.parametrize("layout", ["source", "snapshot"])
@pytest.mark.parametrize("prior_source", ["candidate", "foreign"])
def test_preimported_runtime_is_rejected_before_pytest_imports(
    tmp_path: Path, layout: str, prior_source: str
) -> None:
    candidate = _candidate(tmp_path, layout)
    runtime, foreign = _runtime_candidate(candidate)
    entry = candidate.root / "entry.py"
    source = runtime if prior_source == "candidate" else foreign
    entry.write_text(
        entry.read_text().replace(
            "main(runtime_sources=",
            f"sys.path.insert(0, {str(source.parent)!r})\nimport owned_runtime\n"
            "assert 'pytest' not in sys.modules\nmain(runtime_sources=",
        )
    )
    result = _run(candidate, _environment(candidate), "entry.py", "-q")
    assert result.returncode != 0, result.stdout + result.stderr
    assert (
        "Runtime package was imported before source binding: owned_runtime"
        in result.stderr
    )
    assert not (candidate.root / "master.json").exists()


@pytest.mark.parametrize("layout", ["source", "snapshot"])
@pytest.mark.parametrize("mutation", ["change", "add", "remove"])
def test_workers_reject_owned_runtime_drift_before_loading_it(
    tmp_path: Path, layout: str, mutation: str
) -> None:
    candidate = _candidate(tmp_path, layout)
    runtime, _ = _runtime_candidate(candidate)
    marker = candidate.root / "changed-runtime-executed"
    code = (
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\nVALUE = 'changed'\n"
    )
    module = runtime / "nested/logic.py"
    change = (
        "changed.unlink()" if mutation == "remove" else f"changed.write_text({code!r})"
    )
    if mutation == "add":
        module = runtime / "new_module.py"
    conftest = candidate.root / "conftest.py"
    conftest.write_text(
        conftest.read_text() + "def pytest_configure(config):\n"
        "    if not hasattr(config, 'workerinput'):\n"
        f"        changed = Path({str(module)!r})\n"
        f"        {change}\n"
    )
    result = _run(
        candidate,
        _environment(candidate),
        "entry.py",
        "-p",
        "no:cacheprovider",
        "-p",
        "xdist.plugin",
        "--max-worker-restart=0",
        "-n",
        "2",
        "-q",
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert (
        "Runtime source differs from controller: owned_runtime"
        in result.stdout + result.stderr
    )
    assert (candidate.root / "master.json").exists()
    assert not (candidate.root / "gw0.json").exists()
    assert not (candidate.root / "gw1.json").exists()
    assert not marker.exists()


@pytest.mark.parametrize("layout", ["source", "snapshot"])
def test_controller_rechecks_runtime_bytes_at_first_import(
    tmp_path: Path, layout: str
) -> None:
    candidate = _candidate(tmp_path, layout)
    runtime, _ = _runtime_candidate(candidate)
    marker = candidate.root / "changed-runtime-executed"
    changed = runtime / "__init__.py"
    code = f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
    (candidate.root / "runtime_mutator.py").write_text(
        f"from pathlib import Path\nPath({str(changed)!r}).write_text({code!r})\n"
    )
    result = _run(
        candidate,
        _environment(candidate),
        "entry.py",
        "-p",
        "runtime_mutator",
        "-q",
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "Runtime source changed after capture:" in result.stdout + result.stderr
    assert not marker.exists()


@pytest.mark.parametrize("layout", ["source", "snapshot"])
@pytest.mark.parametrize("worker", [False, True])
def test_assertion_rewrite_cannot_bypass_runtime_hash_checks(
    tmp_path: Path, layout: str, worker: bool
) -> None:
    candidate = _candidate(tmp_path, layout)
    runtime, _ = _runtime_candidate(candidate)
    changed = runtime / "nested/logic.py"
    marker = candidate.root / "changed-runtime-executed"
    code = (
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\nVALUE = 'changed'\n"
    )
    # Conftest executes after the rewrite hook is installed. Change an as-yet
    # unloaded child module then mark it for rewriting: hash validation must
    # precede execution even when the candidate is reachable by PathFinder.
    conftest = candidate.root / "conftest.py"
    conftest.write_text(
        "import os, sys, pytest\nfrom pathlib import Path\n"
        f"sys.path.insert(0, {str(runtime.parent)!r})\n"
        "pytest.register_assert_rewrite('owned_runtime')\n"
        f"if bool(os.environ.get('PYTEST_XDIST_WORKER')) == {worker!r}:\n"
        f"    Path({str(changed)!r}).write_text({code!r})\n" + conftest.read_text()
    )
    arguments = ["entry.py", "-p", "no:cacheprovider", "-q"]
    if worker:
        arguments.extend(["-p", "xdist.plugin", "--max-worker-restart=0", "-n", "1"])
    result = _run(candidate, _environment(candidate), *arguments)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "Runtime source changed after capture:" in result.stdout + result.stderr
    assert not marker.exists()
    assert not (candidate.root / "gw0.json").exists()
    assert (candidate.root / "master.json").exists() == worker


@pytest.mark.parametrize("shape", ["missing", "unregistered"])
def test_unsupported_assertion_installer_is_rejected(
    tmp_path: Path, shape: str
) -> None:
    candidate = _candidate(tmp_path, "source")
    _runtime_candidate(candidate)
    entry = candidate.root / "entry.py"
    mutation = (
        "del assertion.install_importhook"
        if shape == "missing"
        else "assertion.install_importhook = lambda config: object()"
    )
    entry.write_text(
        entry.read_text().replace(
            "main(runtime_sources=",
            "from _pytest import assertion\n" + mutation + "\nmain(runtime_sources=",
        )
    )
    result = _run(candidate, _environment(candidate), "entry.py", "-q")
    assert result.returncode != 0, result.stdout + result.stderr
    assert "Unsupported pytest assertion import-hook" in result.stdout + result.stderr
    assert not (candidate.root / "master.json").exists()


@pytest.mark.parametrize("assertion_mode", ["rewrite", "plain"])
def test_runtime_binding_preserves_pytest_assertion_mode(
    tmp_path: Path, assertion_mode: str
) -> None:
    candidate = _candidate(tmp_path, "source")
    _runtime_candidate(candidate)
    expected = assertion_mode == "rewrite"
    conftest = candidate.root / "conftest.py"
    conftest.write_text(
        f"assert ('@pytest_ar' in globals()) == {expected!r}\n" + conftest.read_text()
    )
    test = candidate.root / "test_candidate.py"
    test.write_text(
        f"assert ('@pytest_ar' in globals()) == {expected!r}\n" + test.read_text()
    )
    result = _run(
        candidate,
        _environment(candidate),
        "entry.py",
        "-p",
        "no:cacheprovider",
        f"--assert={assertion_mode}",
        "-q",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 passed" in result.stdout


def test_runtime_binding_restores_assertion_installer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from _pytest import assertion

    runtime = tmp_path / "unused_runtime"
    runtime.mkdir()
    (runtime / "__init__.py").write_text("")
    original_installer = assertion.install_importhook
    original_finders = list(sys.meta_path)

    def run_pytest(*, plugins: list[object]) -> int:
        assert assertion.install_importhook is not original_installer
        raise RuntimeError("pytest failed after binding")

    monkeypatch.setattr(pytest, "main", run_pytest)
    with pytest.raises(RuntimeError, match="pytest failed after binding"):
        pytest_source.main(runtime_sources={"unused_runtime": runtime})
    assert assertion.install_importhook is original_installer
    assert sys.meta_path == original_finders


@pytest.mark.parametrize("layout", ["source", "snapshot"])
@pytest.mark.parametrize("worker", [False, True])
@pytest.mark.parametrize("owned_runtime", [False, True])
def test_assertion_rewrite_cannot_bypass_toolkit_source_hashes(
    tmp_path: Path, layout: str, worker: bool, owned_runtime: bool
) -> None:
    candidate = _candidate(tmp_path, layout)
    if owned_runtime:
        _runtime_candidate(candidate)
    changed = candidate.package / "version_policy.py"
    marker = candidate.root / "changed-toolkit-executed"
    suffix = f"\nfrom pathlib import Path\nPath({str(marker)!r}).touch()\n"
    # Normal conftest registration happens after pytest installs rewriting, and
    # this toolkit module has not yet been imported by the source bootstrap.
    conftest = candidate.root / "conftest.py"
    conftest.write_text(
        "import os, pytest\nfrom pathlib import Path\n"
        f"pytest.register_assert_rewrite({(candidate.namespace + '.version_policy')!r})\n"
        f"if bool(os.environ.get('PYTEST_XDIST_WORKER')) == {worker!r}:\n"
        f"    changed = Path({str(changed)!r})\n"
        f"    changed.write_text(changed.read_text() + {suffix!r})\n"
        + conftest.read_text()
    )
    arguments = [
        "entry.py",
        "-p",
        "no:cacheprovider",
        "-q",
        "-k",
        "not deliberate_failure",
    ]
    if worker:
        arguments.extend(["-p", "xdist.plugin", "--max-worker-restart=0", "-n", "1"])
    result = _run(candidate, _environment(candidate), *arguments)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "Source changed after bootstrap:" in result.stdout + result.stderr
    assert not marker.exists()
    assert not (candidate.root / "gw0.json").exists()
    assert (candidate.root / "master.json").exists() == worker


@pytest.mark.parametrize("registration", ["missing", "duplicate", "different"])
def test_assertion_guard_requires_unique_verified_toolkit_finder(
    tmp_path: Path, registration: str
) -> None:
    candidate = _candidate(tmp_path, "source")
    mutation = {
        "missing": "sys.meta_path.remove(finder)",
        "duplicate": "sys.meta_path.append(finder)",
        "different": "finder._h2h_source_registration = ('foreign', 'foreign', {})",
    }[registration]
    entry = candidate.root / "entry.py"
    entry.write_text(
        entry.read_text().replace(
            "main(runtime_sources=",
            "finder = next(finder for finder in sys.meta_path if getattr(finder, '_h2h_source_registration', None) is not None)\n"
            + mutation
            + "\nmain(runtime_sources=",
        )
    )
    result = _run(candidate, _environment(candidate), "entry.py", "-q")
    assert result.returncode != 0, result.stdout + result.stderr
    assert (
        "exactly one registered toolkit source finder" in result.stdout + result.stderr
    )
    assert not (candidate.root / "master.json").exists()


@pytest.mark.parametrize("layout", ["source", "snapshot"])
@pytest.mark.parametrize("kind", ["empty", "resource", "python"])
def test_namespace_topology_and_resources_match_in_controller_and_worker(
    tmp_path: Path, layout: str, kind: str
) -> None:
    candidate = _candidate(tmp_path, layout)
    runtime, foreign = _runtime_candidate(candidate)
    namespace = runtime / "data"
    namespace.mkdir()
    (runtime / "regular.json").write_text("regular resource")
    checks = (
        "import os, pytest\nfrom pathlib import Path\n"
        "from importlib.resources import files\n"
        "pytest.register_assert_rewrite('owned_runtime')\n"
    )
    if kind != "empty":
        resource = namespace / "template.json"
        resource.write_text("before source capture")
        # Resource bytes intentionally retain ordinary file semantics and can
        # change between controller capture and worker bootstrap.
        checks += (
            "if not os.environ.get('PYTEST_XDIST_WORKER'):\n"
            f"    Path({str(resource)!r}).write_text('ordinary resource update')\n"
        )
    checks += (
        "import owned_runtime, owned_runtime.data\n"
        "assert owned_runtime.data.__file__ is None\n"
        f"assert list(owned_runtime.data.__path__) == [{str(namespace)!r}]\n"
        "assert files(owned_runtime).joinpath('regular.json').read_text() == 'regular resource'\n"
    )
    if kind == "empty":
        checks += "assert list(files(owned_runtime.data).iterdir()) == []\n"
    else:
        checks += "assert files('owned_runtime.data').joinpath('template.json').read_text() == 'ordinary resource update'\n"
    if kind == "python":
        (namespace / "logic.py").write_text("VALUE = 'namespace Python source'\n")
        checks += (
            "from owned_runtime.data import logic as namespace_logic\n"
            "assert namespace_logic.VALUE == 'namespace Python source'\n"
        )
    conftest = candidate.root / "conftest.py"
    conftest.write_text(checks + conftest.read_text())
    environment = _environment(candidate)
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(foreign.parent), str(candidate.import_root))
    )
    result = _run(
        candidate,
        environment,
        "entry.py",
        "-p",
        "no:cacheprovider",
        "-p",
        "xdist.plugin",
        "--max-worker-restart=0",
        "-n",
        "1",
        "-q",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 passed" in result.stdout
    for worker in ("master", "gw0"):
        assert (candidate.root / f"{worker}.json").exists()
    assert not (candidate.root / "foreign-runtime-executed").exists()


@pytest.mark.parametrize("layout", ["source", "snapshot"])
@pytest.mark.parametrize("worker", [False, True])
@pytest.mark.parametrize("filename", ["late.py", "__init__.py"])
def test_namespace_rejects_uncaptured_python_after_bootstrap(
    tmp_path: Path, layout: str, worker: bool, filename: str
) -> None:
    candidate = _candidate(tmp_path, layout)
    runtime, _ = _runtime_candidate(candidate)
    namespace = runtime / "data"
    namespace.mkdir()
    marker = candidate.root / "uncaptured-python-executed"
    content = f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
    load = (
        "import owned_runtime.data.late"
        if filename == "late.py"
        else "import owned_runtime.data"
    )
    conftest = candidate.root / "conftest.py"
    conftest.write_text(
        "import os, pytest\nfrom pathlib import Path\n"
        "pytest.register_assert_rewrite('owned_runtime')\n"
        f"if bool(os.environ.get('PYTEST_XDIST_WORKER')) == {worker!r}:\n"
        f"    Path({str(namespace / filename)!r}).write_text({content!r})\n"
        f"    {load}\n" + conftest.read_text()
    )
    arguments = ["entry.py", "-p", "no:cacheprovider", "-q"]
    if worker:
        arguments.extend(["-p", "xdist.plugin", "--max-worker-restart=0", "-n", "1"])
    result = _run(candidate, _environment(candidate), *arguments)
    assert result.returncode != 0, result.stdout + result.stderr
    message = (
        "No candidate runtime source for owned_runtime.data.late"
        if filename == "late.py"
        else "Runtime namespace directory changed:"
    )
    assert message in result.stdout + result.stderr
    assert not marker.exists()
    assert (candidate.root / "master.json").exists() == worker
    assert not (candidate.root / "gw0.json").exists()


@pytest.mark.parametrize("layout", ["source", "snapshot"])
@pytest.mark.parametrize("mutation", ["add", "remove"])
def test_workers_reject_namespace_directory_topology_drift(
    tmp_path: Path, layout: str, mutation: str
) -> None:
    candidate = _candidate(tmp_path, layout)
    runtime, _ = _runtime_candidate(candidate)
    directory = runtime / "empty"
    if mutation == "remove":
        directory.mkdir()
    operation = "mkdir" if mutation == "add" else "rmdir"
    conftest = candidate.root / "conftest.py"
    conftest.write_text(
        conftest.read_text()
        + "def pytest_configure(config):\n"
        + "    if not hasattr(config, 'workerinput'):\n"
        + f"        Path({str(directory)!r}).{operation}()\n"
    )
    result = _run(
        candidate,
        _environment(candidate),
        "entry.py",
        "-p",
        "no:cacheprovider",
        "-p",
        "xdist.plugin",
        "--max-worker-restart=0",
        "-n",
        "1",
        "-q",
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert (
        "Runtime source differs from controller: owned_runtime"
        in result.stdout + result.stderr
    )
    assert (candidate.root / "master.json").exists()
    assert not (candidate.root / "gw0.json").exists()
