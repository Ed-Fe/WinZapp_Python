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

    layout.migrate(bridge, running=False)

    assert os.path.isfile(os.path.join(bridge, "dados-real", "account1", "sessao.db"))
    assert os.listdir(os.path.join(bridge, "data")) == []


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
