from main_window import neonize_bridge as nb


def test_a_missing_bridge_on_neonize_is_revived():
    assert nb.should_revive_bridge(True, False, False, None, 100.0)


def test_nothing_is_revived_off_neonize_or_with_the_port_open():
    assert not nb.should_revive_bridge(False, False, False, None, 100.0)
    assert not nb.should_revive_bridge(True, True, False, None, 100.0)


def test_only_one_revival_at_a_time_and_not_again_too_soon():
    assert not nb.should_revive_bridge(True, False, True, None, 100.0)
    assert not nb.should_revive_bridge(True, False, False, 100.0, 100.0 + nb.REVIVE_COOLDOWN - 1)
    assert nb.should_revive_bridge(True, False, False, 100.0, 100.0 + nb.REVIVE_COOLDOWN)


class _Window(nb.NeonizeBridgeMixin):
    wpp_port = 6300

    def __init__(self, uses_neonize=True):
        self._uses = uses_neonize
        self.started = []

    def _uses_neonize(self):
        return self._uses


def test_the_method_does_nothing_when_the_port_answers(monkeypatch):
    from core import neonize_bridge_process as process

    monkeypatch.setattr(process, "port_open", lambda *a, **k: True)
    window = _Window()
    window._revive_neonize_bridge()
    assert not getattr(window, "_neonize_reviving", False)


def test_the_method_starts_the_bridge_once_in_the_background(monkeypatch):
    from core import neonize_bridge_process as process

    started = []

    class _Bridge:
        def __init__(self, port):
            self.port = port

        def start(self):
            started.append(self.port)
            return True

    class _Thread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(process, "port_open", lambda *a, **k: False)
    monkeypatch.setattr(process, "NeonizeBridge", _Bridge)
    monkeypatch.setattr(nb.threading, "Thread", _Thread)
    window = _Window()
    window._use_neonize_bridge_secret = lambda: started.append("secret")
    window._revive_neonize_bridge()
    window._revive_neonize_bridge()      # inside the cooldown: no second start
    assert started == [6300, "secret"]
    assert window._neonize_reviving is False
