"""Prepares the Neonize bridge to run: a virtualenv, its requirements and the
test build of neonize that the bridge needs.

    python neonize_bridge/preparar.py

The bridge uses events and functions that exist only in the test build of
neonize 0.5.2 published at
https://github.com/reffehcsoibaf/neonize/releases/tag/0.5.2-winzapp-teste.2
(they are open pull requests to neonize). This script installs neonize 0.5.2
and extracts that build over it, after checking its SHA-256.

Windows x64 only. Standard library only, so it runs before anything is
installed. Run it again to repair the install.
"""

import hashlib
import io
import os
import subprocess
import sys
import urllib.request
import zipfile

BUILD_URL = ("https://github.com/reffehcsoibaf/neonize/releases/download/"
             "0.5.2-winzapp-teste.2/neonize-0.5.2-winzapp-teste.zip")
BUILD_SHA256 = "06f6cba995d9ea021f85bc3179c004a8e39f3c4707f117cb0372a5efef8d117a"

HERE = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(HERE, ".venv")
PYTHON = os.path.join(VENV, "Scripts", "python.exe")


def run(*cmd):
    subprocess.run(cmd, check=True)


def site_packages() -> str:
    out = subprocess.run(
        [PYTHON, "-c", "import sysconfig;print(sysconfig.get_paths()['purelib'])"],
        check=True, capture_output=True, text=True)
    return out.stdout.strip()


def download_build() -> bytes:
    print("Downloading the neonize test build...")
    with urllib.request.urlopen(BUILD_URL, timeout=120) as response:
        data = response.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != BUILD_SHA256:
        sys.exit(f"The downloaded build does not match the expected SHA-256 ({digest}).")
    return data


def main():
    if sys.platform != "win32":
        sys.exit("The Neonize bridge build is Windows x64 only.")
    if not os.path.isfile(PYTHON):
        print("Creating the virtualenv...")
        run(sys.executable, "-m", "venv", VENV)
    print("Installing requirements...")
    run(PYTHON, "-m", "pip", "install", "-r", os.path.join(HERE, "requirements.txt"))
    data = download_build()
    target = site_packages()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        archive.extractall(target)
    print(f"Test build of neonize installed in {target}.")
    print("Done. WinZapp starts the bridge by itself when the Neonize engine is chosen "
          "(Settings, Connection).")
    print("Voice, video and stickers also need ffmpeg and ffprobe: ffmpeg is in client/lib; "
          "for ffprobe list its folder in neonize_bridge/path_extra.txt (one folder per line).")


if __name__ == "__main__":
    main()
