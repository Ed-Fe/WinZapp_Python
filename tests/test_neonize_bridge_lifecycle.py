"""The Neonize engine's life cycle in the client: starting the bridge at launch
(including its failure and the background mode), stopping it at shutdown, and the
restart flag the settings dialog raises when the engine is changed."""
from types import SimpleNamespace

import pytest

from core import connection_engine as engines
from core import neonize_bridge_process as bridge_process
from main_window import neonize_bridge as mixin_module
from main_window.neonize_bridge import NeonizeBridgeMixin
from ui.dialogs.settings_dialog import SettingsDialog

LOCAL = dict(wpp_server="http://127.0.0.1", wpp_ws_server="ws://127.0.0.1", wpp_port=6300)


class _I18n:
    @staticmethod
    def t(key):
        return key


def _window(custom_api=True, background_mode=False, **overrides):
    window = SimpleNamespace(
        wpp_custom_api=custom_api, background_mode=background_mode, i18n=_I18n(),
        wpp_api_key="wz-local-api-key", **{**LOCAL, **overrides})
    window._uses_neonize = lambda: NeonizeBridgeMixin._uses_neonize(window)
    window._use_neonize_bridge_secret = lambda: window.__dict__.setdefault("secret_used", True)
    window._register_node_lease = lambda: window.__dict__.setdefault("leases", []).append(1)
    window._finish_pending_deletions_on_bridge = lambda: window.__dict__.setdefault(
        "pending_deletions_retried", True)
    return window


class _FakeBridge:
    instances = []

    def __init__(self, port):
        self.port, self.started, self.stopped = port, False, False
        self.bridge = "somewhere"
        self.raises, self.result = False, True
        _FakeBridge.instances.append(self)

    def start(self):
        self.started = True
        if self.raises:
            raise RuntimeError("boom")
        return self.result

    def stop(self):
        self.stopped = True


@pytest.fixture
def boxes(monkeypatch):
    _FakeBridge.instances = []
    monkeypatch.setattr(bridge_process, "NeonizeBridge", _FakeBridge)
    shown = []
    monkeypatch.setattr(mixin_module.wx, "MessageBox", lambda *a, **k: shown.append(a))
    return shown


def test_other_engines_never_start_a_bridge(boxes):
    window = _window(custom_api=False)
    NeonizeBridgeMixin._start_neonize_bridge_if_needed(window)
    assert _FakeBridge.instances == [] and boxes == []
    assert "leases" not in window.__dict__


def test_a_custom_server_on_another_port_is_not_neonize(boxes):
    window = _window(wpp_port=8443)
    NeonizeBridgeMixin._start_neonize_bridge_if_needed(window)
    assert _FakeBridge.instances == []


def test_the_neonize_engine_starts_the_bridge_on_its_port_and_takes_its_secret(boxes):
    window = _window()
    NeonizeBridgeMixin._start_neonize_bridge_if_needed(window)
    bridge, = _FakeBridge.instances
    assert bridge.started and bridge.port == 6300
    assert window.neonize_bridge is bridge and window.secret_used is True
    assert window.pending_deletions_retried is True      # removals waiting for the bridge
    assert window.leases == [1]      # the other accounts and the updater see this one running
    assert boxes == []


@pytest.mark.parametrize("breaks", ["not_up", "raises"])
def test_a_bridge_that_does_not_come_up_is_reported_once_and_keeps_the_default_key(
        boxes, monkeypatch, breaks):
    window = _window()
    original = _FakeBridge.start

    def start(self):
        self.result = breaks != "not_up"
        self.raises = breaks == "raises"
        return original(self)
    monkeypatch.setattr(_FakeBridge, "start", start)

    NeonizeBridgeMixin._start_neonize_bridge_if_needed(window)

    assert "secret_used" not in window.__dict__
    (message, title, _style), = boxes
    assert message == "neonize_bridge_unavailable_message"
    assert title == "neonize_bridge_unavailable_title"


def test_background_mode_does_not_open_a_message_box(boxes, monkeypatch):
    window = _window(background_mode=True)
    monkeypatch.setattr(_FakeBridge, "start", lambda self: False)
    NeonizeBridgeMixin._start_neonize_bridge_if_needed(window)
    assert boxes == []


def test_stopping_asks_the_bridge_to_stop_and_forgets_it():
    window = SimpleNamespace(neonize_bridge=_FakeBridge(6300))
    bridge = window.neonize_bridge
    NeonizeBridgeMixin._stop_neonize_bridge(window)
    assert bridge.stopped and window.neonize_bridge is None


def test_stopping_without_a_bridge_is_a_no_op_and_a_failing_stop_is_swallowed():
    NeonizeBridgeMixin._stop_neonize_bridge(SimpleNamespace())
    bridge = _FakeBridge(6300)
    bridge.stop = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    window = SimpleNamespace(neonize_bridge=bridge)
    NeonizeBridgeMixin._stop_neonize_bridge(window)
    assert window.neonize_bridge is None


@pytest.mark.parametrize("loaded,selected,restart", [
    (engines.WPPCONNECT, engines.WPPCONNECT, False),
    (engines.WPPCONNECT, engines.NEONIZE, True),
    (engines.NEONIZE, engines.CUSTOM, True),
])
def test_the_settings_dialog_flags_a_restart_only_when_the_engine_changed(
        loaded, selected, restart):
    dialog = SimpleNamespace(
        _engine_choice=SimpleNamespace(GetSelection=lambda: engines.index_of(selected)),
        _loaded_engine=loaded, _restart_required=False)
    SettingsDialog._flag_restart_if_engine_changed(dialog)
    assert dialog._restart_required is restart
    assert dialog._loaded_engine == selected


def test_a_neonize_account_shows_up_among_the_running_accounts(boxes, tmp_path):
    """Audit F4, M3: only the Node path wrote a lease, so with the bridge
    "Quit all accounts" and the updater's list of peers were always empty."""
    import node_coord
    from main_window.wpp_server import WppServerMixin

    window = _window()
    window.global_dir, window.account_id = str(tmp_path), "a" * 32
    window._register_node_lease = lambda: WppServerMixin._register_node_lease(window)

    NeonizeBridgeMixin._start_neonize_bridge_if_needed(window)

    leases = node_coord.live_node_leases(str(tmp_path), is_alive=lambda pid, created: True)
    assert [lease.get("account_id") for lease in leases] == ["a" * 32]
