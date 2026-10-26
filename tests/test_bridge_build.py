"""Freezing the Neonize bridge for a compiled build: the rules, without PyInstaller."""

from pathlib import Path

import pytest

from winzapp_tools import bridge_build as bb


def _repo(tmp_path, with_venv=True):
    root = tmp_path / "repo"
    bridge = root / "neonize_bridge"
    bridge.mkdir(parents=True)
    (bridge / "server.py").write_text("")
    (bridge / "prepare.py").write_text("")
    if with_venv:
        (bridge / ".venv" / "Scripts").mkdir(parents=True)
        (bridge / ".venv" / "Scripts" / "python.exe").write_text("")
    return root


class _Runner:
    """Records the commands; the PyInstaller one drops the exe, prepare.py the venv."""

    def __init__(self, root, produce_exe=True):
        self.root, self.produce_exe, self.calls = root, produce_exe, []

    def __call__(self, command, check=True, cwd=None):
        self.calls.append((list(command), cwd))
        if command[-1].endswith("prepare.py"):
            scripts = Path(self.root) / "neonize_bridge" / ".venv" / "Scripts"
            scripts.mkdir(parents=True)
            (scripts / "python.exe").write_text("")
        if "PyInstaller" in command and self.produce_exe:
            out = Path(command[command.index("--distpath") + 1]) / "neonize_bridge"
            out.mkdir(parents=True)
            (out / bb.EXE_NAME).write_text("exe")
            (out / "_internal").mkdir()
            (out / "_internal" / "neonize.dll").write_text("dll")


def test_the_command_freezes_server_py_with_what_pyinstaller_cannot_see(tmp_path):
    root = _repo(tmp_path)
    command = bb.freeze_command(bb.venv_python(root), root, tmp_path / "out")
    assert command[1:3] == ["-m", "PyInstaller"] and "--onedir" in command
    assert command[command.index("--name") + 1] == "neonize_bridge"
    assert command[command.index("--collect-all") + 1] == "neonize"
    assert "engineio.async_drivers.aiohttp" in command
    assert command[-1].endswith("server.py")
    # the bridge's modules are found next to server.py
    assert command[command.index("--paths") + 1] == str(bb.bridge_dir(root))


def test_it_installs_the_same_pyinstaller_as_the_client_build(tmp_path):
    root = _repo(tmp_path)
    run = _Runner(root)
    bb.build(root, tmp_path / "out", "6.21.0", "2026.6", run=run, log=lambda *_: None)
    install = next(c for c, _ in run.calls if "pip" in c)
    assert "pyinstaller==6.21.0" in install and "pyinstaller-hooks-contrib==2026.6" in install
    assert install[0] == str(bb.venv_python(root))      # in the bridge's environment


def test_a_missing_bridge_environment_is_prepared_first(tmp_path):
    root = _repo(tmp_path, with_venv=False)
    run = _Runner(root)
    result = bb.build(root, tmp_path / "out", "6.21.0", "2026.6", run=run,
                      base_python="build-python", log=lambda *_: None)
    assert run.calls[0][0][0] == "build-python" and run.calls[0][0][1].endswith("prepare.py")
    assert (result / bb.EXE_NAME).is_file()


def test_an_existing_environment_is_not_prepared_again(tmp_path):
    root = _repo(tmp_path)
    run = _Runner(root)
    bb.build(root, tmp_path / "out", "6.21.0", "2026.6", run=run, log=lambda *_: None)
    assert not any(c[-1].endswith("prepare.py") for c, _ in run.calls)


def test_a_build_without_the_exe_is_a_failure_not_a_silent_skip(tmp_path):
    root = _repo(tmp_path)
    with pytest.raises(bb.BridgeBuildError):
        bb.build(root, tmp_path / "out", "6.21.0", "2026.6",
                 run=_Runner(root, produce_exe=False), log=lambda *_: None)


def test_staging_copies_the_exe_and_its_runtime_under_neonize_bridge(tmp_path):
    root = _repo(tmp_path)
    frozen = bb.build(root, tmp_path / "out", "6.21.0", "2026.6", run=_Runner(root),
                      log=lambda *_: None)
    staging = tmp_path / "staging"
    (staging / "neonize_bridge").mkdir(parents=True)
    (staging / "neonize_bridge" / "stale.txt").write_text("old")
    assert bb.stage(frozen, staging) == 2
    assert (staging / "neonize_bridge" / bb.EXE_NAME).is_file()
    assert (staging / "neonize_bridge" / "_internal" / "neonize.dll").is_file()
    assert not (staging / "neonize_bridge" / "stale.txt").exists()
