from types import SimpleNamespace

import pytest

from core import wpp_update_health
from main_window import wpp_update_validation as validation


@pytest.fixture
def phases(monkeypatch):
    workers, callbacks, events, done = [], [], [], []
    results = [True]
    class Thread:
        def __init__(self, target, **kw):
            self.target = target
        def start(self):
            workers.append(self.target)
    window = SimpleNamespace(
        ensure_wpp_running=lambda: events.append("start"),
        _stop_wpp_server=lambda: events.append("stop"),
        wait_for_profile_release=lambda *a, **k: events.append("released"),
        wpp_server="http://127.0.0.1", wpp_port=6300,
        wpp_process=SimpleNamespace(pid=123), _node_instance_id="instance",
        _shutting_down=False,
    )
    monkeypatch.setattr(validation, "threading", SimpleNamespace(Thread=Thread))
    monkeypatch.setattr(validation.wx, "CallAfter", lambda fn, *a: callbacks.append(lambda: fn(*a)))
    monkeypatch.setattr(validation, "wait_for_api", lambda *a, **k: results.pop(0))
    monkeypatch.setattr(validation.api_staging, "discard", lambda path: events.append(("discard", path)))
    monkeypatch.setattr(validation.api_staging, "detach_previous_api",
                        lambda *a: events.append("detach") or "detached")
    monkeypatch.setattr(validation.api_staging, "restore_previous_api",
                        lambda *a: events.append("restore") or "failed")
    return SimpleNamespace(window=window, workers=workers, callbacks=callbacks,
                           events=events, done=done, results=results)


def test_backup_and_success_wait_for_health_validation(phases, caplog):
    caplog.set_level("INFO")
    p = phases
    validation.restart_api_after_update(p.window, "api", "api_old", p.done.append)
    assert p.events == ["start"] and p.done == []
    p.workers.pop(0)()
    assert p.events[-1] == "detach" and p.done == []
    assert not any(isinstance(event, tuple) and event[0] == "discard" for event in p.events)
    p.callbacks.pop(0)()
    assert p.done == [True]
    p.workers.pop(0)()
    assert p.events[-1] == ("discard", "detached")
    for step in ("api_startup", "api_http_validation", "previous_api_cleanup"):
        assert f"step={step} event=start" in caplog.text
        assert f"step={step} event=end" in caplog.text


def test_cleanup_rename_failure_keeps_working_new_api(phases, monkeypatch):
    p = phases
    monkeypatch.setattr(validation.api_staging, "detach_previous_api",
                        lambda *a: (_ for _ in ()).throw(OSError("locked")))
    validation.restart_api_after_update(p.window, "api", "api_old", p.done.append)
    p.workers.pop(0)()
    p.callbacks.pop(0)()
    assert p.done == [True] and p.workers == []
    assert "restore" not in p.events


def test_cleanup_waits_for_whatsapp_reconnection(phases, monkeypatch):
    p = phases
    def sleep(_seconds):
        assert p.done == [True]
        assert ("discard", "detached") not in p.events
        p.window._wpp_reconnect_started = None
        p.events.append("reconnected")
    monkeypatch.setattr(validation, "time", SimpleNamespace(monotonic=lambda: 0.0, sleep=sleep))
    validation.restart_api_after_update(p.window, "api", "api_old", p.done.append)
    p.workers.pop(0)()
    p.window._wpp_reconnect_started = 1.0
    p.callbacks.pop(0)()
    p.workers.pop(0)()
    assert p.events[-2:] == ["reconnected", ("discard", "detached")]


def test_shutdown_leaves_detached_tree_for_next_startup(phases):
    p = phases
    validation.restart_api_after_update(p.window, "api", "api_old", p.done.append)
    p.workers.pop(0)()
    p.window._wpp_reconnect_started = 1.0
    p.callbacks.pop(0)()
    p.window._shutting_down = True
    p.workers.pop(0)()
    assert ("discard", "detached") not in p.events


def test_failed_new_api_restores_and_checks_old_api_but_reports_failed_update(phases):
    p = phases
    p.results[:] = [False, True]
    validation.restart_api_after_update(p.window, "api", "api_old", p.done.append)
    p.workers.pop(0)()
    p.callbacks.pop(0)()
    assert "restore" not in p.events and p.done == []
    p.workers.pop(0)()
    assert p.events[1:4] == ["stop", "released", "restore"]
    assert ("discard", "api_old") not in p.events
    p.callbacks.pop(0)()
    p.workers.pop(0)()
    p.callbacks.pop(0)()
    assert p.events.count("start") == 2 and p.done == [False]


def test_restart_exception_still_allows_rollback(phases):
    p = phases
    def fail():
        raise SystemExit(1)
    p.window.ensure_wpp_running = fail
    validation.restart_api_after_update(p.window, "api", "api_old", p.done.append)
    p.workers.pop(0)()
    p.callbacks.pop(0)()
    p.workers.pop(0)()
    assert "restore" in p.events and ("discard", "api_old") not in p.events


def test_shutdown_retains_backup_without_starting_a_rollback(phases):
    p = phases
    p.results[:] = [False]
    p.window._shutting_down = True
    validation.restart_api_after_update(p.window, "api", "api_old", p.done.append)
    p.workers.pop(0)()
    p.callbacks.pop(0)()
    assert p.done == [False] and "restore" not in p.events


def test_a_profile_still_in_use_prevents_rollback_and_retains_backup(phases):
    p = phases
    p.results[:] = [False]
    p.window.wait_for_profile_release = lambda *a, **k: False
    validation.restart_api_after_update(p.window, "api", "api_old", p.done.append)
    p.workers.pop(0)()
    p.callbacks.pop(0)()
    p.workers.pop(0)()
    p.callbacks.pop(0)()
    assert p.done == [False] and "restore" not in p.events
    assert ("discard", "api_old") not in p.events


@pytest.mark.parametrize("updating", [False, True])
def test_background_startup_timeout_does_not_exit_during_an_update(monkeypatch, updating):
    from main_window import wpp_server
    monkeypatch.setattr(wpp_server, "os", SimpleNamespace(path=SimpleNamespace(isfile=lambda _p: True)))
    monkeypatch.setattr(wpp_server, "time", SimpleNamespace(time=iter([0, 301]).__next__, sleep=lambda _s: None))
    monkeypatch.setattr("core.wa_version_refresh.wait_for_refresh", lambda: None)
    window = SimpleNamespace(
        _is_wpp_running=lambda: False, background_mode=True, _wpp_updating=updating,
        _start_wpp_background=lambda: None,
    )
    if updating:
        assert wpp_server.WppServerMixin.ensure_wpp_running(window) is False
    else:
        with pytest.raises(SystemExit):
            wpp_server.WppServerMixin.ensure_wpp_running(window)


def test_port_close_wait_is_bounded(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(wpp_update_health.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(wpp_update_health.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    assert wpp_update_health.wait_for_port_closed(lambda: True, timeout=1) is False
    assert now[0] == 1


@pytest.mark.parametrize("health,identity,expected", [
    ({"message": "OK"}, {"protocol_version": 1, "pid": 123}, True),
    ({"message": "ERROR"}, {"protocol_version": 1, "pid": 123}, False),
    ({"message": "OK"}, {"protocol_version": 1, "pid": 999}, False),
    ({"message": "OK"}, {"protocol_version": 2, "pid": 123}, False),
])
def test_http_health_requires_the_expected_api_identity(monkeypatch, health, identity, expected):
    now, calls = [0.0], []
    class Response:
        status_code = 200
        def __init__(self, body): self.body = body
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def json(self): return self.body
    class Session:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, url, **kwargs):
            assert self.trust_env is False and kwargs["allow_redirects"] is False
            calls.append(url)
            return Response(health if url.endswith("healthz") else identity)
    monkeypatch.setattr(wpp_update_health.requests, "Session", Session)
    monkeypatch.setattr(wpp_update_health.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(wpp_update_health.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    assert wpp_update_health.wait_for_api("http://127.0.0.1", 6301,
                                         identity={"pid": 123}, timeout=1) is expected
    assert calls[0] == "http://127.0.0.1:6301/healthz"
