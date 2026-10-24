"""Starts and stops the neonize bridge for the Neonize connection engine.

The bridge (``neonize_bridge/server.py``) is one process for the whole install,
shared by every account on this machine, like the WPPConnect port is. So:

* the first client to need it starts it (hidden, detached) and writes
  ``ponte.pid``; later clients find the port open and just use it;
* every client registers itself in ``clientes/<pid>``; when one leaves it
  prunes the dead ones, and the last one out stops the bridge;
* a bridge the user started by hand has no ``ponte.pid`` and is never stopped.

Everything that touches the system (is the port open, is the pid alive, how to
spawn, how to kill) is a parameter, so the rules are tested without processes.
"""

import logging
import os
import socket
import subprocess
import sys
import time

from app_paths import resource_path

log = logging.getLogger("neonize_bridge")

PORT = 6300
START_TIMEOUT = 60.0          # seconds to wait for the port after spawning
BRIDGE_PID_FILE = "ponte.pid"
CLIENTS_DIR = "clientes"
EXTRA_PATH_FILE = "path_extra.txt"    # one folder per line, e.g. where ffprobe lives
OUTPUT_FILE = "ponte.log"
HISTORY_DAYS = "180"


def bridge_dir() -> str:
    return os.path.abspath(resource_path("..", "neonize_bridge"))


def data_dir(bridge: str) -> str:
    """Where the bridge keeps its sessions and messages (every sub-folder in
    here is a session, so nothing else may live in it)."""
    return os.path.join(bridge, "dados-real")


def run_dir(bridge: str) -> str:
    """Where this feature keeps its own files: the bridge pid, the clients
    using it and the bridge's output."""
    return os.path.join(bridge, "execucao")


def command(bridge: str, port: int = PORT):
    """The command that starts the bridge, or None if this install has none.
    From the repository: the bridge's own virtualenv. Packaged builds will
    ship a ``bridge/neonize_bridge.exe`` instead."""
    packaged = os.path.join(bridge, "neonize_bridge.exe")
    if os.path.isfile(packaged):
        return [packaged, "--porta", str(port), "--pasta", data_dir(bridge),
                "--historico-dias", HISTORY_DAYS]
    python = os.path.join(bridge, ".venv", "Scripts", "python.exe")
    script = os.path.join(bridge, "server.py")
    if os.path.isfile(python) and os.path.isfile(script):
        return [python, script, "--porta", str(port), "--pasta",
                data_dir(bridge), "--historico-dias", HISTORY_DAYS]
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


def _read_pid(path: str) -> int:
    try:
        with open(path, encoding="utf-8") as stream:
            return int(stream.read().strip())
    except (OSError, ValueError):
        return 0


class NeonizeBridge:
    """One client's handle on the shared bridge."""

    def __init__(self, bridge=None, port=PORT, *, is_open=port_open,
                 alive=pid_alive, start_process=spawn, kill=kill_tree,
                 sleep=time.sleep, clock=time.monotonic, my_pid=None):
        self.bridge = bridge or bridge_dir()
        self.port = port
        self._is_open, self._alive = is_open, alive
        self._spawn, self._kill = start_process, kill
        self._sleep, self._clock = sleep, clock
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

    # -- life cycle
    def start(self) -> bool:
        """Make sure the bridge answers on its port. False if it could not be
        started (no bridge in this install, or it never came up)."""
        self._register()
        if self._is_open(self.port):
            return True
        cmd = command(self.bridge, self.port)
        if cmd is None:
            log.error("no neonize bridge to start in %s", self.bridge)
            return False
        os.makedirs(data_dir(self.bridge), exist_ok=True)
        os.makedirs(run_dir(self.bridge), exist_ok=True)
        self.proc = self._spawn(cmd, environment(self.bridge), self.bridge,
                                os.path.join(run_dir(self.bridge), OUTPUT_FILE))
        with open(self._pid_file, "w", encoding="utf-8") as stream:
            stream.write(str(self.proc.pid))
        deadline = self._clock() + START_TIMEOUT
        while self._clock() < deadline:
            if self._is_open(self.port):
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
        bridge_pid = _read_pid(self._pid_file)
        if not bridge_pid or not self._alive(bridge_pid):
            return False
        self._kill(bridge_pid)
        try:
            os.remove(self._pid_file)
        except OSError:
            pass
        log.info("neonize bridge stopped (pid %s)", bridge_pid)
        return True
