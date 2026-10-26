"""Brings the neonize bridge's own folders to the current names.

Before the bridge's files were named in English, an install kept the bridge's
sessions in ``neonize_bridge/dados-real/`` and the client's own files (pid,
output, registered clients) in ``neonize_bridge/execucao/``. Those folders
hold an account's WhatsApp keys, so they are renamed in place, never recreated:
a new empty folder would ask the user to pair again.

The files inside the data folder are renamed by the bridge itself when it
starts (``neonize_bridge/legacy_layout.py``). The folders are renamed here, by
the client, and only while no bridge is running: a running bridge holds its
files open (and Windows refuses to rename them).
"""

import logging
import os

log = logging.getLogger("neonize_bridge")

#: folder of the bridge -> current name
LEGACY_FOLDERS = {"dados-real": "data", "execucao": "run"}

#: entry of the run folder -> current name
LEGACY_RUN_ENTRIES = {"ponte.pid": "bridge.pid", "ponte.log": "bridge.log",
                      "ponte.log.1": "bridge.log.1", "ponte.lock": "bridge.lock",
                      "clientes": "clients"}


def legacy_pid_file(bridge: str) -> str:
    """Where an older WinZapp wrote the pid of the bridge it started."""
    return os.path.join(bridge, "execucao", "ponte.pid")


def _rename(parent, old, new, rename):
    source, target = os.path.join(parent, old), os.path.join(parent, new)
    if not os.path.exists(source):
        return False
    if os.path.exists(target):
        log.warning("both %s and %s exist in %s; the old one was left in place",
                    old, new, os.path.basename(parent) or parent)
        return False
    try:
        rename(source, target)
    except OSError as error:
        log.warning("could not rename %s to %s: %s", old, new, type(error).__name__)
        return False
    return True


def migrate(bridge: str, running: bool, rename=os.rename) -> int:
    """Renames the legacy folders of ``bridge`` and returns how many entries
    moved. Does nothing while a bridge is ``running``."""
    if running:
        return 0
    moved = 0
    for old, new in LEGACY_FOLDERS.items():
        moved += _rename(bridge, old, new, rename)
    run = os.path.join(bridge, LEGACY_FOLDERS["execucao"])
    if os.path.isdir(run):
        for old, new in LEGACY_RUN_ENTRIES.items():
            moved += _rename(run, old, new, rename)
    if moved:
        log.info("neonize bridge folders brought to the current names (%d)", moved)
    return moved
