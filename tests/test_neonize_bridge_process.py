"""Starting and stopping the neonize bridge: the rules, with no real process."""

import os

from core import neonize_bridge_process as nb


class _Proc:
    def __init__(self, pid=900, exits=None):
        self.pid, self._exits, self.returncode = pid, exits, exits

    def poll(self):
        return self._exits


def _bridge(tmp_path, with_python=True):
    root = tmp_path / "neonize_bridge"
    (root / ".venv" / "Scripts").mkdir(parents=True)
    if with_python:
        (root / ".venv" / "Scripts" / "python.exe").write_text("")
        (root / "server.py").write_text("")
    return str(root)


def _handle(bridge, *, port_open=False, alive=lambda pid: False, spawned=None,
            killed=None, proc=None, my_pid=100, created=lambda pid: f"t{pid}",
            rotate=lambda path: False):
    spawned = [] if spawned is None else spawned
    killed = [] if killed is None else killed

    def start_process(cmd, env, cwd, output=None):
        spawned.append((cmd, cwd))
        return proc or _Proc()

    clock = iter(range(0, 10_000))
    return nb.NeonizeBridge(
        bridge, 6300, is_open=lambda port: port_open() if callable(port_open) else port_open,
        alive=alive, created=created, rotate=rotate, start_process=start_process, kill=killed.append,
        sleep=lambda s: None, clock=lambda: next(clock), my_pid=my_pid)


def test_the_command_uses_the_bridge_virtualenv_and_the_fixed_port(tmp_path):
    bridge = _bridge(tmp_path)
    cmd = nb.command(bridge)
    assert cmd[0].endswith("python.exe") and cmd[1].endswith("server.py")
    assert cmd[cmd.index("--porta") + 1] == "6300"
    assert cmd[cmd.index("--pasta") + 1] == nb.data_dir(bridge)


def test_no_command_when_the_install_has_no_bridge(tmp_path):
    assert nb.command(_bridge(tmp_path, with_python=False)) is None


def test_a_packaged_bridge_exe_wins(tmp_path):
    bridge = _bridge(tmp_path)
    open(os.path.join(bridge, "neonize_bridge.exe"), "w").close()
    assert nb.command(bridge)[0].endswith("neonize_bridge.exe")


def test_an_open_port_is_adopted_without_spawning(tmp_path):
    spawned = []
    handle = _handle(_bridge(tmp_path), port_open=True, spawned=spawned)
    assert handle.start() is True
    assert spawned == []
    assert not os.path.exists(handle._pid_file)       # not ours: never stopped


def test_a_closed_port_spawns_the_bridge_and_waits_for_it(tmp_path):
    opened = iter([False, False, False, True, True])
    spawned = []
    handle = _handle(_bridge(tmp_path), port_open=lambda: next(opened, True),
                     spawned=spawned, proc=_Proc(pid=900))
    assert handle.start() is True
    assert len(spawned) == 1
    assert nb._read_pid(handle._pid_file) == 900


def test_a_bridge_that_dies_at_once_is_a_failure(tmp_path):
    handle = _handle(_bridge(tmp_path), proc=_Proc(pid=900, exits=1))
    assert handle.start() is False


def test_a_missing_bridge_is_a_failure_not_a_crash(tmp_path):
    spawned = []
    handle = _handle(_bridge(tmp_path, with_python=False), spawned=spawned)
    assert handle.start() is False and spawned == []


def test_the_last_client_out_stops_the_bridge(tmp_path):
    bridge, killed = _bridge(tmp_path), []
    alive = lambda pid: pid in (100, 200, 900)
    opened = iter([False, True])
    first = _handle(bridge, port_open=lambda: next(opened, True), alive=alive,
                    killed=killed, my_pid=100, proc=_Proc(pid=900))
    assert first.start() is True
    second = _handle(bridge, port_open=True, alive=alive, killed=killed, my_pid=200)
    assert second.start() is True

    assert first.stop() is False and killed == []      # 200 is still using it
    assert second.stop() is True and killed == [900]   # the last one out
    assert not os.path.exists(first._pid_file)


def test_dead_clients_do_not_keep_the_bridge_alive(tmp_path):
    bridge, killed = _bridge(tmp_path), []
    alive = lambda pid: pid in (100, 900)              # 200 crashed
    opened = iter([False, True])
    first = _handle(bridge, port_open=lambda: next(opened, True), alive=alive,
                    killed=killed, my_pid=100, proc=_Proc(pid=900))
    first.start()
    _handle(bridge, port_open=True, alive=alive, my_pid=200).start()
    assert first.stop() is True and killed == [900]


def test_a_bridge_started_by_hand_is_never_stopped(tmp_path):
    bridge, killed = _bridge(tmp_path), []
    handle = _handle(bridge, port_open=True, alive=lambda pid: True,
                     killed=killed, my_pid=100)
    handle.start()
    assert handle.stop() is False and killed == []


def test_the_environment_puts_ffmpeg_folders_first(tmp_path):
    bridge = _bridge(tmp_path)
    extra = tmp_path / "ffprobe_bin"
    extra.mkdir()
    with open(os.path.join(bridge, nb.EXTRA_PATH_FILE), "w", encoding="utf-8") as stream:
        stream.write(str(extra) + "\n" + str(tmp_path / "missing") + "\n")
    env = nb.environment(bridge, {"PATH": "C:\\base"})
    parts = env["PATH"].split(os.pathsep)
    assert str(extra) in parts and str(tmp_path / "missing") not in parts
    assert parts[-1] == "C:\\base"


def test_the_secret_is_read_from_the_bridge_data_folder(tmp_path):
    folder = tmp_path / "dados-real"
    folder.mkdir()
    (folder / "segredo").write_text("abc123\n", encoding="utf-8")
    assert nb.read_secret(str(tmp_path)) == "abc123"


def test_no_secret_file_means_an_empty_secret(tmp_path):
    assert nb.read_secret(str(tmp_path)) == ""


def test_the_stop_kills_only_the_process_that_was_started(tmp_path):
    bridge, killed = _bridge(tmp_path), []
    opened = iter([False, True])
    handle = _handle(bridge, port_open=lambda: next(opened, True), alive=lambda pid: True,
                     killed=killed, proc=_Proc(pid=900), created=lambda pid: "t1")
    handle.start()
    assert open(handle._pid_file, encoding="utf-8").read().split() == ["900", "t1"]
    assert handle.stop() is True and killed == [900]


def test_a_reused_pid_is_not_killed(tmp_path):
    bridge, killed = _bridge(tmp_path), []
    opened = iter([False, True])
    stamps = iter(["t1", "t2"])                  # at spawn, then at stop
    handle = _handle(bridge, port_open=lambda: next(opened, True), alive=lambda pid: True,
                     killed=killed, proc=_Proc(pid=900), created=lambda pid: next(stamps))
    handle.start()
    assert handle.stop() is False and killed == []
    assert not os.path.exists(handle._pid_file)  # stale: it named another program


def test_an_unverifiable_pid_is_left_alone(tmp_path):
    bridge, killed = _bridge(tmp_path), []
    handle = _handle(bridge, alive=lambda pid: True, killed=killed, my_pid=100)
    os.makedirs(nb.run_dir(bridge))
    with open(handle._pid_file, "w", encoding="utf-8") as stream:
        stream.write("900")                      # written without a creation time
    assert handle.stop() is False and killed == []

    with open(handle._pid_file, "w", encoding="utf-8") as stream:
        stream.write("900\nt1")
    unreadable = _handle(bridge, alive=lambda pid: True, killed=killed, created=lambda pid: "")
    assert unreadable.stop() is False and killed == []
    assert os.path.exists(handle._pid_file)      # not proven stale, kept


def test_the_output_file_is_rotated_when_it_is_too_big():
    moved = []
    assert nb.rotate_output("o.log", 100, getsize=lambda p: 101,
                            replace=lambda a, b: moved.append((a, b))) is True
    assert moved == [("o.log", "o.log.1")]


def test_a_small_output_file_is_kept():
    moved = []
    assert nb.rotate_output("o.log", 100, getsize=lambda p: 100,
                            replace=lambda a, b: moved.append((a, b))) is False
    assert moved == []


def test_rotation_failures_never_stop_the_start():
    def missing(path):
        raise FileNotFoundError(path)

    def locked(a, b):
        raise PermissionError(b)

    assert nb.rotate_output("o.log", getsize=missing) is False
    assert nb.rotate_output("o.log", 1, getsize=lambda p: 2, replace=locked) is False


def test_starting_the_bridge_rotates_its_output_first(tmp_path):
    rotated = []
    handle = _handle(_bridge(tmp_path), proc=_Proc(pid=900), rotate=rotated.append)
    handle.start()
    assert rotated == [os.path.join(nb.run_dir(handle.bridge), nb.OUTPUT_FILE)]
