"""Freezes the Neonize bridge (``neonize_bridge/``) so a compiled build carries it.

The bridge runs under its own virtualenv (``neonize_bridge/.venv``, made by
``neonize_bridge/prepare.py``, which also installs the reviewed test build of
neonize). A compiled WinZapp has no such virtualenv, so the build freezes the
bridge with PyInstaller, in that same virtualenv, into ``neonize_bridge.exe`` +
``_internal/``; the client already starts a ``neonize_bridge.exe`` next to it when
there is one (``client/core/neonize_bridge_process.py``).

Everything that touches the system is a parameter, so the rules are tested
without running PyInstaller.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

BRIDGE_FOLDER = "neonize_bridge"
EXE_NAME = "neonize_bridge.exe"

#: python-socketio loads its aiohttp driver by name at run time, so PyInstaller's
#: static analysis does not see it.
HIDDEN_IMPORTS = ("engineio.async_drivers.aiohttp",)
COLLECTED_SUBMODULES = ("engineio.async_drivers",)
#: neonize carries the Go DLL and the generated protobuf modules.
COLLECTED_PACKAGES = ("neonize",)


class BridgeBuildError(RuntimeError):
    """The bridge could not be frozen; the build must not ship without it."""


def bridge_dir(root: Path) -> Path:
    return Path(root) / BRIDGE_FOLDER


def venv_python(root: Path) -> Path:
    return bridge_dir(root) / ".venv" / "Scripts" / "python.exe"


def frozen_dir(out_root: Path) -> Path:
    return Path(out_root) / "dist" / "neonize_bridge"


def freeze_command(python: Path, root: Path, out_root: Path) -> list:
    """The PyInstaller command line that freezes ``server.py``."""
    out_root = Path(out_root)
    command = [str(python), "-m", "PyInstaller", "--noconfirm", "--onedir",
               "--name", "neonize_bridge",
               "--distpath", str(out_root / "dist"),
               "--workpath", str(out_root / "work"),
               "--specpath", str(out_root / "spec"),
               "--paths", str(bridge_dir(root))]
    for package in COLLECTED_PACKAGES:
        command += ["--collect-all", package]
    for package in COLLECTED_SUBMODULES:
        command += ["--collect-submodules", package]
    for module in HIDDEN_IMPORTS:
        command += ["--hidden-import", module]
    command.append(str(bridge_dir(root) / "server.py"))
    return command


def pyinstaller_requirements(pyinstaller_version: str, hooks_version: str) -> list:
    """The same PyInstaller as the client build, so one version is homologated."""
    return [f"pyinstaller=={pyinstaller_version}", f"pyinstaller-hooks-contrib=={hooks_version}"]


def build(root: Path, out_root: Path, pyinstaller_version: str, hooks_version: str,
          *, run=subprocess.run, base_python: str = sys.executable, log=print) -> Path:
    """Freezes the bridge under ``out_root`` and returns the folder that holds
    ``neonize_bridge.exe``. Creates the bridge virtualenv first when missing."""
    root, out_root = Path(root), Path(out_root)
    python = venv_python(root)
    if not python.is_file():
        log("  [bridge] no bridge virtualenv; running neonize_bridge/prepare.py")
        run([base_python, str(bridge_dir(root) / "prepare.py")], check=True)
    if not python.is_file():
        raise BridgeBuildError(f"{python} is missing after prepare.py")
    run([str(python), "-m", "pip", "install", "--quiet",
         *pyinstaller_requirements(pyinstaller_version, hooks_version)], check=True)
    if out_root.exists():
        shutil.rmtree(out_root)
    run(freeze_command(python, root, out_root), check=True, cwd=str(bridge_dir(root)))
    result = frozen_dir(out_root)
    if not (result / EXE_NAME).is_file():
        raise BridgeBuildError(f"PyInstaller did not produce {result / EXE_NAME}")
    return result


def stage(frozen: Path, staging: Path) -> int:
    """Copies the frozen bridge into ``<staging>/neonize_bridge``; returns the
    number of files."""
    destination = Path(staging) / BRIDGE_FOLDER
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(frozen, destination)
    return sum(1 for item in destination.rglob("*") if item.is_file())
