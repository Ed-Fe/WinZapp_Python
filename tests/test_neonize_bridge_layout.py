"""The bridge's folders written under the old (Portuguese) names keep working:
they are renamed in place, so an account paired before the rename does not have
to pair again."""

import os

from core import neonize_bridge_layout as layout
from core import neonize_bridge_process as nb


def _legacy_bridge(tmp_path):
    root = tmp_path / "neonize_bridge"
    session = root / "dados-real" / "account1"
    session.mkdir(parents=True)
    (session / "sessao.db").write_bytes(b"keys")
    run = root / "execucao"
    (run / "clientes").mkdir(parents=True)
    (run / "clientes" / "100").write_text("100")
    (run / "ponte.pid").write_text("900\nt900")
    (run / "ponte.log").write_text("output")
    (run / "ponte.lock").write_text("")
    return str(root)


def test_the_legacy_folders_and_run_files_get_the_current_names(tmp_path):
    bridge = _legacy_bridge(tmp_path)

    moved = layout.migrate(bridge, running=False)

    assert moved == 6
    assert (tmp_path / "neonize_bridge" / "data" / "account1" / "sessao.db").read_bytes() == b"keys"
    run = tmp_path / "neonize_bridge" / "run"
    assert (run / "bridge.pid").read_text() == "900\nt900"
    assert (run / "bridge.log").read_text() == "output"
    assert (run / "clients" / "100").exists() and (run / "bridge.lock").exists()
    assert not (tmp_path / "neonize_bridge" / "dados-real").exists()
    assert not (tmp_path / "neonize_bridge" / "execucao").exists()


def test_nothing_is_renamed_while_a_bridge_is_running(tmp_path):
    bridge = _legacy_bridge(tmp_path)

    assert layout.migrate(bridge, running=True) == 0

    assert os.path.isdir(os.path.join(bridge, "dados-real"))
    assert os.path.isdir(os.path.join(bridge, "execucao"))


def test_a_folder_whose_new_name_exists_is_never_replaced(tmp_path):
    bridge = _legacy_bridge(tmp_path)
    os.makedirs(os.path.join(bridge, "data"))
    open(os.path.join(bridge, "data", "keep"), "w").close()

    layout.migrate(bridge, running=False)

    assert os.path.isfile(os.path.join(bridge, "dados-real", "account1", "sessao.db"))
    assert os.listdir(os.path.join(bridge, "data")) == ["keep"]


def test_a_rename_that_fails_leaves_the_old_folder_in_place(tmp_path):
    bridge = _legacy_bridge(tmp_path)

    def refuse(source, target):
        raise PermissionError(target)

    assert layout.migrate(bridge, running=False, rename=refuse) == 0
    assert os.path.isfile(os.path.join(bridge, "dados-real", "account1", "sessao.db"))


def test_a_fresh_install_has_nothing_to_rename(tmp_path):
    root = tmp_path / "neonize_bridge"
    root.mkdir()
    assert layout.migrate(str(root), running=False) == 0


def test_starting_the_bridge_renames_the_legacy_folders_first(tmp_path):
    bridge = _legacy_bridge(tmp_path)
    (tmp_path / "neonize_bridge" / ".venv" / "Scripts").mkdir(parents=True)
    (tmp_path / "neonize_bridge" / ".venv" / "Scripts" / "python.exe").write_text("")
    (tmp_path / "neonize_bridge" / "server.py").write_text("")

    class _Proc:
        pid, returncode = 900, None

        def poll(self):
            return None

    opened = iter([False, False, True])
    clock = iter(range(0, 10_000))
    handle = nb.NeonizeBridge(
        bridge, 6300, is_open=lambda port: next(opened, True), alive=lambda pid: False,
        created=lambda pid: "t900", rotate=lambda path: False,
        start_process=lambda cmd, env, cwd, output=None: _Proc(), kill=lambda pid: None,
        sleep=lambda s: None, clock=lambda: next(clock), my_pid=100,
        identity=lambda port: {"installation_id": "", "instance_id": "",
                               "protocol_version": 1, "pid": 900, "ppid": 1})

    assert handle.start() is True

    assert os.path.isfile(os.path.join(bridge, "data", "account1", "sessao.db"))
    assert not os.path.exists(os.path.join(bridge, "dados-real"))
    assert os.path.isfile(os.path.join(nb.run_dir(bridge), nb.BRIDGE_PID_FILE))


def _handle(bridge, *, alive, created, killed):
    class _Proc:
        pid, returncode = 900, None

        def poll(self):
            return None

    opened = iter([False, False, True])
    clock = iter(range(0, 10_000))
    return nb.NeonizeBridge(
        bridge, 6300, is_open=lambda port: next(opened, True), alive=alive,
        created=created, rotate=lambda path: False,
        start_process=lambda cmd, env, cwd, output=None: _Proc(), kill=killed.append,
        sleep=lambda s: None, clock=lambda: next(clock), my_pid=100,
        identity=lambda port: {"installation_id": "", "instance_id": "",
                               "protocol_version": 1, "pid": 900, "ppid": 1})


def _installed(tmp_path):
    bridge = _legacy_bridge(tmp_path)
    scripts = tmp_path / "neonize_bridge" / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_text("")
    (tmp_path / "neonize_bridge" / "server.py").write_text("")
    return bridge


def test_a_reused_legacy_pid_does_not_block_the_rename(tmp_path):
    bridge, killed = _installed(tmp_path), []
    # the old ponte.pid names pid 900 (stamp t900), now another program with another stamp
    handle = _handle(bridge, alive=lambda pid: pid == 900,
                     created=lambda pid: "other", killed=killed)

    assert handle.start() is True

    assert os.path.isfile(os.path.join(bridge, "data", "account1", "sessao.db"))
    assert not os.path.exists(os.path.join(bridge, "dados-real"))
    assert killed == []


def test_a_legacy_bridge_still_running_without_clients_is_stopped_then_migrated(tmp_path):
    bridge, killed = _installed(tmp_path), []
    os.remove(os.path.join(bridge, "execucao", "clientes", "100"))
    handle = _handle(bridge, alive=lambda pid: pid == 900,
                     created=lambda pid: "t900", killed=killed)

    handle.start()

    assert 900 in killed
    assert os.path.isfile(os.path.join(bridge, "data", "account1", "sessao.db"))


def test_a_legacy_bridge_with_a_live_old_client_is_left_alone_and_nothing_starts(tmp_path):
    bridge, killed = _installed(tmp_path), []
    handle = _handle(bridge, alive=lambda pid: pid in (900, 100000),
                     created=lambda pid: "t900", killed=killed)
    with open(os.path.join(bridge, "execucao", "clientes", "100000"), "w") as stream:
        stream.write("100000")

    assert handle.start() is False

    assert killed == []
    assert os.path.isfile(os.path.join(bridge, "dados-real", "account1", "sessao.db"))
    assert not os.path.exists(os.path.join(bridge, "data"))


def test_an_empty_current_folder_does_not_strand_the_legacy_data(tmp_path):
    bridge = _legacy_bridge(tmp_path)
    os.makedirs(os.path.join(bridge, "data"))

    layout.migrate(bridge, running=False)

    assert os.path.isfile(os.path.join(bridge, "data", "account1", "sessao.db"))


# -- the bridge that ships inside a compiled install -----------------------

def _compiled_install(tmp_path):
    """dist/WinZapp/neonize_bridge (frozen) next to an older dist/neonize_bridge."""
    install = tmp_path / "dist" / "WinZapp"
    bridge = install / "neonize_bridge"
    bridge.mkdir(parents=True)
    (bridge / nb.PACKAGED_EXE).write_text("exe")
    older = tmp_path / "dist" / "neonize_bridge" / "data" / "account1"
    older.mkdir(parents=True)
    (older / "session.db").write_bytes(b"keys")
    return str(bridge), str(install / "data" / "global" / "neonize_bridge" / "data"), tmp_path / "dist"


def test_the_sessions_of_an_older_install_move_into_the_compiled_one(tmp_path):
    bridge, target, dist = _compiled_install(tmp_path)

    assert layout.adopt_sibling_data(bridge, target) == "moved"

    assert open(os.path.join(target, "account1", "session.db"), "rb").read() == b"keys"
    assert not (dist / "neonize_bridge" / "data").exists()


def test_a_sibling_with_the_portuguese_names_is_brought_along(tmp_path):
    bridge, target, dist = _compiled_install(tmp_path)
    (dist / "neonize_bridge" / "data").rename(dist / "neonize_bridge" / "dados-real")

    assert layout.adopt_sibling_data(bridge, target) == "moved"

    assert os.path.isfile(os.path.join(target, "account1", "session.db"))


def test_an_install_that_has_its_own_data_keeps_it(tmp_path):
    bridge, target, dist = _compiled_install(tmp_path)
    os.makedirs(os.path.join(target, "mine"))

    assert layout.adopt_sibling_data(bridge, target) == ""

    assert (dist / "neonize_bridge" / "data" / "account1" / "session.db").exists()


def test_an_empty_target_is_replaced_and_nothing_to_adopt_is_not_an_error(tmp_path):
    bridge, target, dist = _compiled_install(tmp_path)
    os.makedirs(target)
    assert layout.adopt_sibling_data(bridge, target) == "moved"
    assert layout.adopt_sibling_data(bridge, target) == ""        # nothing left to adopt


def test_a_move_that_fails_is_reported_and_leaves_the_data_where_it_was(tmp_path):
    bridge, target, dist = _compiled_install(tmp_path)

    def refuse(source, destination):
        raise PermissionError(destination)

    assert layout.adopt_sibling_data(bridge, target, move=refuse) == "failed"
    assert (dist / "neonize_bridge" / "data" / "account1" / "session.db").exists()


def test_a_compiled_bridge_keeps_its_data_and_run_files_outside_the_install_folder(tmp_path):
    bridge, target, _ = _compiled_install(tmp_path)
    assert nb.is_packaged(bridge)
    assert nb.data_dir(bridge).endswith(os.path.join("global", "neonize_bridge", "data"))
    assert nb.run_dir(bridge).endswith(os.path.join("global", "neonize_bridge", "run"))
    assert not nb.is_packaged(str(tmp_path))
    assert nb.data_dir(str(tmp_path)) == str(tmp_path / "data")
