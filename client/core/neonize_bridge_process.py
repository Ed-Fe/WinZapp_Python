"""Starts and stops the neonize bridge for the Neonize connection engine.

The bridge (``neonize_bridge/server.py``) is one process for the whole install,
shared by every account on this machine, like the WPPConnect port is. So:

* the first client to need it starts it (hidden, detached) and writes
  ``bridge.pid``; later clients find the port open and just use it;
* every client registers itself in ``clients/<pid>``; when one leaves it
  prunes the dead ones, and the last one out stops the bridge;
* a bridge the user started by hand has no ``bridge.pid`` and is never stopped.

``bridge.pid`` holds the bridge's pid and, on the next line, when that process
was created: a pid alone could name some other program after a reboot or a
crash, and the last client out would kill it. Without a matching creation time
nothing is killed.

A listener on the port is not trusted just for being there: the client is about
to hand it the bridge's secret. Before using an already-open port the client asks
the bridge's open route ``/winzapp/identity`` who it is and compares the answer
with ``bridge.pid`` (``identity_matches``); a program that merely took port 6300
is refused.

Everything that touches the system (is the port open, is the pid alive, when
the process was created, how to spawn, how to kill, what the listener says it
is) is a parameter, so the rules are tested without processes.
"""

import logging
import os
import socket
import subprocess
import sys
import time

from app_paths import resource_path
from coord_locks import LockTimeout, neonize_bridge_lock
from core import neonize_bridge_layout

log = logging.getLogger("neonize_bridge")

PORT = 6300
START_TIMEOUT = 60.0          # seconds to wait for the port after spawning
BRIDGE_PID_FILE = "bridge.pid"
CLIENTS_DIR = "clients"
EXTRA_PATH_FILE = "path_extra.txt"    # one folder per line, e.g. where ffprobe lives
OUTPUT_FILE = "bridge.log"
OUTPUT_LIMIT = 5 * 1024 * 1024   # bytes; past this the next start keeps one older copy
HISTORY_DAYS = "180"
SECRET_FILE = "secret"         # written by the bridge in its data folder


def bridge_dir() -> str:
    return os.path.abspath(resource_path("..", "neonize_bridge"))


def data_dir(bridge: str) -> str:
    """Where the bridge keeps its sessions and messages (every sub-folder in
    here is a session, so nothing else may live in it)."""
    return os.path.join(bridge, "data")


def read_secret(bridge: str) -> str:
    """The bridge's secret (the key the client presents to it), or "" when the
    bridge has not written one: a bridge started by hand with another --folder."""
    try:
        with open(os.path.join(data_dir(bridge), SECRET_FILE), encoding="utf-8") as stream:
            return stream.read().strip()
    except OSError:
        return ""


def run_dir(bridge: str) -> str:
    """Where this feature keeps its own files: the bridge pid, the clients
    using it and the bridge's output."""
    return os.path.join(bridge, "run")


def command(bridge: str, port: int = PORT):
    """The command that starts the bridge, or None if this install has none.
    From the repository: the bridge's own virtualenv. Packaged builds will
    ship a ``bridge/neonize_bridge.exe`` instead."""
    packaged = os.path.join(bridge, "neonize_bridge.exe")
    if os.path.isfile(packaged):
        return [packaged, "--port", str(port), "--folder", data_dir(bridge),
                "--history-days", HISTORY_DAYS]
    python = os.path.join(bridge, ".venv", "Scripts", "python.exe")
    script = os.path.join(bridge, "server.py")
    if os.path.isfile(python) and os.path.isfile(script):
        return [python, script, "--port", str(port), "--folder",
                data_dir(bridge), "--history-days", HISTORY_DAYS]
    return None


def environment(bridge: str, base_env=None) -> dict:
    """The bridge's environment: ffmpeg/ffprobe on PATH (client/lib, plus the
    folders listed in neonize_bridge/path_extra.txt)."""
    env = dict(os.environ if base_env is None else base_env)
    folders = [resource_path("lib")]
    extra = os.path.join(bridge, EXTRA_PATH_FILE)
    if os.path.isfile(extra):
        with open(extra, encoding="utf-8") as stream:
            folders += [line.strip() for line in stream if line.strip()]
    env["PATH"] = os.pathsep.join(
        [f for f in folders if os.path.isdir(f)] + [env.get("PATH", "")])
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def port_open(port: int = PORT, host: str = "127.0.0.1", timeout=0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    import ctypes
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259                        # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def process_created(pid: int) -> str:
    """When the process was created, as a string that only means something
    next to the same pid ("" if it cannot be read, or off Windows, where the
    bridge is never started by this feature)."""
    if sys.platform != "win32" or pid <= 0:
        return ""
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        created, ignored = wintypes.FILETIME(), wintypes.FILETIME()
        if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(ignored),
                                        ctypes.byref(ignored), ctypes.byref(ignored)):
            return ""
        return str((created.dwHighDateTime << 32) | created.dwLowDateTime)
    finally:
        kernel32.CloseHandle(handle)


IDENTITY_TIMEOUT = 3.0


def fetch_identity(port: int = PORT, host: str = "127.0.0.1",
                   timeout: float = IDENTITY_TIMEOUT):
    """What the listener on ``port`` answers at ``/winzapp/identity`` (an open
    route: nothing secret is sent), or None when it does not answer like a
    bridge. Proxies are ignored: this is a loopback call."""
    import requests
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(f"http://{host}:{port}/winzapp/identity",
                                   timeout=timeout)
            body = response.json() if response.status_code == 200 else None
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def _is_number(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def identity_matches(identity, expected_pid: int) -> bool:
    """Whether the listener's ``/winzapp/identity`` answer is the bridge this
    client expects.

    ``expected_pid`` is the pid ``bridge.pid`` names (0 when there is none to
    trust: a bridge started by hand, or a stale file). With a pid, the listener
    must be that process or its child: on Windows what the client launches (the
    virtualenv's python.exe, the packaged exe) can be a launcher that runs the
    real bridge as a child process, and the bridge reports its parent for that.
    Without one, the best that can be asked is that it answers in the bridge's
    shape. Pure."""
    if not isinstance(identity, dict):
        return False
    pid, parent = identity.get("pid"), identity.get("ppid")
    if not _is_number(identity.get("protocol_version")) or not _is_number(pid):
        return False
    if expected_pid <= 0:
        return True
    return pid == expected_pid or (_is_number(parent) and parent == expected_pid)


def rotate_output(path: str, limit: int = OUTPUT_LIMIT, *,
                  getsize=os.path.getsize, replace=os.replace) -> bool:
    """Keep the bridge's output file from growing forever: past ``limit`` bytes
    it becomes ``<path>.1`` (replacing the previous copy) and the next start
    writes a fresh file. Returns whether it rotated."""
    try:
        if getsize(path) <= limit:
            return False
        replace(path, path + ".1")
    except OSError:                # no file yet, or another program holds it
        return False
    return True


def spawn(cmd, env, cwd, output=None):
    """Start the bridge hidden, its output going to the ``output`` file."""
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    sink = open(output, "ab") if output else subprocess.DEVNULL
    try:
        return subprocess.Popen(
            cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=sink, stderr=subprocess.STDOUT, creationflags=flags)
    finally:
        if output:
            sink.close()


def kill_tree(pid: int):
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        os.kill(pid, 15)


def _read_pid_file(path: str):
    """(pid, creation stamp) from bridge.pid; (0, "") if unreadable, and an
    empty stamp for a file written without one."""
    try:
        with open(path, encoding="utf-8") as stream:
            lines = stream.read().split()
        return int(lines[0]), (lines[1] if len(lines) > 1 else "")
    except (OSError, ValueError, IndexError):
        return 0, ""


class NeonizeBridge:
    """One client's handle on the shared bridge."""

    def __init__(self, bridge=None, port=PORT, *, is_open=port_open,
                 alive=pid_alive, created=process_created, rotate=rotate_output,
                 start_process=spawn, kill=kill_tree, sleep=time.sleep, clock=time.monotonic, my_pid=None,
                 lock=neonize_bridge_lock, identity=fetch_identity):
        self.bridge = bridge or bridge_dir()
        self.port = port
        self._is_open, self._alive = is_open, alive
        self._created, self._rotate = created, rotate
        self._spawn, self._kill = start_process, kill
        self._sleep, self._clock = sleep, clock
        self._lock = lock
        self._identity = identity
        self.pid = my_pid or os.getpid()
        self.proc = None

    # -- files
    @property
    def _pid_file(self):
        return os.path.join(run_dir(self.bridge), BRIDGE_PID_FILE)

    @property
    def _clients(self):
        return os.path.join(run_dir(self.bridge), CLIENTS_DIR)

    def _register(self):
        os.makedirs(self._clients, exist_ok=True)
        with open(os.path.join(self._clients, str(self.pid)), "w") as stream:
            stream.write(str(self.pid))

    def _live_clients(self):
        """Pids of the clients still running, pruning the dead ones."""
        live = []
        try:
            names = os.listdir(self._clients)
        except OSError:
            return live
        for name in names:
            path = os.path.join(self._clients, name)
            pid = int(name) if name.isdigit() else 0
            if pid == self.pid:
                continue
            if self._alive(pid):
                live.append(pid)
            else:
                try:
                    os.remove(path)
                except OSError:
                    pass
        return live

    def _expected_bridge_pid(self) -> int:
        """The pid ``bridge.pid`` names, if it can still be believed: the process
        is alive and (where the creation time can be read) it is the one that was
        recorded. 0 means there is no bridge of ours to compare with."""
        pid, stamp = _read_pid_file(self._pid_file)
        if not pid or not self._alive(pid):
            return 0
        now = self._created(pid)
        if stamp and now and now != stamp:
            return 0
        return pid

    def _listener_is_the_bridge(self, expected_pid=None) -> bool:
        """The port is open: is what listens on it the bridge, before the
        client sends it the secret? ``expected_pid`` is the process this client
        just spawned; otherwise the one ``bridge.pid`` names. Logs only the
        verdict, never what the listener said."""
        if expected_pid is None:
            expected_pid = self._expected_bridge_pid()
        try:
            answer = self._identity(self.port)
        except Exception:
            answer = None
        if identity_matches(answer, expected_pid):
            return True
        log.error("port %s is taken by a program that is not the neonize bridge "
                  "(answer type: %s); refusing to use it", self.port, type(answer).__name__)
        return False

    def _migrate_legacy_layout(self):
        """Renames folders written under the old names. A bridge of an older
        WinZapp that is still running (verified like bridge.pid is) is stopped first
        when no old client uses it; otherwise nothing is renamed."""
        pid, stamp = _read_pid_file(neonize_bridge_layout.legacy_pid_file(self.bridge))
        running = False
        if pid and self._alive(pid):
            now = self._created(pid)
            running = not (stamp and now and now != stamp)    # else the pid was reused
        if running:
            clients = neonize_bridge_layout.legacy_clients_dir(self.bridge)
            try:
                names = os.listdir(clients)
            except OSError:
                names = []
            if not any(n.isdigit() and int(n) != self.pid and self._alive(int(n)) for n in names):
                self._kill(pid)
                running = False
        neonize_bridge_layout.migrate(self.bridge, running)

    # -- life cycle
    def start(self) -> bool:
        """Make sure the bridge answers on its port. False if it could not be
        started (no bridge in this install, or it never came up)."""
        cmd = command(self.bridge, self.port)
        # Folders written under the old names are renamed first (only while no bridge runs).
        self._migrate_legacy_layout()
        if neonize_bridge_layout.stranded(self.bridge):
            log.error("neonize bridge not started: its data folder could not be renamed; "
                      "starting now would ask for pairing again")
            return False
        # Before anything is written: an install with no bridge (a packaged
        # build without one) must not leave client files behind.
        if cmd is None and not self._is_open(self.port):
            log.error("no neonize bridge to start in %s", self.bridge)
            return False
        self._register()
        if self._is_open(self.port):
            return self._listener_is_the_bridge()
        os.makedirs(data_dir(self.bridge), exist_ok=True)
        os.makedirs(run_dir(self.bridge), exist_ok=True)
        # Two accounts starting together (at login) must not both find the port
        # closed and spawn: the loser fails to bind, bridge.pid may name the dead
        # one, and the last client out then never stops the real bridge.
        try:
            with self._lock(run_dir(self.bridge)):
                if self._is_open(self.port):       # the other client got there first
                    return self._listener_is_the_bridge()
                return self._spawn_and_wait(cmd)
        except LockTimeout:
            log.error("neonize bridge start: another client held the start lock too long")
            return False

    def _spawn_and_wait(self, cmd) -> bool:
        output = os.path.join(run_dir(self.bridge), OUTPUT_FILE)
        self._rotate(output)
        self.proc = self._spawn(cmd, environment(self.bridge), self.bridge, output)
        with open(self._pid_file, "w", encoding="utf-8") as stream:
            stream.write(f"{self.proc.pid}\n{self._created(self.proc.pid)}")
        deadline = self._clock() + START_TIMEOUT
        while self._clock() < deadline:
            if self._is_open(self.port):
                # Our process may have lost the bind to a program that took the
                # port meanwhile: the port is open, but not with our bridge.
                if self.proc.poll() is not None:
                    log.error("neonize bridge exited with code %s but port %s is open "
                              "(taken by another program)", self.proc.returncode, self.port)
                    return False
                if not self._listener_is_the_bridge(self.proc.pid):
                    return False
                log.info("neonize bridge up on port %s (pid %s)", self.port, self.proc.pid)
                return True
            if self.proc.poll() is not None:
                log.error("neonize bridge exited early with code %s", self.proc.returncode)
                return False
            self._sleep(0.5)
        log.error("neonize bridge did not open port %s in time", self.port)
        return False

    def stop(self) -> bool:
        """Leave. The last client out stops a bridge this feature started.
        Returns whether the bridge was stopped."""
        try:
            os.remove(os.path.join(self._clients, str(self.pid)))
        except OSError:
            pass
        if self._live_clients():
            return False
        bridge_pid, stamp = _read_pid_file(self._pid_file)
        if not bridge_pid or not self._alive(bridge_pid):
            return False
        now = self._created(bridge_pid)
        if not stamp or not now:
            log.warning("neonize bridge pid %s not verified, left running", bridge_pid)
            return False
        if now != stamp:               # the pid now belongs to another program
            log.info("neonize bridge pid %s was reused, nothing to stop", bridge_pid)
            try:
                os.remove(self._pid_file)
            except OSError:
                pass
            return False
        self._kill(bridge_pid)
        try:
            os.remove(self._pid_file)
        except OSError:
            pass
        log.info("neonize bridge stopped (pid %s)", bridge_pid)
        return True
