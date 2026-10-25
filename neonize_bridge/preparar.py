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
import posixpath
import subprocess
import sys
import urllib.parse
import urllib.request
import zipfile

BUILD_URL = ("https://github.com/reffehcsoibaf/neonize/releases/download/"
             "0.5.2-winzapp-teste.2/neonize-0.5.2-winzapp-teste.zip")
BUILD_SHA256 = "06f6cba995d9ea021f85bc3179c004a8e39f3c4707f117cb0372a5efef8d117a"

# The build replaces files of the neonize package and nothing else. The
# hash above proves the archive is the one that was reviewed; these limits
# make sure that even a wrong archive cannot drop a `.pth` file or a
# `sitecustomize.py` (both run on every start of the bridge's Python) or
# anything outside `neonize/` into site-packages.
PACKAGE = "neonize"
ALLOWED_EXTENSIONS = (".py", ".pyi", ".pyc", ".dll", ".typed")
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
MAX_UNPACKED_BYTES = 400 * 1024 * 1024
MAX_MEMBERS = 2000
ALLOWED_HOST_SUFFIXES = ("github.com", "githubusercontent.com")

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


def member_problem(info) -> str:
    """Why this archive member may not be extracted, or "" when it may."""
    name = info.filename
    if "\\" in name or name.startswith("/") or ":" in name:
        return f"{name}: not a plain relative path"
    parts = name.split("/")
    if ".." in parts or "" in parts[:-1]:
        return f"{name}: leaves its folder"
    if parts[0] != PACKAGE:
        return f"{name}: outside {PACKAGE}/"
    if (info.external_attr >> 16) & 0o170000 == 0o120000:
        return f"{name}: a symbolic link"
    if info.is_dir():
        return ""
    if not name.lower().endswith(ALLOWED_EXTENSIONS):
        return f"{name}: a file type the neonize package does not have"
    return ""


def check_members(infos) -> list:
    """Every reason the archive must be refused (empty list: it is fine)."""
    infos = list(infos)
    problems = [p for p in map(member_problem, infos) if p]
    if len(infos) > MAX_MEMBERS:
        problems.append(f"{len(infos)} members (more than {MAX_MEMBERS})")
    if sum(i.file_size for i in infos) > MAX_UNPACKED_BYTES:
        problems.append("unpacked size is too large")
    return problems


def extract_checked(archive, target):
    """Extract an archive that passed check_members(), one member at a time,
    each into a path that is proven to stay inside `target`."""
    problems = check_members(archive.infolist())
    if problems:
        sys.exit("The neonize build is not a plain neonize package:\n  "
                 + "\n  ".join(problems[:10]))
    root = os.path.realpath(target)
    written = 0
    for info in archive.infolist():
        destination = os.path.realpath(
            os.path.join(root, *posixpath.normpath(info.filename).split("/")))
        if os.path.commonpath([root, destination]) != root:
            sys.exit(f"Refusing to extract {info.filename} outside {target}.")
        if info.is_dir():
            os.makedirs(destination, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        with archive.open(info) as source, open(destination, "wb") as out:
            # The sizes in the archive can lie: count what is really written.
            while chunk := source.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_UNPACKED_BYTES:
                    sys.exit("The neonize build unpacks to more than expected.")
                out.write(chunk)


def host_allowed(url: str) -> bool:
    host = urllib.parse.urlparse(url).hostname or ""
    return urllib.parse.urlparse(url).scheme == "https" and any(
        host == suffix or host.endswith("." + suffix) for suffix in ALLOWED_HOST_SUFFIXES)


def download_build() -> bytes:
    print("Downloading the neonize test build...")
    with urllib.request.urlopen(BUILD_URL, timeout=120) as response:
        if not host_allowed(response.geturl()):
            sys.exit(f"The download was redirected to an unexpected address ({response.geturl()}).")
        data = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(data) > MAX_DOWNLOAD_BYTES:
        sys.exit("The download is larger than expected.")
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
        extract_checked(archive, target)
    print(f"Test build of neonize installed in {target}.")
    print("Done. WinZapp starts the bridge by itself when the Neonize engine is chosen "
          "(Settings, Connection).")
    print("Voice, video and stickers also need ffmpeg and ffprobe: ffmpeg is in client/lib; "
          "for ffprobe list its folder in neonize_bridge/path_extra.txt (one folder per line).")


if __name__ == "__main__":
    main()
