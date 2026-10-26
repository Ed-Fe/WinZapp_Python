"""The one-time notice that tells an install on WPPConnect about Neonize.

It runs before anything connects, so choosing Neonize starts that very launch
on it (its pairing comes next, no restart); both connections stay: the session of
the engine left is set aside and comes back when the user returns to it.

MainWindow is a wx.Frame, so the unbound method runs against a plain stub; the
dialog is replaced, nothing opens a window.
"""

import pytest

from core import connection_engine as engines
from core import neonize_bridge_process as bridge
from core import session_engine
from main import MainWindow
from ui.dialogs import engine_picker


class TestWhenItIsDue:
    def test_only_for_wppconnect_with_a_bridge_and_once(self):
        assert engines.should_offer_neonize(engines.WPPCONNECT, True, False)
        assert not engines.should_offer_neonize(engines.WPPCONNECT, True, True)
        assert not engines.should_offer_neonize(engines.WPPCONNECT, False, False)

    def test_not_to_whoever_already_uses_neonize_or_a_custom_server(self):
        assert not engines.should_offer_neonize(engines.NEONIZE, True, False)
        assert not engines.should_offer_neonize(engines.CUSTOM, True, False)

    def test_not_in_a_background_start(self):
        assert not engines.should_offer_neonize(engines.WPPCONNECT, True, False, background=True)


class _Stub:
    _offer_neonize_engine = MainWindow._offer_neonize_engine
    _apply_engine_choice = MainWindow._apply_engine_choice

    def __init__(self, settings=None, background_mode=False, engine=engines.WPPCONNECT,
                 sibling=False):
        self.sibling = sibling
        self.settings = settings if settings is not None else {}
        self.background_mode = background_mode
        self.wpp_engine = engine
        self.i18n = object()
        self.saved = []
        self.wpp_custom_api, self.wpp_server = False, "http://original"
        self.wpp_ws_server, self.wpp_port = "ws://original", 1234

    def _other_account_is_running(self):
        return self.sibling

    def save_settings(self):
        self.saved.append({k: dict(v) for k, v in self.settings.items() if isinstance(v, dict)})


@pytest.fixture
def with_bridge(monkeypatch):
    monkeypatch.setattr(bridge, "command", lambda folder, *a: ["bridge.exe"])


def _answer(monkeypatch, switch):
    asked = []
    monkeypatch.setattr(engine_picker, "ask_neonize_offer",
                        lambda i18n: asked.append(i18n) or switch)
    return asked


def test_keeping_wppconnect_changes_nothing_but_the_notice_is_not_asked_again(
        monkeypatch, with_bridge):
    mw = _Stub()
    asked = _answer(monkeypatch, False)
    mw._offer_neonize_engine()
    assert len(asked) == 1
    assert mw.settings["general"]["neonize_offer_asked"] is True
    assert "connection" not in mw.settings
    assert mw.wpp_engine == engines.WPPCONNECT and mw.wpp_port == 1234
    mw._offer_neonize_engine()
    assert len(asked) == 1


def test_switching_applies_neonize_in_this_process_without_a_restart(monkeypatch, with_bridge):
    mw = _Stub()
    _answer(monkeypatch, True)
    mw._offer_neonize_engine()
    assert mw.settings["general"]["neonize_offer_asked"] is True
    assert mw.settings["connection"] == {
        "wpp_custom_api": True, "wpp_engine": "neonize", "wpp_server": "http://127.0.0.1",
        "wpp_ws_server": "ws://127.0.0.1", "wpp_port": 6300}
    assert mw.wpp_engine == engines.NEONIZE
    assert (mw.wpp_server, mw.wpp_ws_server, mw.wpp_port) == (
        "http://127.0.0.1", "ws://127.0.0.1", 6300)
    # the flag was saved before the engine was applied: closing the program in
    # between still counts as having been told
    assert "neonize_offer_asked" in mw.saved[0]["general"]


def test_the_notice_is_marked_before_the_answer(monkeypatch, with_bridge):
    mw = _Stub()

    def explode(i18n):
        raise RuntimeError("closed while open")
    monkeypatch.setattr(engine_picker, "ask_neonize_offer", explode)
    with pytest.raises(RuntimeError):
        mw._offer_neonize_engine()
    assert mw.settings["general"]["neonize_offer_asked"] is True


@pytest.mark.parametrize("kwargs,bridge_there,asked_before", [
    ({"background_mode": True}, True, False),
    ({"engine": engines.NEONIZE}, True, False),
    ({"engine": engines.CUSTOM}, True, False),
    ({}, False, False),
    ({}, True, True),
])
def test_nothing_is_shown_when_it_is_not_due(monkeypatch, kwargs, bridge_there, asked_before):
    monkeypatch.setattr(bridge, "command", lambda folder, *a: ["x"] if bridge_there else None)
    settings = {"general": {"neonize_offer_asked": asked_before}}
    asked = _answer(monkeypatch, True)
    _Stub(settings, **kwargs)._offer_neonize_engine()
    assert asked == []


def test_both_connections_survive_the_round_trip():
    """What the notice's switch leads to: the WPPConnect session is set aside by the
    startup reconcile, and returning to WPPConnect brings it back untouched."""
    token = "wpp-session:$2b$10$abcdef"
    info = {"WA_token_protected": token, "paired": True, "WA_phone_number": "5511"}
    assert session_engine.reconcile(info, engines.NEONIZE, token) == session_engine.PARKED
    assert "WA_token_protected" not in info                 # Neonize starts unpaired
    neonize_token = "neo-session:" + "a1" * 32
    info.update({"WA_token_protected": neonize_token, "paired": True})
    assert session_engine.reconcile(info, engines.WPPCONNECT, neonize_token) == session_engine.RESTORED
    assert info["WA_token_protected"] == token and info["paired"] is True
    assert session_engine.reconcile(info, engines.NEONIZE, token) == session_engine.RESTORED
    assert info["WA_token_protected"] == neonize_token      # and Neonize's is intact


def test_nothing_is_shown_while_another_account_runs_and_it_waits_for_a_later_start(
        monkeypatch, with_bridge):
    mw = _Stub(sibling=True)
    asked = _answer(monkeypatch, True)
    mw._offer_neonize_engine()
    assert asked == [] and mw.settings.get("general", {}).get("neonize_offer_asked") is None
    assert mw.wpp_engine == engines.WPPCONNECT
    mw.sibling = False
    mw._offer_neonize_engine()
    assert len(asked) == 1


def test_a_failed_look_for_other_accounts_counts_as_one_running(monkeypatch):
    import update_coord
    monkeypatch.setattr(update_coord, "other_live_leases",
                        lambda global_dir: (_ for _ in ()).throw(OSError("lock")))
    assert MainWindow._other_account_is_running(object()) is True
    monkeypatch.setattr(update_coord, "other_live_leases", lambda global_dir: [])
    assert MainWindow._other_account_is_running(object()) is False
    monkeypatch.setattr(update_coord, "other_live_leases", lambda global_dir: [{"pid": 1}])
    assert MainWindow._other_account_is_running(object()) is True


def test_switching_from_the_notice_skips_the_second_pairing_dialog(monkeypatch, with_bridge):
    mw = _Stub()
    _answer(monkeypatch, True)
    mw._offer_neonize_engine()
    assert mw._engine_switch_announced is True


class _Reconciler:
    _reconcile_session_engine = MainWindow._reconcile_session_engine

    def __init__(self, announced):
        self.settings = {"privateinfo": {
            "WA_token_protected": "x", "paired": True, "session_engine": engines.WPPCONNECT}}
        self.background_mode = False
        self.shown = []
        if announced:
            self._engine_switch_announced = True

    def _session_engine_name(self):
        return engines.NEONIZE

    def _get_wa_token(self):
        return "wpp-session:$2b$10$abcdef"

    def save_settings(self):
        pass

    def _shutdown_audit(self, text):
        pass

    def _announce_engine_switch(self):
        self.shown.append("dialog")


def test_a_plain_engine_change_still_explains_why_pairing_is_asked():
    mw = _Reconciler(announced=False)
    mw._reconcile_session_engine()
    assert mw.shown == ["dialog"]


def test_the_notice_already_explained_it():
    mw = _Reconciler(announced=True)
    mw._reconcile_session_engine()
    assert mw.shown == []
    assert "WA_token_protected" not in mw.settings["privateinfo"]    # still set aside
