"""Pairing refused by WhatsApp: the QR shown, the repair and its limits."""
import base64
import logging
import os
import struct
import threading
from types import SimpleNamespace as N

import segno

import pairing_repair
import server
import session_lifecycle


def test_failure_kind_reads_the_two_whatsmeow_lines():
    assert pairing_repair.failure_kind(
        "Failed to pair device: invalid device identity HMAC") == pairing_repair.QR
    assert pairing_repair.failure_kind(
        "Failed to handle code pair notification: x") == pairing_repair.CODE
    assert pairing_repair.failure_kind("Connected to WhatsApp") is None


def test_budget_gives_three_restarts_and_is_given_back():
    b = pairing_repair.Budget()
    assert [b.take() for _ in range(4)] == [True, True, True, False]
    b.reset()
    assert b.take()


def test_handler_reports_only_error_lines():
    seen = []
    handler = pairing_repair.FailureHandler(seen.append)
    log = logging.getLogger("test.pairing.repair")
    log.propagate = False
    log.addHandler(handler)
    log.error("Failed to pair device: invalid device identity HMAC")
    log.warning("Failed to pair device: warning level is not an error")
    log.error("something else")
    log.removeHandler(handler)
    assert seen == [pairing_repair.QR]


def test_wipe_removes_the_keys_and_keeps_the_messages(tmp_path):
    for name in ("session.db", "session.db-wal", "bridge.db"):
        (tmp_path / name).write_bytes(b"x")
    assert pairing_repair.wipe_session_keys(str(tmp_path))
    assert sorted(os.listdir(tmp_path)) == ["bridge.db"]
    assert pairing_repair.wipe_session_keys(str(tmp_path))     # nothing left is fine


def test_qr_image_has_the_wide_margin_and_whole_pixel_modules():
    text = "2@" + "abc/+=" * 30
    url = server.qr_data_url(text)
    assert url.startswith("data:image/png;base64,")
    png = base64.b64decode(url.split(",", 1)[1])
    width, height = struct.unpack(">II", png[16:24])
    modules = segno.make_qr(text, error="l").symbol_size(border=server.QR_BORDER, scale=1)[0]
    assert width == height == modules * server.QR_SCALE


def stub(tmp_path, *, current=True, status="QRCODE", used=0, alive=False):
    calls = []
    s = N(
        folder=str(tmp_path), name="s", status=status, thread=None,
        lifecycle=N(current=lambda generation: current),
        pairing_repairs=pairing_repair.Budget(), _lifecycle_lock=threading.RLock(),
        stop=lambda status=None: calls.append(("stop", status)),
        emit=lambda event, data: calls.append(("emit", event)),
        begin=lambda *a: calls.append(("begin",)),
        _thread_alive=lambda: alive, _repair_pairing=lambda *a: None)
    s.pairing_repairs.used = used
    return s, calls


def test_qr_refusal_wipes_the_keys_and_starts_again(tmp_path):
    (tmp_path / "session.db").write_bytes(b"keys")
    s, calls = stub(tmp_path)
    server.Session._repair_pairing(s, 1, pairing_repair.QR)
    assert calls == [("stop", session_lifecycle.CLOSED), ("begin",)]
    assert not (tmp_path / "session.db").exists()


def test_code_refusal_wipes_but_does_not_mint_another_code(tmp_path):
    (tmp_path / "session.db").write_bytes(b"keys")
    s, calls = stub(tmp_path)
    server.Session._repair_pairing(s, 1, pairing_repair.CODE)
    assert ("begin",) not in calls and ("emit", "phoneCodeError") in calls
    assert not (tmp_path / "session.db").exists()


def test_after_three_repairs_it_stops_and_tells_the_client(tmp_path):
    s, calls = stub(tmp_path, used=3)
    server.Session._repair_pairing(s, 1, pairing_repair.QR)
    assert ("begin",) not in calls and ("emit", "phoneCodeError") in calls


def test_an_old_session_or_a_paired_one_is_not_repaired(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(server.threading, "Thread",
                        lambda **kw: N(start=lambda: started.append(1)))
    for session in (stub(tmp_path, current=False)[0], stub(tmp_path, status="CONNECTED")[0]):
        server.Session._on_pairing_failure(session, 1, pairing_repair.QR, None)
    assert started == []
    server.Session._on_pairing_failure(stub(tmp_path)[0], 1, pairing_repair.QR, None)
    assert started == [1]
