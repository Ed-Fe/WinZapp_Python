"""The Sparkle bridge (sparkle_mac) as Sparkle calls it.

Sparkle calls its delegate and its user driver from Objective-C: a BOOL
answer with an NSError written through an out pointer, a block to call
later. A selector signature PyObjC reads differently would turn "refuse this
update" into a crash or, worse, a YES. So the delegate is called here through
objc_msgSend with the C types of Sparkle 2.10.0's headers, not through
Python, and the tests check what Objective-C receives: the veto refuses each
failure, a download request is only ever the release's https zip (never a
file:// URL, which SPUDownloader.m:65-81 would refuse after nothing but
waste), the last step before installing asks the official repository again
and answers Skip (cancel) on anything but a fresh yes, quitting cancels a
pending install, and an exception refuses. No window is created; the user
driver subclass is exercised against the real framework only when the build
has unpacked it (macos/cache), with its windows replaced.
"""

import ctypes
import os
import signal
import sys
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path[:0] = [os.path.join(ROOT, "macos"), os.path.join(ROOT, "client"), os.path.join(ROOT, ".pydeps")]

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS layer")

import objc  # noqa: E402
from Foundation import NSBundle, NSDictionary, NSMutableURLRequest, NSObject, NSURL  # noqa: E402

from winzapp_mac import provenance as P, sparkle_mac as sm, sparkle_policy as S  # noqa: E402

REPO = "rocco-labs/WinZapp_Python"
RUNNING = "v2.1.0.4250alpha"
TAG = "v2.1.0.4258alpha"
INFO = {"WinZappMacReleasesRepo": REPO, "WinZappReleaseTag": RUNNING}
ASSET = S.zip_url(REPO, TAG, sm.ARCH)
FRAMEWORK = os.path.join(ROOT, "macos", "cache", "sparkle-2.10.0", "Sparkle.framework")

_RealThread = threading.Thread      # the fixture makes sm's threads run inline
_RealBarrier = threading.Barrier

_lib = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
_lib.sel_registerName.restype = ctypes.c_void_p
_lib.sel_registerName.argtypes = [ctypes.c_char_p]
_SEND = ctypes.cast(_lib.objc_msgSend, ctypes.c_void_p).value
_ID = ctypes.c_void_p


def _send(restype, obj, sel, *args):
    """objc_msgSend(obj, sel, *args) with each arg as (ctype, value)."""
    fn = ctypes.CFUNCTYPE(restype, _ID, _ID, *[t for t, _ in args])(_SEND)
    return fn(objc.pyobjc_id(obj), _lib.sel_registerName(sel), *[v for _, v in args])


def _obj(o):
    """A bare pointer: the caller keeps *o* referenced until the call returns."""
    return (_ID, objc.pyobjc_id(o))


class FakeAppcastItem(NSObject):
    """What the bridge reads of SUAppcastItem."""

    def versionString(self):
        return self._version

    def displayVersionString(self):
        return self._display

    def fileURL(self):
        return NSURL.URLWithString_(self._url)

    def deltaUpdates(self):
        return self._deltas

    def installationType(self):
        return self._type


def _item(tag=TAG, url=None, deltas=None, kind="application"):
    item = FakeAppcastItem.alloc().init()
    item._version, item._display = P.bundle_version(tag), P.tag_version(tag)
    item._url = url or S.zip_url(REPO, tag, sm.ARCH)
    item._deltas = NSDictionary.dictionaryWithDictionary_(deltas) if deltas else None
    item._type = kind
    return item


class _Window:
    def __init__(self):
        self.settings = {"general": {}}
        self.spoken = []

        class I18n:
            def t(self, key):
                return {"error": "{app_name} error"}.get(key, key)
        self.i18n = I18n()

    def output(self, text, interrupt=False):
        self.spoken.append(text)


class _Now:
    """threading.Thread that runs its target at start(), in the test."""

    def __init__(self, target, **kw):
        self.target = target

    def start(self):
        self.target()


@pytest.fixture
def bridge(monkeypatch):
    monkeypatch.setattr(sm.updater_mac, "_info", lambda: dict(INFO))
    monkeypatch.setattr(sm.wx, "CallAfter", lambda fn, *a, **k: fn(*a, **k))
    monkeypatch.setattr(sm.wx, "CallLater", lambda ms, fn, *a, **k: None)
    monkeypatch.setattr(sm.wx, "MessageBox", lambda *a, **k: shown.append(a[0]))
    monkeypatch.setattr(sm.threading, "Thread", _Now)
    shown = []
    monkeypatch.setattr(sm, "_state", {"mw": _Window(), "updater": None, "driver": None,
                                       "delegate": None, "checking": "", "user_waiting": False,
                                       "offered": "", "offered_item": None, "outstanding": None,
                                       "slot": None, "installing": False, "closed_accounts": [],
                                       "quitting": False, "cancel_deadline": None,
                                       "driver_delegate": None, "immediate_focus": False,
                                       "user_initiated": False})
    monkeypatch.setattr(sm, "SLOT_WAIT", 0)
    monkeypatch.setattr(sm, "_verdicts", {})
    sm._state["shown"] = shown
    return sm.WinZappSparkleDelegate.alloc().init()


def _proceed(delegate, item, check):
    err = ctypes.c_void_p(0)
    ok = _send(ctypes.c_bool, delegate, b"updater:shouldProceedWithUpdate:updateCheck:error:",
               _obj(delegate), _obj(item), (ctypes.c_long, check), (ctypes.POINTER(_ID), ctypes.byref(err)))
    return ok, (objc.objc_object(c_void_p=err.value) if err.value else None)


class TestTheVetoBeforeOffering:
    def test_first_sight_aborts_silently_and_asks_the_official_repository(self, bridge, monkeypatch):
        asked = []
        monkeypatch.setattr(sm, "_check_offer", lambda tag, check: asked.append((tag, check)))
        ok, err = _proceed(bridge, _item(), S.CHECK_USER)
        assert ok is False
        # The one code Sparkle's UI aborts on without an error window.
        assert err.code() == S.SILENT_ABORT and err.domain() == "SUSparkleErrorDomain"
        assert asked == [(TAG, S.CHECK_USER)]

    def test_a_confirmed_release_goes_through_once(self, bridge):
        sm._verdicts[TAG] = (sm.time.monotonic(), (True, "ok"))
        assert _proceed(bridge, _item(), S.CHECK_BACKGROUND) == (True, None)
        assert TAG not in sm._verdicts and sm._state["offered"] == TAG

    def test_a_refusal_reaches_sparkle_with_its_reason(self, bridge):
        sm._verdicts[TAG] = (sm.time.monotonic(), (False, "could not verify commit provenance: no such tag"))
        ok, err = _proceed(bridge, _item(), S.CHECK_USER)
        assert ok is False and err.code() == S.REFUSED
        assert "no such tag" in err.localizedDescription()

    @pytest.mark.parametrize("item", [
        lambda: _item(url="https://evil.example/WinZapp-macOS-arm64.zip"),
        lambda: _item(url=ASSET.replace("https://", "http://")),
        lambda: _item(deltas={"2.1.0.42501": "x"}),
        lambda: _item(kind="package"),
    ])
    def test_anything_but_the_releases_full_zip_is_refused_without_asking(self, bridge, monkeypatch, item):
        monkeypatch.setattr(sm, "_check_offer", lambda *a: pytest.fail("asked about a foreign item"))
        sm._verdicts[TAG] = (sm.time.monotonic(), (True, "ok"))
        ok, err = _proceed(bridge, item(), S.CHECK_USER)
        assert ok is False and err.code() == S.REFUSED

    def test_an_old_answer_is_asked_again(self, bridge, monkeypatch):
        asked = []
        monkeypatch.setattr(sm, "_check_offer", lambda tag, check: asked.append(tag))
        sm._verdicts[TAG] = (sm.time.monotonic() - sm.VERDICT_TTL - 1, (True, "ok"))
        ok, err = _proceed(bridge, _item(), S.CHECK_USER)
        assert ok is False and err.code() == S.SILENT_ABORT and asked == [TAG]

    def test_an_exception_refuses(self, bridge, monkeypatch):
        def boom(*a):
            raise RuntimeError("bug")
        monkeypatch.setattr(sm.policy, "decide", boom)
        ok, err = _proceed(bridge, _item(), S.CHECK_USER)
        assert ok is False and err.code() == S.REFUSED

    def test_the_question_is_release_provenance_and_its_answer_comes_back_as_the_same_check(
            self, bridge, monkeypatch):
        seen, rechecks = [], []
        monkeypatch.setattr(sm.updater_mac, "release_provenance",
                            lambda version: seen.append(version) or (b"raw", "ok"))
        monkeypatch.setattr(sm, "_recheck", lambda user, attempt=0: rechecks.append(user))
        sm._check_offer(TAG, S.CHECK_BACKGROUND)
        assert seen == ["2.1.0.4258alpha"]
        assert sm._verdicts[TAG][1] == (True, "ok") and rechecks == [False]

    def test_a_refusal_the_user_asked_for_is_shown_one_a_schedule_found_is_not(self, bridge, monkeypatch):
        rechecks = []
        monkeypatch.setattr(sm.updater_mac, "release_provenance", lambda v: (None, "offline"))
        monkeypatch.setattr(sm, "_recheck", lambda user, attempt=0: rechecks.append(user))
        sm._check_offer(TAG, S.CHECK_BACKGROUND)
        assert sm._verdicts == {} and rechecks == []
        sm._check_offer(TAG, S.CHECK_USER)
        assert sm._verdicts[TAG][1] == (False, "offline") and rechecks == [True]


class TestTheDownloadRequest:
    def _download(self, delegate, item, url):
        request = NSMutableURLRequest.requestWithURL_(NSURL.URLWithString_(url))
        _send(None, delegate, b"updater:willDownloadUpdate:withRequest:",
              _obj(delegate), _obj(item), _obj(request))
        return str(request.URL().absoluteString())

    def test_the_offered_release_is_downloaded_from_its_https_asset(self, bridge):
        """What Sparkle fetches is the published asset itself, over https:
        never a file:// URL (SPUDownloader.m:65-81 refuses those)."""
        sm._state["offered"] = TAG
        got = self._download(bridge, _item(), ASSET)
        assert got == ASSET and got.startswith("https://")

    @pytest.mark.parametrize("case", ["not offered", "other url", "delta"])
    def test_anything_else_is_sent_where_it_fails(self, bridge, case):
        if case != "not offered":
            sm._state["offered"] = TAG
        url = {"other url": "https://evil.example/x.zip", "delta": ASSET + ".delta"}.get(case, ASSET)
        got = self._download(bridge, _item(), url)
        assert got == sm.REFUSED_DOWNLOAD and got.startswith("https://")

    def test_an_exception_refuses_the_download(self, bridge, monkeypatch):
        sm._state["offered"] = TAG
        monkeypatch.setattr(sm, "_item", lambda item: 1 / 0)
        assert self._download(bridge, _item(), ASSET) == sm.REFUSED_DOWNLOAD


class _Accounts:
    """Two account processes, as update_coord sees them, with its real
    liveness (no stub): this one (the test process) and acc2, a real second
    process of this interpreter, the way a second account is a second process
    of the app, which ends when it is asked to quit."""

    def __init__(self, monkeypatch, tmp_path, quits=True):
        pytest.importorskip("psutil")
        import subprocess
        import update_coord
        from winzapp_mac import coord_mac
        monkeypatch.setattr(update_coord, "_default_proc_matches_us", coord_mac.matches_us)
        self.uc, self.gd = update_coord, str(tmp_path)
        self.other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        time.sleep(0.2)
        update_coord.try_create_runtime_lease(self.gd)
        update_coord.try_create_runtime_lease(self.gd, pid=self.other.pid)
        self.quit_requests = []

        def request_quit(gd, account):
            self.quit_requests.append(account)
            if quits:
                self.other.kill()
                self.other.wait()
        import ipc
        monkeypatch.setattr(ipc, "request_quit", request_quit)
        monkeypatch.setattr(sm, "_other_accounts",
                            lambda mw: ["acc2"] if self.other.poll() is None else [])
        monkeypatch.setattr(sm, "SLOT_ATTEMPTS", 3)
        mw = sm._state["mw"]
        mw.global_dir, mw.account_id = self.gd, "acc1"

    def close(self):
        if self.other.poll() is None:
            self.other.kill()
            self.other.wait()

    def a_new_account_may_start(self):
        """main.py's start-up gate for a new account process."""
        return not self.uc.is_update_in_progress(self.gd)


@pytest.fixture
def accounts(monkeypatch, tmp_path):
    made = []

    def make(quits=True):
        made.append(_Accounts(monkeypatch, tmp_path, quits))
        return made[-1]
    yield make
    for a in made:
        a.close()


def _found(item=None):
    replies, shown = [], []
    sm.on_update_found(item or _item(), S.STAGE_NOT_DOWNLOADED, replies.append, shown.append)
    return replies, shown


class TestInstallChosenInTheUpdateWindow:
    def test_the_other_accounts_quit_and_none_can_start_before_sparkle_downloads(
            self, bridge, accounts):
        accounts = accounts()
        assert accounts.a_new_account_may_start()
        replies, shown = _found()
        assert replies == [] and len(shown) == 1         # the window got a wrapped reply
        shown[0](S.CHOICE_INSTALL)
        assert accounts.quit_requests == ["acc2"]
        assert replies == [S.CHOICE_INSTALL]              # only now does Sparkle download
        assert not accounts.a_new_account_may_start()     # held until the update is over
        assert "update_closing_other_accounts" in sm._state["mw"].spoken

    def test_an_account_that_will_not_quit_means_no_download(self, bridge, accounts):
        accounts = accounts(quits=False)
        replies, shown = _found()
        shown[0](S.CHOICE_INSTALL)
        assert replies == [S.CHOICE_DISMISS]              # aborts, nothing downloaded
        assert accounts.a_new_account_may_start() and sm._state["slot"] is None
        assert "update_other_accounts_running" in sm._state["shown"][0]

    def test_a_failed_slot_never_spawns_an_account_that_did_not_quit(self, bridge, accounts, monkeypatch):
        accounts = accounts(quits=False)                 # a real process: spawn it first
        spawned = []
        monkeypatch.setattr(sm.subprocess, "Popen", lambda cmd, **kw: spawned.append(cmd))
        replies, shown = _found()
        shown[0](S.CHOICE_INSTALL)
        assert replies == [S.CHOICE_DISMISS] and spawned == []

    @pytest.mark.parametrize("choice", [S.CHOICE_SKIP, S.CHOICE_DISMISS])
    def test_skip_and_later_are_the_users_choice_and_quit_no_one(self, bridge, accounts, choice):
        accounts = accounts()
        replies, shown = _found()
        shown[0](choice)
        assert replies == [choice] and accounts.quit_requests == []

    @pytest.mark.parametrize("item", [
        lambda: _item(url="https://evil.example/WinZapp-macOS-arm64.zip"),
        lambda: _item(deltas={"2.1.0.42501": "x"}), lambda: _item(kind="package"),
        lambda: _item(tag="v2.1.0.4249alpha"),
    ])
    def test_a_resumed_item_is_checked_again_and_refused_without_a_window(self, bridge, item):
        """A resumed update skipped the veto (SPUBasicUpdateDriver.m:150)."""
        replies, shown = _found(item())
        assert replies == [S.CHOICE_DISMISS] and shown == []

    def test_an_install_prepared_by_another_process_is_left_alone(self, bridge):
        """Never Skip there: it would cancel that process's installer and hide
        the release from every account's scheduled checks."""
        replies, shown = [], []
        sm.on_update_found(_item(), S.STAGE_INSTALLING, replies.append, shown.append)
        assert replies == [S.CHOICE_DISMISS] and shown == []

    def test_a_window_that_cannot_be_shown_refuses(self, bridge):
        replies = []

        def broken(reply):
            raise RuntimeError("no window")
        sm.on_update_found(_item(), S.STAGE_NOT_DOWNLOADED, replies.append, broken)
        assert replies == [S.CHOICE_DISMISS]


class TestReadyToInstall:
    """The last answer before Sparkle installs."""

    def _ready(self, monkeypatch, verdict, item=None):
        monkeypatch.setattr(sm.updater_mac, "release_provenance",
                            lambda version: (b"raw" if verdict[0] else None, verdict[1]))
        sm._state["offered"] = TAG
        sm._state["offered_item"] = sm._item(item or _item())
        sm._state["slot"] = {"owner_token": "t"}
        released = []
        monkeypatch.setattr(sm, "_release_update_slot", lambda: released.append(True))
        replies, shown = [], []
        sm.on_ready_to_install(replies.append, shown.append)
        return replies, shown[0], released

    def test_a_fresh_yes_installs_and_only_then_says_installing(self, bridge, monkeypatch):
        replies, user, released = self._ready(monkeypatch, (True, "ok"))
        assert "update_installing" not in sm._state["mw"].spoken
        user(S.CHOICE_INSTALL)
        assert replies == [S.CHOICE_INSTALL] and released == []
        assert sm._state["mw"].spoken[-1] == "update_installing" and sm._state["installing"]

    @pytest.mark.parametrize("verdict", [
        (False, "could not verify commit provenance: the official repository has no such tag"),
        (False, "could not verify commit provenance: GitHub unreachable or rate-limited, try again later"),
    ])
    def test_a_withdrawn_tag_or_unreachable_github_cancels(self, bridge, monkeypatch, verdict):
        replies, user, released = self._ready(monkeypatch, verdict)
        user(S.CHOICE_INSTALL)
        assert replies == [S.CHOICE_SKIP] and released == [True]
        assert verdict[1] in sm._state["shown"][0]

    def test_the_item_is_checked_again_too(self, bridge, monkeypatch):
        replies, user, released = self._ready(monkeypatch, (True, "ok"), item=_item(kind="package"))
        user(S.CHOICE_INSTALL)
        assert replies == [S.CHOICE_SKIP]

    def test_a_crash_in_the_check_cancels(self, bridge, monkeypatch):
        replies, user, released = self._ready(monkeypatch, (True, "ok"))
        monkeypatch.setattr(sm.policy, "decide", lambda *a: 1 / 0)
        user(S.CHOICE_INSTALL)
        assert replies == [S.CHOICE_SKIP]

    @pytest.mark.parametrize("choice", [S.CHOICE_DISMISS, S.CHOICE_SKIP])
    def test_anything_but_install_cancels(self, bridge, monkeypatch, choice):
        replies, user, released = self._ready(monkeypatch, (True, "ok"))
        user(choice)
        assert replies == [S.CHOICE_SKIP] and released == [True]

    def test_an_install_of_a_release_never_offered_here_cancels(self, bridge):
        replies, shown = [], []
        sm.on_ready_to_install(replies.append, shown.append)
        shown[0](S.CHOICE_INSTALL)
        assert replies == [S.CHOICE_SKIP]

    def test_a_window_that_cannot_be_shown_cancels(self, bridge):
        replies = []

        def broken(reply):
            raise RuntimeError("no window")
        sm.on_ready_to_install(replies.append, broken)
        assert replies == [S.CHOICE_SKIP]

    def test_two_answers_race_and_sparkle_gets_exactly_one(self, bridge, monkeypatch):
        """A quit (shutdown thread) and the check's end (main thread) answer
        the same reply: the lock lets one through."""
        monkeypatch.setattr(sm, "CANCEL_CONFIRM_SECONDS", 0)
        replies, user, released = self._ready(monkeypatch, (True, "ok"))
        reply = sm._state["outstanding"]
        barrier = _RealBarrier(2, timeout=5)

        def quit_now():
            barrier.wait()
            sm.cancel_pending_install()

        def check_ends():
            barrier.wait()
            sm._ready_checked(reply, TAG, sm._state["offered_item"], (True, "ok"))
        threads = [_RealThread(target=f) for f in (quit_now, check_ends)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        assert len(replies) == 1


class TestReadyToInstallIsHeard:
    """Sparkle shows "Ready to Install" without making it key, so WinZapp's
    window kept VoiceOver: it is said, and brought forward when that takes
    focus from no one."""

    @pytest.fixture
    def ready(self, bridge, monkeypatch):
        from winzapp_mac import notify_mac
        posted, focused = [], []
        monkeypatch.setattr(notify_mac, "post_plain",
                            lambda ident, title, body, on_open=None: posted.append((ident, title, body, on_open)))

        class Driver:
            def showUpdateInFocus(self):
                focused.append(True)
        sm._state["driver"] = Driver()
        sm._state["offered"] = TAG
        sm._state["mw"].i18n.t = lambda key: {"mac_update_ready_to_install": "ready {new_version}"}.get(key, key)
        return posted, focused

    def _reach_ready(self, monkeypatch, active, user):
        monkeypatch.setattr(sm, "_app_is_active", lambda: active)
        sm._state["user_initiated"] = user
        sm.on_ready_to_install(lambda c: None, lambda r: None)

    @pytest.mark.parametrize("active,user", [(True, False), (True, True), (False, True)])
    def test_brought_forward_when_that_takes_focus_from_no_one(self, ready, monkeypatch, active, user):
        posted, focused = ready
        self._reach_ready(monkeypatch, active, user)
        assert sm._state["mw"].spoken == ["ready 2.1.0.4258alpha"]
        assert focused == [True] and posted == []

    def test_after_a_scheduled_check_in_the_background_it_is_announced_not_focused(self, ready, monkeypatch):
        posted, focused = ready
        self._reach_ready(monkeypatch, active=False, user=False)
        assert sm._state["mw"].spoken == ["ready 2.1.0.4258alpha"] and focused == []
        ident, title, body, on_open = posted[0]
        assert (ident, title, body) == (sm.UPDATE_NOTIFICATION, "update_available_title", "ready 2.1.0.4258alpha")
        on_open()
        assert focused == [True]

    def test_the_check_kind_comes_from_the_update_window(self, bridge):
        sm.on_update_found(_item(), S.STAGE_NOT_DOWNLOADED, lambda c: None, lambda r: None,
                           user_initiated=True)
        assert sm._state["user_initiated"] is True
        sm.on_update_found(_item(), S.STAGE_NOT_DOWNLOADED, lambda c: None, lambda r: None)
        assert sm._state["user_initiated"] is False


class TestQuitting:
    def _sparkle_ends_the_cycle(self, delegate, replies):
        """What Sparkle does with Skip at Ready to Install: ends the update
        cycle, which reaches the delegate (SPUUpdater.m:781-812)."""
        def reply(choice):
            replies.append(choice)
            _send(None, delegate, b"updater:didFinishUpdateCycleForUpdateCheck:error:",
                  _obj(delegate), (ctypes.c_long, 0), (_ID, None))
        return reply

    def test_quitting_at_ready_to_install_cancels_and_waits_for_sparkle(self, bridge, monkeypatch):
        replies = []
        sm._state["offered"] = TAG
        sm.on_ready_to_install(replies.append, lambda r: None)
        sm._cycle_done.set()                             # a stale confirmation must not count
        sm._state["outstanding"] = self._sparkle_ends_the_cycle(bridge, replies)
        started = sm.time.monotonic()
        sm.on_quit()
        assert replies == [S.CHOICE_SKIP] and sm._state["outstanding"] is None
        assert sm._cycle_done.is_set()
        assert sm.time.monotonic() - started < sm.CANCEL_CONFIRM_SECONDS
        sm.cancel_pending_install()                      # answered once only
        assert replies == [S.CHOICE_SKIP]

    def test_quitting_at_ready_to_install_reopens_nothing(self, bridge, monkeypatch):
        """The review's repro: the cycle Sparkle ends during the quit must
        not start the closed accounts again, nor speak, during a shutdown."""
        spawned, released = [], []
        monkeypatch.setattr(sm.subprocess, "Popen", lambda cmd, **kw: spawned.append(cmd))
        monkeypatch.setattr(sm, "_release_update_slot",
                            lambda: released.append(True) or sm._state.update(slot=None))
        sm._state["closed_accounts"] = ["acc2"]
        sm._state["slot"] = {"owner_token": "t"}
        sm._state["offered"] = TAG
        sm._state["outstanding"] = self._sparkle_ends_the_cycle(bridge, [])
        sm.on_quit()                                     # Cmd+Q / logout / SIGTERM
        assert spawned == []
        assert "mac_update_reopened_other_accounts" not in sm._state["mw"].spoken
        assert released                                  # the slot still goes back

    def test_a_second_quit_waits_for_the_first_ones_cancel(self, bridge, monkeypatch):
        """A signal during the first quit's wait: the second on_quit (after
        which main.py os._exits) returns only once Sparkle confirmed."""
        replies, seen = [], []
        sm._state["offered"] = TAG
        sm._state["outstanding"] = replies.append       # Sparkle has not confirmed yet

        def second_quit_mid_wait(choice):
            replies.append(choice)
            _RealThread(target=lambda: (time.sleep(0.3), sm._cycle_done.set())).start()
            sm.on_quit()                                 # re-entrant, as from a signal handler
            seen.append(sm._cycle_done.is_set())
        sm._state["outstanding"] = second_quit_mid_wait
        sm.on_quit()
        assert replies == [S.CHOICE_SKIP] and seen == [True]

    def test_no_confirmation_is_logged_and_the_quit_goes_on(self, bridge, monkeypatch, caplog):
        monkeypatch.setattr(sm, "CANCEL_CONFIRM_SECONDS", 0.05)
        replies = []
        sm._state["outstanding"] = replies.append
        sm.cancel_pending_install()
        assert replies == [S.CHOICE_SKIP]
        assert "did not confirm the cancel" in caplog.text

    def test_a_late_answer_after_the_quit_is_dropped(self, bridge, monkeypatch):
        monkeypatch.setattr(sm, "CANCEL_CONFIRM_SECONDS", 0)
        replies = []
        sm._state["outstanding"] = replies.append
        reply = sm._state["outstanding"]
        sm.cancel_pending_install()
        sm._ready_checked(reply, TAG, sm._item(_item()), (True, "ok"))
        assert replies == [S.CHOICE_SKIP]

    def test_the_cycle_end_confirms_and_gives_an_unused_slot_back(self, bridge, monkeypatch):
        released = []
        monkeypatch.setattr(sm, "_release_update_slot", lambda: released.append(True))
        sm._state["slot"] = {"owner_token": "t"}
        sm._cycle_done.clear()
        _send(None, bridge, b"updater:didFinishUpdateCycleForUpdateCheck:error:",
              _obj(bridge), (ctypes.c_long, 0), (_ID, None))
        assert sm._cycle_done.is_set() and released == [True]
        released.clear()
        sm._state["installing"] = True                   # Install confirmed: held until we exit
        _send(None, bridge, b"updater:didFinishUpdateCycleForUpdateCheck:error:",
              _obj(bridge), (ctypes.c_long, 0), (_ID, None))
        assert released == []

    def test_every_quit_path_goes_through_on_quit_first(self, bridge, monkeypatch):
        from main_window import session_lifecycle, settings, updates, window_lifecycle
        for owner, name in ((updates.UpdatesMixin, "_start_update_checker"),
                            (updates.UpdatesMixin, "_on_force_update"),
                            (window_lifecycle.WindowLifecycleMixin, "_perform_shutdown"),
                            (session_lifecycle.SessionLifecycleMixin, "_on_query_end_session"),
                            (settings.SettingsMixin, "choose_global_settings"),
                            (settings.SettingsMixin, "_apply_pulled_global_settings")):
            monkeypatch.setattr(owner, name, getattr(owner, name))      # restored after the test
        order = []
        monkeypatch.setattr(window_lifecycle.WindowLifecycleMixin, "_perform_shutdown",
                            lambda self: order.append("teardown") or True)
        monkeypatch.setattr(session_lifecycle.SessionLifecycleMixin, "_on_query_end_session",
                            lambda self, event: order.append("query"))
        monkeypatch.setattr(sm, "enabled", lambda: True)
        monkeypatch.setattr(sm, "on_quit", lambda: order.append("on_quit"))
        sm.install()
        window_lifecycle.WindowLifecycleMixin._perform_shutdown(object())
        session_lifecycle.SessionLifecycleMixin._on_query_end_session(object(), None)
        assert order == ["on_quit", "teardown", "on_quit", "query"]


class TestWhenTheUpdateDoesNotInstall:
    """Items closed for an update that then does not install come back."""

    def _closed(self, monkeypatch):
        spawned, order = [], []
        monkeypatch.setattr(sm.subprocess, "Popen", lambda cmd, **kw: spawned.append(cmd))
        monkeypatch.setattr(sm, "_release_update_slot",
                            lambda: order.append("slot") or sm._state.update(slot=None))
        sm._state["closed_accounts"] = ["acc2", "acc3"]
        sm._state["slot"] = {"owner_token": "t"}
        return spawned, order

    def test_a_failed_download_gives_the_slot_back_and_reopens_the_accounts(self, bridge, monkeypatch):
        spawned, order = self._closed(monkeypatch)
        err = sm._error(2001, "network")                  # SUDownloadError
        _send(None, bridge, b"updater:didFinishUpdateCycleForUpdateCheck:error:",
              _obj(bridge), (ctypes.c_long, 0), _obj(err))
        assert order == ["slot"]
        assert [c[c.index("--account") + 1] for c in spawned] == ["acc2", "acc3"]
        assert all("--background" in c for c in spawned)
        assert "mac_update_reopened_other_accounts" in sm._state["mw"].spoken
        assert sm._state["closed_accounts"] == []

    def test_sparkle_giving_up_after_install_was_confirmed_resets_it(self, bridge, monkeypatch):
        spawned, order = self._closed(monkeypatch)
        sm._state["installing"] = True
        err = sm._error(4005, "installation failed")      # SUInstallationError
        _send(None, bridge, b"updater:didFinishUpdateCycleForUpdateCheck:error:",
              _obj(bridge), (ctypes.c_long, 0), _obj(err))
        assert sm._state["installing"] is False and order == ["slot"] and len(spawned) == 2

    def test_a_refused_last_check_reopens_them_too(self, bridge, monkeypatch):
        spawned, order = self._closed(monkeypatch)
        monkeypatch.setattr(sm.updater_mac, "release_provenance", lambda v: (None, "withdrawn"))
        sm._state["offered"], sm._state["offered_item"] = TAG, sm._item(_item())
        replies, shown = [], []
        sm.on_ready_to_install(replies.append, shown.append)
        shown[0](S.CHOICE_INSTALL)
        assert replies == [S.CHOICE_SKIP] and order == ["slot"] and len(spawned) == 2

    def test_the_slot_goes_back_before_the_accounts_start(self, bridge, monkeypatch):
        """They would exit at start-up while the slot is held (main.py)."""
        order = []
        monkeypatch.setattr(sm, "_release_update_slot", lambda: order.append("slot"))
        monkeypatch.setattr(sm.subprocess, "Popen", lambda cmd, **kw: order.append("spawn"))
        sm._state["closed_accounts"] = ["acc2"]
        sm._update_abandoned()
        assert order == ["slot", "spawn"]


class TestQuitPaths:
    def test_a_quit_during_the_download_gives_the_slot_back(self, bridge, monkeypatch):
        released = []
        monkeypatch.setattr(sm, "_release_update_slot", lambda: released.append(True))
        sm._state["slot"] = {"owner_token": "t"}                       # no Ready reply yet
        sm.on_quit()
        assert released == [True]

    def test_after_install_was_confirmed_the_slot_stays_held(self, bridge, monkeypatch):
        released = []
        monkeypatch.setattr(sm, "_release_update_slot", lambda: released.append(True))
        sm._state["slot"], sm._state["installing"] = {"owner_token": "t"}, True
        sm.on_quit()
        assert released == []

    @pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
    def test_sigint_and_sigterm_cancel_first_then_chain(self, bridge, monkeypatch, sig):
        """main.py's handler ends in os._exit(0), past every quit path."""
        order = []
        saved = {s_: signal.getsignal(s_) for s_ in (signal.SIGINT, signal.SIGTERM)}
        try:
            signal.signal(sig, lambda signum, frame: order.append("main.py handler"))
            monkeypatch.setattr(sm, "on_quit", lambda: order.append("on_quit"))
            sm._chain_signals()
            signal.getsignal(sig)(sig, None)
            assert order == ["on_quit", "main.py handler"]
        finally:
            for s_, handler in saved.items():
                signal.signal(s_, handler)


class FakeUpdateState(NSObject):
    """What the reminder reads of SPUUserUpdateState."""

    def userInitiated(self):
        return self._user


class TestScheduledUpdateReminders:
    """Sparkle's standard window waits for WinZapp to be activated
    (SPUStandardUserDriver.m:263-271, 316-332); a blind user must hear that
    an update is there, as WinZapp's own updater says it."""

    @pytest.fixture
    def reminders(self, bridge, monkeypatch):
        from winzapp_mac import notify_mac
        posted, removed = [], []
        monkeypatch.setattr(notify_mac, "post_plain",
                            lambda ident, title, body, on_open=None: posted.append((ident, title, body, on_open)))
        monkeypatch.setattr(notify_mac, "remove_plain", lambda ident: removed.append(ident))
        sm._state["mw"].i18n.t = lambda key: {"mac_update_available_reminder": "new {new_version}"}.get(key, key)
        return sm.WinZappSparkleUserDriverDelegate.alloc().init(), posted, removed

    def _will_show(self, driver_delegate, user, immediate):
        item = _item()          # held: _obj() passes a bare pointer
        _send(ctypes.c_bool, driver_delegate,
              b"standardUserDriverShouldHandleShowingScheduledUpdate:andInImmediateFocus:",
              _obj(item), (ctypes.c_bool, immediate))
        state = FakeUpdateState.alloc().init()
        state._user = user
        _send(None, driver_delegate, b"standardUserDriverWillHandleShowingUpdate:forUpdate:state:",
              (ctypes.c_bool, True), _obj(item), _obj(state))

    def test_gentle_reminders_are_declared(self, reminders):
        driver_delegate, _, _ = reminders
        assert _send(ctypes.c_bool, driver_delegate, b"supportsGentleScheduledUpdateReminders") is True
        # and Sparkle keeps showing the update itself
        item = _item()
        assert _send(ctypes.c_bool, driver_delegate,
                     b"standardUserDriverShouldHandleShowingScheduledUpdate:andInImmediateFocus:",
                     _obj(item), (ctypes.c_bool, False)) is True

    def test_a_deferred_scheduled_update_is_spoken_and_notified(self, reminders, monkeypatch):
        driver_delegate, posted, _ = reminders
        rechecks = []
        monkeypatch.setattr(sm, "_recheck", lambda user, attempt=0: rechecks.append(user))
        self._will_show(driver_delegate, user=False, immediate=False)
        assert sm._state["mw"].spoken == ["new 2.1.0.4258alpha"]
        ident, title, body, on_open = posted[0]
        assert (ident, title, body) == (sm.UPDATE_NOTIFICATION, "update_available_title", "new 2.1.0.4258alpha")
        on_open()                               # activating it opens Sparkle's window
        assert rechecks == [True]

    def test_a_check_the_user_asked_for_is_not_announced(self, reminders):
        driver_delegate, posted, _ = reminders
        self._will_show(driver_delegate, user=True, immediate=False)
        assert posted == [] and sm._state["mw"].spoken == []

    def test_the_reminder_goes_once_the_user_attends_to_the_update(self, reminders):
        driver_delegate, _, removed = reminders
        item = _item()
        _send(None, driver_delegate, b"standardUserDriverDidReceiveUserAttentionForUpdate:", _obj(item))
        _send(None, driver_delegate, b"standardUserDriverWillFinishUpdateSession")
        assert removed == [sm.UPDATE_NOTIFICATION, sm.UPDATE_NOTIFICATION]


def test_every_successful_step_is_logged(bridge, accounts, monkeypatch, caplog):
    """A run can be read from log.log alone."""
    import logging as _logging
    caplog.set_level(_logging.INFO)
    monkeypatch.setattr(sm.updater_mac, "release_provenance", lambda v: (
        P.build_provenance(TAG, "c" * 40, {"WinZapp-macOS-arm64.zip": "a" * 64}), "ok"))
    assert sm._ask(TAG) == (True, "ok")
    accounts = accounts()
    replies, shown = _found()
    shown[0](S.CHOICE_INSTALL)
    request = NSMutableURLRequest.requestWithURL_(NSURL.URLWithString_(ASSET))
    delegate, item = sm._state["delegate"] or sm.WinZappSparkleDelegate.alloc().init(), _item()
    _send(None, delegate, b"updater:willDownloadUpdate:withRequest:", _obj(delegate), _obj(item), _obj(request))
    _send(None, delegate, b"updater:didDownloadUpdate:", _obj(delegate), _obj(item))
    ready = []
    sm.on_ready_to_install(lambda choice: None, ready.append)
    ready[0](S.CHOICE_INSTALL)
    text = caplog.text
    for line in ("official tag at commit cccccccccccc", "appcast item checks pass", "Install chosen",
                 "other accounts asked to quit", "update slot claimed", "download started",
                 "download finished", "Ready to Install", "last check OK", "Install handed to Sparkle"):
        assert line in text, line


class TestScheduledChecks:
    def _may(self, delegate, check):
        err = ctypes.c_void_p(0)
        ok = _send(ctypes.c_bool, delegate, b"updater:mayPerformUpdateCheck:error:",
                   _obj(delegate), (ctypes.c_long, check), (ctypes.POINTER(_ID), ctypes.byref(err)))
        return ok, err.value

    def test_the_user_can_always_check(self, bridge):
        sm._state["mw"].settings = {"general": {"updates_enabled": False}}
        assert self._may(bridge, S.CHECK_USER) == (True, None)

    def test_off_in_settings_means_no_scheduled_check(self, bridge):
        sm._state["mw"].settings = {"general": {"updates_enabled": False}}
        ok, err = self._may(bridge, S.CHECK_BACKGROUND)
        assert ok is False and err

    def test_one_account_runs_the_scheduled_checks(self, bridge, monkeypatch):
        sm._state["mw"].global_dir = "/nonexistent"
        import update_coord
        monkeypatch.setattr(update_coord, "try_claim_update_prompt", lambda *a, **k: None)
        assert self._may(bridge, S.CHECK_BACKGROUND)[0] is False
        monkeypatch.setattr(update_coord, "try_claim_update_prompt", lambda gd, v, name: {"t": 1}
                            if name == S.CLAIM_FILE else None)
        assert self._may(bridge, S.CHECK_BACKGROUND)[0] is True

    def test_an_exception_refuses_the_check(self, bridge, monkeypatch):
        monkeypatch.setattr(sm, "_settings", lambda: 1 / 0)
        assert self._may(bridge, S.CHECK_BACKGROUND)[0] is False

    def test_the_alpha_setting_is_the_alpha_channel(self, bridge):
        sm._state["mw"].settings = {"general": {"alpha_updates_enabled": True}}
        got = objc.objc_object(c_void_p=_send(_ID, bridge, b"allowedChannelsForUpdater:", _obj(bridge)))
        assert set(got) == {"alpha"}
        sm._state["mw"].settings = {"general": {}}
        got = objc.objc_object(c_void_p=_send(_ID, bridge, b"allowedChannelsForUpdater:", _obj(bridge)))
        assert set(got) == set()

    def test_turning_automatic_checks_on_or_off_applies_at_once(self, bridge, monkeypatch):
        class Updater:
            auto = False

            def automaticallyChecksForUpdates(self):
                return self.auto

            def setAutomaticallyChecksForUpdates_(self, value):
                self.auto = value
        sm._state["updater"] = Updater()
        sm._state["mw"].settings = {"general": {"updates_enabled": True}}
        sm._sync_automatic_checks()
        assert sm._state["updater"].auto is True
        sm._state["mw"].settings = {"general": {"updates_enabled": False}}
        sm._sync_automatic_checks()
        assert sm._state["updater"].auto is False


@pytest.mark.skipif(not os.path.isdir(FRAMEWORK), reason="Sparkle not unpacked (run build_app.py)")
def test_the_user_driver_subclass_receives_and_answers_sparkles_blocks(bridge, monkeypatch):
    """Against Sparkle 2.10.0 itself: the subclass's overrides get Sparkle's
    reply blocks as callables and an answer reaches the block. The standard
    windows are replaced by the test, so nothing is shown."""
    assert NSBundle.bundleWithPath_(FRAMEWORK).load()
    cls = sm._user_driver_class()
    driver = cls.alloc().initWithHostBundle_delegate_(NSBundle.mainBundle(), None)
    got = []

    def answer(reply, show):
        reply(S.CHOICE_SKIP)
    monkeypatch.setattr(sm, "on_ready_to_install", answer)
    # PyObjC turns the callable into an Objective-C block (the metadata
    # _user_driver_class registered), the call is dispatched by the runtime to
    # the Python override, which gets that block back and answers it.
    driver.showReadyToInstallAndRelaunch_(lambda choice: got.append(choice))
    assert got == [S.CHOICE_SKIP]
    monkeypatch.setattr(sm, "on_update_found",
                        lambda item, stage, reply, show, user_initiated=False:
                        got.append((stage, user_initiated)) or reply(S.CHOICE_DISMISS))
    state = objc.lookUpClass("SPUUserUpdateState").alloc()
    if state.respondsToSelector_(b"initWithStage:userInitiated:"):
        state = state.initWithStage_userInitiated_(S.STAGE_INSTALLING, True)
        item = _item()
        driver.showUpdateFoundWithAppcastItem_state_reply_(item, state, lambda c: got.append(c))
        assert got == [S.CHOICE_SKIP, (S.STAGE_INSTALLING, True), S.CHOICE_DISMISS]


def test_off_without_sparkle_keys(monkeypatch):
    """A build without the keys (every build until the release workflow sets
    WINZAPP_SPARKLE_PUBLIC_KEY) keeps updater_mac, untouched."""
    from main_window import updates
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sm.updater_mac, "_info", lambda: dict(INFO))
    before = updates.UpdatesMixin._on_force_update
    assert not sm.enabled()
    sm.install()
    assert updates.UpdatesMixin._on_force_update is before


def test_a_sparkle_that_cannot_start_falls_back_to_winzapps_updater(monkeypatch):
    from main_window import session_lifecycle, settings, updates, window_lifecycle
    for owner, name in ((window_lifecycle.WindowLifecycleMixin, "_perform_shutdown"),
                        (session_lifecycle.SessionLifecycleMixin, "_on_query_end_session"),
                        (settings.SettingsMixin, "choose_global_settings"),
                        (settings.SettingsMixin, "_apply_pulled_global_settings")):
        monkeypatch.setattr(owner, name, getattr(owner, name))
    called = []
    monkeypatch.setattr(sm, "enabled", lambda: True)
    monkeypatch.setattr(sm, "_start", lambda mw: False)
    monkeypatch.setattr(updates.UpdatesMixin, "_on_force_update", lambda self, e: called.append("old"))
    monkeypatch.setattr(updates.UpdatesMixin, "_start_update_checker",
                        lambda self, force=False: called.append(("old start", force)))
    sm.install()
    updates.UpdatesMixin._on_force_update(object(), None)
    updates.UpdatesMixin._start_update_checker(object(), True)
    assert called == ["old", ("old start", True)]


def test_other_accounts_use_the_strict_liveness_rule_of_the_slot_claim(monkeypatch):
    """A leaked lease the slot claim calls dead must not make the update wait
    for an account that is no longer running."""
    import node_coord
    from winzapp_mac import coord_mac
    seen = {}

    def live_node_leases(gd, is_alive):
        seen["is_alive"] = is_alive
        return [{"account_id": "acc1"}, {"account_id": "acc2"},
                {"account_id": "acc3", "_corrupt": True}]
    monkeypatch.setattr(node_coord, "live_node_leases", live_node_leases)
    mw = type("MW", (), {"global_dir": "gd", "account_id": "acc1"})()
    assert sm._other_accounts(mw) == ["acc2"]
    assert seen["is_alive"] is coord_mac.lease_alive
