"""Real fixture launchers preserve interpreter authority and command data."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import venv
import zipfile
from pathlib import Path

import pytest

from h2h_dev_tools import bundle_verify, executable_fixture


@pytest.mark.parametrize("status", [0, 7])
@pytest.mark.parametrize("name", ["fixture spaces", "工具 café 空間"])
def test_python_fixture_executes_with_spaced_venv_and_payload_paths(
    tmp_path: Path,
    status: int,
    name: str,
) -> None:
    directory = tmp_path / name
    environment = directory / "python environment"
    venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(environment)
    interpreter = environment / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python"
    )
    target = directory / "commands with spaces" / "fixture-tool"
    source = (
        "import json, os, sys\n"
        "print(json.dumps({'argv':sys.argv, 'file':__file__, 'cwd':os.getcwd(), "
        "'prefix':sys.prefix, 'executable':sys.executable, 'input':sys.stdin.read(), "
        "'pid':os.getpid()}))\n"
        f"raise SystemExit({status})\n"
    )
    executable = executable_fixture.write_python_executable(
        target, source, python=interpreter
    )
    cwd = directory / "working directory"
    cwd.mkdir()
    arguments = [
        "one argument",
        "unicode 測試",
        "",
        'a"b\\c',
        "%PATH%",
        "!value!",
        "line\nbreak",
    ]
    with subprocess.Popen(
        [str(executable), *arguments],
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as child:
        output, error = child.communicate("input bytes\n", timeout=15)
        assert child.returncode == status, error
        result = json.loads(output)
        if os.name == "posix":
            assert result["pid"] == child.pid
    if os.name == "nt":
        assert executable == target.with_name(target.name + ".exe")
        assert result["argv"] == [str(executable), *arguments]
        assert Path(result["file"]) == executable / "__main__.py"
        with zipfile.ZipFile(executable) as archive:
            assert archive.namelist() == ["__main__.py"]
            assert (
                archive.read("__main__.py")
                == ("#!/usr/bin/env python\n" + source).encode()
            )
    else:
        payload = target.with_name(target.name + ".py")
        assert result["argv"] == [str(payload), *arguments]
        assert result["file"] == str(payload)
        assert payload.read_text() == source
    assert Path(result["cwd"]) == cwd.resolve()
    assert Path(result["prefix"]) == environment.resolve()
    assert result["executable"] == str(interpreter)
    assert result["input"] == "input bytes\n"


@pytest.mark.skipif(
    os.name != "posix", reason="POSIX shell paths can contain line breaks and quotes"
)
def test_posix_fixture_quotes_interpreter_payload_and_arguments(tmp_path: Path) -> None:
    directory = tmp_path / "quote ' \" dollar $ semi ;\nline"
    directory.mkdir()
    interpreter = directory / "python executable"
    interpreter.symlink_to(sys.executable)
    target = directory / "tool ' \" $ ;\nname"
    executable = executable_fixture.write_python_executable(
        target,
        "import json, sys; print(json.dumps(sys.argv))\n",
        python=interpreter,
    )
    arguments = ["space and ' \" quotes", "$(touch unexpected)", "line\nbreak", ""]
    result = subprocess.run(
        [str(executable), *arguments],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    assert json.loads(result.stdout) == [str(target) + ".py", *arguments]
    assert not (tmp_path / "unexpected").exists()


def test_fixture_replaces_only_its_explicit_files(tmp_path: Path) -> None:
    target = tmp_path / "tool"
    unrelated = tmp_path / "keep"
    unrelated.write_text("unchanged")
    first = executable_fixture.write_python_executable(target, "raise SystemExit(3)\n")
    second = executable_fixture.write_python_executable(target, "raise SystemExit(7)\n")
    assert first == second
    assert unrelated.read_text() == "unchanged"
    result = subprocess.run([str(second)], check=False, timeout=15)
    assert result.returncode == 7


def test_test_support_is_explicit_and_outside_production_review_closure() -> None:
    assert "executable_fixture.py" in bundle_verify.component_files(["test-support"])
    assert "executable_fixture.py" not in bundle_verify.component_files(["code-review"])


@pytest.mark.skipif(os.name != "nt", reason="Native Windows console code-page oracle")
def test_windows_fixture_preserves_unicode_paths_under_legacy_code_page(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "工具 café %name%! 空間"
    environment = directory / "Python 環境"
    venv.EnvBuilder(with_pip=False).create(environment)
    interpreter = environment / "Scripts/python.exe"
    executable = executable_fixture.write_python_executable(
        directory / "command 測試.exe",
        "import json,sys\n"
        "print(json.dumps([sys.argv[1:], sys.stdin.buffer.read().hex(),sys.prefix]))\n"
        "raise SystemExit(7)\n",
        python=interpreter,
    )
    assert executable.name == "command 測試.exe"
    # Only this disposable child gets a new console. The parent console and
    # concurrent tests never share its deliberately legacy code page.
    driver = (
        "import ctypes,json,os,subprocess,sys\n"
        "kernel=ctypes.windll.kernel32\n"
        "assert kernel.SetConsoleCP(437)\n"
        "assert kernel.SetConsoleOutputCP(437)\n"
        "before=[kernel.GetConsoleCP(),kernel.GetConsoleOutputCP()]\n"
        "arguments=json.loads(os.environ['FIXTURE_ARGUMENTS'])\n"
        "result=subprocess.run(arguments,input=sys.stdin.buffer.read(),capture_output=True,timeout=15)\n"
        "after=[kernel.GetConsoleCP(),kernel.GetConsoleOutputCP()]\n"
        "print(json.dumps({'before':before,'after':after,'returncode':result.returncode,"
        "'stdout':result.stdout.decode('ascii'),'stderr':result.stderr.decode('ascii')}))\n"
    )
    arguments = ["unicode 測試", "", 'quote"value', "%PATH%", "!name!", "line\nbreak"]
    environment_values = dict(os.environ)
    environment_values["FIXTURE_ARGUMENTS"] = json.dumps([str(executable), *arguments])
    creation_flags = getattr(subprocess, "CREATE_NEW_CONSOLE", None)
    assert isinstance(creation_flags, int)
    result = subprocess.run(
        [sys.executable, "-c", driver],
        env=environment_values,
        input=b"binary\x00\xff\n",
        capture_output=True,
        creationflags=creation_flags,
        check=True,
        timeout=30,
    )
    observed = json.loads(result.stdout)
    assert observed["before"] == observed["after"] == [437, 437]
    assert observed["returncode"] == 7, observed
    assert json.loads(observed["stdout"]) == [
        arguments,
        "62696e61727900ff0a",
        str(environment),
    ]
    assert observed["stderr"] == ""


@pytest.mark.skipif(
    os.name != "nt", reason="Native Windows launcher generation failure"
)
def test_windows_generation_failure_keeps_existing_fixture_and_unrelated_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Exercise ScriptMaker's real failure path, not a replacement launcher.
    from distlib.util import FileOperator  # type: ignore[import-untyped]  # Upstream has no typing metadata.

    def fail_write(self: object, path: str, data: bytes) -> None:
        raise PermissionError("fixture-generation-denied")

    target = tmp_path / "fixture.exe"
    target.write_bytes(b"original executable")
    other = tmp_path / "fixture.exe.deleteme"
    other.write_bytes(b"unrelated data")
    monkeypatch.setattr(FileOperator, "write_binary_file", fail_write)
    with pytest.raises(PermissionError, match="fixture-generation-denied"):
        executable_fixture.write_python_executable(target, "raise SystemExit(0)\n")
    assert target.read_bytes() == b"original executable"
    assert other.read_bytes() == b"unrelated data"
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "fixture.exe",
        "fixture.exe.deleteme",
    ]


@pytest.mark.skipif(
    os.name != "posix", reason="POSIX relocatable launcher/payload pair"
)
@pytest.mark.parametrize("invocation", ["absolute", "relative", "path"])
def test_posix_fixture_pair_can_move_before_launch(
    tmp_path: Path, invocation: str
) -> None:
    original = tmp_path / "original location"
    target = original / "fixture-tool"
    executable = executable_fixture.write_python_executable(
        target, "import json,sys; print(json.dumps([sys.argv,sys.executable]))\n"
    )
    moved = tmp_path / "moved space ' quote\nline"
    original.rename(moved)
    assert not original.exists()
    relocated = moved / executable.name
    environment = dict(os.environ)
    if invocation == "absolute":
        command = str(relocated)
    elif invocation == "relative":
        command = str(relocated.relative_to(tmp_path))
    else:
        command = relocated.name
        environment["PATH"] = str(moved) + os.pathsep + environment.get("PATH", "")
    result = subprocess.run(
        [command, "argument with spaces"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    arguments, interpreter = json.loads(result.stdout)
    assert (tmp_path / arguments[0]).resolve() == Path(str(relocated) + ".py")
    assert arguments[1:] == ["argument with spaces"]
    assert interpreter == sys.executable


@pytest.mark.skipif(os.name != "nt", reason="Native Windows npm-style command wrapper")
def test_windows_cmd_opt_in_preserves_npm_entry_in_unicode_directory(
    tmp_path: Path,
) -> None:
    target = tmp_path / "工具 café %name%! 空間" / "markdownlint-cli2"
    executable = executable_fixture.write_python_executable(
        target,
        "import json,sys; print(json.dumps([sys.argv[1:],sys.stdin.read()]))\n"
        "raise SystemExit(7)\n",
        windows_cmd=True,
    )
    assert executable == target.with_suffix(".cmd")
    native = executable.with_suffix(".exe")
    assert native.is_file()
    assert executable.read_bytes().isascii()
    result = subprocess.run(
        [str(executable), "argument with spaces", "unicode 測試"],
        cwd=tmp_path,
        input="input bytes\n",
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 7, result.stderr
    assert json.loads(result.stdout) == [
        ["argument with spaces", "unicode 測試"],
        "input bytes\n",
    ]
