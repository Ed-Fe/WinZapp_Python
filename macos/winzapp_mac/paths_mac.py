"""Where the Mac app keeps things.

A frozen Windows WinZapp writes data/ next to its .exe and ships the Node
server (api/) and runtime (node/) beside it. Inside a Mac .app bundle that
would put the account data and the paired WhatsApp session inside the app,
wiped by every update. So, when frozen on macOS:

  ~/Library/Application Support/WinZapp/data/          accounts, messages, session
  ~/Library/Application Support/WinZapp/runtime/api    WPPConnect server
  ~/Library/Application Support/WinZapp/runtime/node   Node.js

The app carries its own runtime in Contents/Resources/runtime (built by
macos/build_app.py, stamped with a build id). At launch, if the installed
runtime's stamp differs, the bundled one is cloned into Application Support
(APFS clone: about a second, no extra disk), keeping the folders the server
writes to. The server writes next to itself, so it must not run from inside
the signed app. Dev runs (macos/launcher.py) keep upstream's behaviour.
"""

import logging
import os
import shutil
import signal
import subprocess
import sys

SUPPORT = os.path.expanduser("~/Library/Application Support/WinZapp")
RUNTIME = os.path.join(SUPPORT, "runtime")
_RUNTIME_PARTS = ("api", "node")
STAMP = "STAMP"
# Written by the server at run time; carried over when the runtime updates.
KEEP_IN_API = ("log", "uploads", "WhatsAppImages", "wppconnect_tokens", "tokens", "userDataDir")


def bundled_runtime():
    if not hasattr(sys, "_MEIPASS"):
        return None
    path = os.path.join(os.path.dirname(sys._MEIPASS), "Resources", "runtime")
    return path if os.path.isfile(os.path.join(path, STAMP)) else None


def _read(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def _clone_tree(src, dst):
    """APFS copy-on-write clone; a plain copy where cloning isn't possible."""
    r = subprocess.run(["cp", "-cR", src, dst], capture_output=True)
    if r.returncode != 0:
        if os.path.exists(dst):
            shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst, symlinks=True)


def executable_of(pid):
    """Full path of the executable process *pid* runs (libproc), or None."""
    import ctypes
    import ctypes.util
    try:
        libproc = ctypes.CDLL(ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib")
        buf = ctypes.create_string_buffer(4096)         # PROC_PIDPATHINFO_MAXSIZE
        n = libproc.proc_pidpath(int(pid), buf, ctypes.sizeof(buf))
    except (OSError, AttributeError, ValueError):
        return None
    return os.fsdecode(buf.raw[:n]) if n > 0 else None


def runtime_node_pids(node_path):
    """This user's processes whose executable IS *node_path* — not every
    process that merely mentions the path on its command line."""
    try:
        out = subprocess.run(["pgrep", "-U", str(os.getuid()), "-f", node_path],
                             capture_output=True, text=True).stdout.split()
    except Exception:
        return []
    want = os.path.realpath(node_path)
    pids = []
    for pid in out:
        try:
            pid = int(pid)
        except ValueError:
            continue
        exe = executable_of(pid)
        if pid != os.getpid() and exe and os.path.realpath(exe) == want:
            pids.append(pid)
    return pids


def _stop_stale_node(node_path=None, grace=3.0):
    """A Node left running from the runtime being replaced (only after a
    crash — a normal quit stops it) would keep serving the old code. Asked
    to quit first; one still running after *grace* seconds (checked again,
    so a reused pid is never hit) is killed."""
    import time
    node_path = node_path or os.path.join(RUNTIME, "node", "node")
    pids = runtime_node_pids(node_path)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + grace
    while pids and time.monotonic() < deadline:
        time.sleep(0.1)
        pids = [pid for pid in pids if pid in runtime_node_pids(node_path)]
    for pid in pids:
        logging.warning("[paths_mac] stale Node %s ignored SIGTERM; killing it", pid)
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def sync_runtime(bundle=None, runtime=RUNTIME):
    """Install the bundled runtime if it differs from the one in place.
    Returns True when it replaced (or first installed) the runtime."""
    bundle = bundle or bundled_runtime()
    if not bundle:
        return False
    import fcntl
    os.makedirs(os.path.dirname(runtime), exist_ok=True)
    # Several accounts' processes can start together; one installs.
    with open(runtime + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _sync_locked(bundle, runtime)


def _sync_locked(bundle, runtime):
    want = _read(os.path.join(bundle, STAMP))
    if want and _read(os.path.join(runtime, STAMP)) == want:
        return False
    logging.info("[paths_mac] installing bundled runtime %s", want)
    _stop_stale_node()
    new, old = runtime + ".new", runtime + ".old"
    for p in (new, old):
        if os.path.exists(p):
            shutil.rmtree(p, ignore_errors=True)
    os.makedirs(os.path.dirname(runtime), exist_ok=True)
    _clone_tree(bundle, new)
    for name in KEEP_IN_API:
        src = os.path.join(runtime, "api", name)
        if os.path.exists(src):
            dst = os.path.join(new, "api", name)
            if os.path.exists(dst):
                shutil.rmtree(dst, ignore_errors=True)
            os.rename(src, dst)
    if os.path.exists(runtime):
        os.rename(runtime, old)
    os.rename(new, runtime)
    shutil.rmtree(old, ignore_errors=True)
    return True


def install():
    if not getattr(sys, "frozen", False):
        return
    import app_paths

    orig_resource_path = app_paths.resource_path

    def resource_path(*parts):
        if parts and parts[0] in _RUNTIME_PARTS:
            return os.path.join(RUNTIME, *parts)
        return orig_resource_path(*parts)

    def writable_base_dir():
        os.makedirs(SUPPORT, exist_ok=True)
        return SUPPORT

    app_paths.resource_path = resource_path
    app_paths._writable_base_dir = writable_base_dir

    # platform_utils assumes a py2app layout (<exe>/Contents/MacOS) for
    # frozen Mac apps; sound_lib uses it to find its dylibs. PyInstaller
    # puts bundled files under sys._MEIPASS.
    from platform_utils import paths as pu_paths
    pu_paths.embedded_data_path = lambda: sys._MEIPASS

    try:
        sync_runtime()
    except Exception:
        logging.exception("[paths_mac] could not install the bundled runtime")
