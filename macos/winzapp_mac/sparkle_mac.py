"""Updates through Sparkle 2, gated by the official repository's tags.

A release build whose Info.plist names a Sparkle feed and key
(sparkle_policy.configured(), written by build_app.py from
WINZAPP_SPARKLE_PUBLIC_KEY) updates through Sparkle's standard windows, which
VoiceOver reads well; the "Check for updates" menu item and the automatic
check WinZapp starts after launch go to Sparkle instead of UpdateChecker.
Without those keys, or if Sparkle.framework cannot be loaded or started,
nothing here runs and updater_mac goes on exactly as before.

What WinZapp checks (sparkle_policy explains why only this): the release is
an existing tag of the official repository pointing at the commit its
provenance declares, newer than the running tag
(updater_mac.release_provenance()), and the appcast item stands for exactly
that tag's full zip. Sparkle downloads, verifies (signed appcast, EdDSA, code
signing) and installs with its own windows; nothing is downloaded before the
user chooses Install. With Sparkle 2.10.0's public API only:

1. Before Sparkle shows a release: updater:shouldProceedWithUpdate:...
   (SPUUpdaterDelegate.h:252), Sparkle's one veto before anything is shown
   or downloaded (SPUBasicUpdateDriver.m:150-158). It is synchronous on the
   main thread and the answer needs the network, so the first answer is a
   silent abort (SUInstallationCanceledError, SPUUIBasedUpdateDriver.m:
   482-483), the question runs on a thread, and its end starts the same kind
   of check again, which the stored verdict (at most VERDICT_TTL old) answers.
2. When the user chooses Install in the update window (WinZapp's subclass of
   Sparkle's standard user driver holds that reply,
   SPUUIBasedUpdateDriver.m:244-275): the appcast item is checked again
   (an update Sparkle resumes skipped step 1, SPUBasicUpdateDriver.m:150), the
   other accounts quit, and WinZapp claims update_coord's update slot, which
   keeps any account from starting until the update is over (main.py exits a
   new account process while is_update_in_progress()). Only then does Install
   reach Sparkle, which downloads and prepares its installer. This has to
   happen before the installer exists: it watches only the first running
   process of the app (InstallerProgressAppController.m:292-308) and installs
   as soon as that one ends (AppInstaller.m:403-410), so an account quitting
   later would install it, unchecked.
3. At "Ready to Install", the last answer Sparkle waits for
   (SPUUIBasedUpdateDriver.m:416-425): the official repository is asked again
   and the item checked again; only a fresh yes answers Install, anything
   else answers Skip, which there cancels the prepared installer
   (SPUCoreBasedUpdateDriver.m:313-314 -> SPUInstallerDriver.m:563-572 ->
   AppInstaller.m:681-684). updater:shouldPostponeRelaunchForUpdate: would
   come later, but it cannot cancel (an installer past its first stage
   installs whenever the app ends, AppInstaller.m:731-744), so it is not used.
4. Quitting while the answer at 3 is outstanding answers Skip first and waits
   for Sparkle to end that update cycle (cancel_pending_install()), so Sparkle
   never installs on quit; so does SIGINT/SIGTERM, whose handler in main.py
   ends in os._exit(). Quitting at any point before Install is confirmed gives
   the update slot back.

When the update does not install after the other accounts were closed for it
(the download failed or was cancelled, Sparkle aborted, the last check
refused), the slot is given back, those accounts are opened again in the
background, and WinZapp says so.

An install Sparkle resumes at the installing stage belongs to the process
that prepared it: here it is dismissed without a window. Dismiss there only
ends this check (SPUCoreBasedUpdateDriver.m:310-312); Skip would cancel the
other process's installer and mark the version as skipped for every account's
scheduled checks (SPUUIBasedUpdateDriver.m:282-283, SUAppcastDriver.m:228).

All Sparkle calls happen on the main thread, which is wx's: Sparkle calls the
delegate and the user driver there, and the threads come back through
wx.CallAfter. No Python exception crosses into Objective-C: every method
Sparkle calls refuses (and logs) instead.
"""

import logging
import os
import signal
import subprocess
import sys
import threading
import time

import objc
import wx
from Foundation import NSBundle, NSDate, NSDefaultRunLoopMode, NSError, NSObject, NSRunLoop, NSSet, NSURL

from . import coord_mac, provenance, sparkle_policy as policy, updater_mac

ARCH = updater_mac.ARCH
FRAMEWORK = "Sparkle.framework"
RECHECK_ATTEMPTS = 60             # one a second while Sparkle finishes the aborted cycle
NOT_NOW_RETRY_MS = 5 * 60 * 1000  # pairing or start-up still owns the screen
VERDICT_TTL = 10 * 60             # an offer-time answer older than this is asked again
SLOT_ATTEMPTS = 40                # the update slot, while the other accounts finish quitting
SLOT_WAIT = 0.5
CANCEL_CONFIRM_SECONDS = 3.0      # for Sparkle to end the cancelled cycle before we exit
CANCEL_STEP = 0.05
# Where a download that is not this release's zip is sent instead: https, so
# Sparkle accepts the request (SPUDownloader.m:65-81 refuses anything but
# http and https), and a closed local port, so it fails at once.
REFUSED_DOWNLOAD = "https://127.0.0.1:9/winzapp-refused-update.zip"
UPDATE_NOTIFICATION = "winzapp.sparkle.update"   # notify_mac identifier of the reminder

# Who answers the outstanding reply: exactly one path. Re-entrant, because a
# signal handler (main thread) can land while the main thread holds it.
_lock = threading.RLock()
_cycle_done = threading.Event()   # Sparkle ended an update cycle (didFinishUpdateCycle)
_state = {"mw": None, "updater": None, "driver": None, "delegate": None,
          "checking": "", "user_waiting": False, "offered": "", "offered_item": None,
          "outstanding": None, "slot": None, "installing": False, "closed_accounts": [],
          "quitting": False, "cancel_deadline": None, "driver_delegate": None,
          "immediate_focus": False, "user_initiated": False}
_verdicts = {}      # tag -> (monotonic time, (ok, detail)), for the next veto only

_CHOICE_BLOCK = {"callable": {"retval": {"type": b"v"},
                              "arguments": {0: {"type": b"^v"}, 1: {"type": b"q"}}}}


def framework_path():
    return os.path.join(updater_mac._bundle_path(), "Contents", "Frameworks", FRAMEWORK)


def enabled():
    """A release build (updater_mac.enabled()) whose Info.plist configures
    Sparkle and that carries the framework."""
    return (updater_mac.enabled() and policy.configured(updater_mac._info())
            and os.path.isdir(framework_path()))


def _error(code, text):
    return NSError.errorWithDomain_code_userInfo_(
        policy.ERROR_DOMAIN, code, {"NSLocalizedDescription": text})


def _settings():
    return getattr(_state["mw"], "settings", None) or {}


def _repo():
    return updater_mac.releases_repo(updater_mac._info())


def _running_tag():
    return provenance.running_release_tag(updater_mac._info())


def _say(text_of):
    """Speak *text_of*(i18n.t) through WinZapp's speech. The key is written
    as a literal t("...") at each call so tests/test_macos_layer_contract.py
    checks it exists in every locale."""
    mw = _state["mw"]
    try:
        mw.output(text_of(mw.i18n.t), interrupt=True)
    except Exception:
        logging.debug("[sparkle_mac] could not speak", exc_info=True)


def _show_refusal(detail):
    """The update did not go ahead: said and shown the way updater_mac says
    it (MacUpdateProgress._fail)."""
    mw = _state["mw"]
    try:
        i18n = mw.i18n
        text = f"{i18n.t('error').format(app_name='WinZapp')}\n\n{detail}"
        mw.output(text, interrupt=True)
        wx.MessageBox(text, i18n.t("update_available_title"), wx.OK | wx.ICON_ERROR, mw)
    except Exception:
        logging.exception("[sparkle_mac] could not show the refusal")


def _window_ready(mw):
    """UpdateChecker._main_window_ready(): no update window over start-up or
    pairing."""
    ready = getattr(mw, "_ui_ready_event", None)
    if ready is not None and not ready.is_set():
        return False
    may_run = getattr(mw, "wpp_update_may_run_now", None)
    return may_run is None or bool(may_run())


def _holds_claim(mw):
    """Every account is its own process and each loads Sparkle; only the one
    holding this claim (until it quits) runs the scheduled checks, so one
    update is not offered once per account. Fails open like
    UpdateChecker._claim_prompt: a check that cannot be coordinated is worth
    more than one suppressed by a bug in the coordination."""
    gd = getattr(mw, "global_dir", None)
    if not gd:
        return True
    try:
        import update_coord
        return update_coord.try_claim_update_prompt(gd, "sparkle", name=policy.CLAIM_FILE) is not None
    except Exception:
        logging.exception("[sparkle_mac] update claim failed — checking anyway")
        return True


def _other_accounts(mw):
    gd, acc_id = getattr(mw, "global_dir", None), getattr(mw, "account_id", None)
    if not (gd and acc_id):
        return []
    try:
        import node_coord
        import update_coord
        others = [l.get("account_id") for l in node_coord.live_node_leases(
            gd, is_alive=update_coord.lease_alive) if not l.get("_corrupt")]
        return sorted({a for a in others if a and a != acc_id})
    except Exception:
        logging.exception("[sparkle_mac] enumerating other accounts failed (non-fatal)")
        return []


def _quit_other_accounts(mw, others):
    """The peer walk of MainWindow.quit_all_accounts(), without exiting:
    each account closes its WhatsApp session and confirms before the next."""
    gd = getattr(mw, "global_dir", None)
    try:
        import ipc
        for other in others:
            try:
                logging.info("[sparkle_mac] asking account %s to quit before the update", other)
                ipc.request_quit(gd, other)
            except Exception:
                logging.exception("[sparkle_mac] request_quit failed for %s", other)
        if others:
            logging.info("[sparkle_mac] other accounts asked to quit: %d", len(others))
    except Exception:
        logging.exception("[sparkle_mac] quitting the other accounts failed")


def _claim_update_slot(mw):
    """update_coord's update slot, or None while another account still runs.
    Held, nothing else can start an account process (main.py) until it is
    released (_release_update_slot) or this process ends (a dead owner is
    recovered). The quitting accounts drop their leases as they exit, so a
    few attempts are allowed. Without a global dir there is no other account:
    a placeholder slot."""
    gd = getattr(mw, "global_dir", None)
    if not gd:
        return {}
    import update_coord
    for attempt in range(SLOT_ATTEMPTS):
        try:
            # coord_mac's strict rule: a leaked 0.0 lease, or one whose pid is
            # now some other program, is not an account that is still open.
            token = update_coord.try_begin_update(gd, is_alive=coord_mac.lease_alive)
        except Exception:
            logging.exception("[sparkle_mac] try_begin_update failed — not updating")
            return None
        if token is not None:
            return token
        time.sleep(SLOT_WAIT)
    return None


def _release_update_slot():
    token, _state["slot"] = _state["slot"], None
    gd = getattr(_state["mw"], "global_dir", None)
    if not (gd and token):
        return
    try:
        import update_coord
        update_coord.end_update(gd, token)
    except Exception:
        logging.exception("[sparkle_mac] end_update failed")


def _reopen_accounts(accounts):
    """Start again, in the background, the accounts closed for an update that
    did not install (the way account_launcher starts an account)."""
    if not accounts:
        return
    try:
        import account_launcher
        frozen = bool(getattr(sys, "frozen", False))
        for account in accounts:
            logging.info("[sparkle_mac] reopening account %s", account)
            subprocess.Popen(account_launcher.build_launch_command(
                sys.argv[0], sys.executable, account, frozen,
                startup_source="user", background=True))
        _say(lambda t: t("mac_update_reopened_other_accounts"))
    except Exception:
        logging.exception("[sparkle_mac] reopening the other accounts failed")


def _update_abandoned():
    """The update will not install: the slot goes back first, so the accounts
    closed for it can start again, and those that did close are opened again
    (one that never quit is still running). Not while this process is
    quitting: Sparkle ends the cancelled cycle during a quit or a logout, and
    accounts must not start then."""
    _state["installing"] = False
    _release_update_slot()
    accounts, _state["closed_accounts"] = _state["closed_accounts"], []
    if _state["quitting"] or not accounts:
        return
    running = set(_other_accounts(_state["mw"]))
    _reopen_accounts([a for a in accounts if a not in running])


def _item(item):
    url = item.fileURL()
    deltas = item.deltaUpdates()
    return {"version": str(item.versionString() or ""),
            "display_version": str(item.displayVersionString() or ""),
            "url": str(url.absoluteString()) if url is not None else "",
            "deltas": bool(deltas is not None and len(deltas)),
            "installation_type": str(item.installationType() or "")}


def _item_refusal(fields):
    """The item checks of sparkle_policy.decide() alone (versions name one
    tag, its zip over https, no deltas, an application, newer than running):
    (tag, "") when they pass, (tag, why) otherwise."""
    action, tag, detail = policy.decide(fields, _repo(), ARCH, _running_tag(), (True, "ok"))
    return tag, ("" if action == policy.PROCEED else detail)


def _ask(tag):
    """updater_mac.release_provenance() for *tag*, never raising: (ok, detail)."""
    try:
        raw, detail = updater_mac.release_provenance(provenance.tag_version(tag))
        if raw is not None:
            try:
                commit = provenance.parse_provenance(raw)["source_commit"][:12]
            except Exception:
                commit = "?"        # only the log line: the verdict is release_provenance's
            logging.info("[sparkle_mac] provenance OK: %s is an official tag at commit %s", tag, commit)
        return raw is not None, detail
    except Exception as exc:
        logging.exception("[sparkle_mac] provenance of %s failed", tag)
        return False, f"could not verify commit provenance: {type(exc).__name__}"


def _take(reply):
    """True for exactly one caller per reply: the one that answers it."""
    with _lock:
        if reply is not None and _state["outstanding"] is reply:
            _state["outstanding"] = None
            return True
        return False


def _call(reply, choice):
    try:
        reply(choice)
    except Exception:
        logging.exception("[sparkle_mac] answering Sparkle failed")


# -- 1. before Sparkle shows a release --------------------------------------------

def _pop_verdict(tag):
    entry = _verdicts.pop(tag, None)
    if entry is None or time.monotonic() - entry[0] > VERDICT_TTL:
        return None
    return entry[1]


def _check_offer(tag, check):
    """Ask the official repository about *tag* on a thread, then the same kind
    of check again on the main thread."""
    _state["checking"] = tag

    def work():
        verdict = _ask(tag)
        wx.CallAfter(_offer_checked, tag, check, verdict)

    threading.Thread(target=work, daemon=True, name="winzapp-sparkle-provenance").start()


def _offer_checked(tag, check, verdict):
    _state["checking"] = ""
    user = check == policy.CHECK_USER or _state["user_waiting"]
    _state["user_waiting"] = False
    if not verdict[0]:
        logging.error("[sparkle_mac] %s refused: %s", tag, verdict[1])
        if not user:
            # A scheduled check stays silent, as it always has; the next one
            # asks again (offline and rate limits are usually gone by then).
            return
    _verdicts[tag] = (time.monotonic(), verdict)    # a refusal: Sparkle's error window
    _recheck(user)


def _recheck(user, attempt=0):
    updater = _state["updater"]
    if updater is None:
        return
    try:
        if updater.sessionInProgress():
            if attempt < RECHECK_ATTEMPTS:
                wx.CallLater(1000, _recheck, user, attempt + 1)
            return
        if user:
            updater.checkForUpdates()
        else:
            updater.checkForUpdatesInBackground()
    except Exception:
        logging.exception("[sparkle_mac] could not check again")


# -- 2. Install chosen in the update window ---------------------------------------

def on_update_found(item, stage, reply, show, user_initiated=False):
    """SPUUserDriver showUpdateFoundWithAppcastItem:state:reply:. *show* hands
    a reply to Sparkle's standard window."""
    try:
        _state["user_initiated"] = bool(user_initiated)
        if stage == policy.STAGE_INSTALLING:
            # Another process prepared this installer and answers for it.
            logging.info("[sparkle_mac] an install prepared elsewhere is pending: leaving it alone")
            return _call(reply, policy.CHOICE_DISMISS)
        fields = _item(item)
        tag, why = _item_refusal(fields)
        if why:
            logging.error("[sparkle_mac] not offering %s: %s", tag or "an appcast item", why)
            return _call(reply, policy.CHOICE_DISMISS)
        logging.info("[sparkle_mac] showing %s (appcast item checks pass)", tag)
        _state["offered"], _state["offered_item"] = tag, fields
        show(lambda choice: on_update_choice(choice, reply))
    except Exception:
        logging.exception("[sparkle_mac] update found: refusing")
        _call(reply, policy.CHOICE_DISMISS)


def on_update_choice(choice, reply):
    """The user answered the update window. Skip and Later go to Sparkle as
    they are (that is the user's choice for this release); Install first
    closes the other accounts and claims the update slot."""
    try:
        if choice != policy.CHOICE_INSTALL:
            logging.info("[sparkle_mac] %s: the user chose %s", _state["offered"] or "update",
                         "skip" if choice == policy.CHOICE_SKIP else "later")
            return _call(reply, choice)
        mw = _state["mw"]
        others = _other_accounts(mw)
        logging.info("[sparkle_mac] Install chosen for %s; closing %d other account(s) first",
                     _state["offered"] or "update", len(others))
        if others:
            _say(lambda t: t("update_closing_other_accounts"))

        _state["closed_accounts"] = list(others)

        def work():
            _quit_other_accounts(mw, others)
            slot = _claim_update_slot(mw)
            wx.CallAfter(_update_slot_claimed, reply, slot)

        threading.Thread(target=work, daemon=True, name="winzapp-sparkle-slot").start()
    except Exception:
        logging.exception("[sparkle_mac] install chosen: refusing")
        _call(reply, policy.CHOICE_DISMISS)


def _update_slot_claimed(reply, slot):
    try:
        if slot is None:
            logging.error("[sparkle_mac] another account is still running: not updating")
            _call(reply, policy.CHOICE_DISMISS)
            _update_abandoned()
            mw = _state["mw"]
            return _show_refusal(mw.i18n.t("update_other_accounts_running"))
        _state["slot"], _state["installing"] = slot, False
        logging.info("[sparkle_mac] update slot claimed (no account can start); Sparkle downloads %s",
                     _state["offered"] or "the update")
        _call(reply, policy.CHOICE_INSTALL)
    except Exception:
        logging.exception("[sparkle_mac] could not go on with the update")
        _call(reply, policy.CHOICE_DISMISS)
        _update_abandoned()


# -- 3. Ready to Install -------------------------------------------------------------

def on_ready_to_install(reply, show):
    """SPUUserDriver showReadyToInstallAndRelaunch: (its window has only
    "Install and Relaunch", SPUStandardUserDriver.m:514)."""
    try:
        with _lock:
            _state["outstanding"] = reply
        logging.info("[sparkle_mac] %s downloaded and prepared: Ready to Install", _state["offered"] or "update")
        show(lambda choice: on_ready_choice(choice, reply))
        announce_ready_to_install()
    except Exception:
        logging.exception("[sparkle_mac] ready to install: cancelling")
        if _take(reply):
            _call(reply, policy.CHOICE_SKIP)
            _update_abandoned()


def on_ready_choice(choice, reply):
    """Install goes ahead only if the release still holds, asked again now."""
    _forget_announcement()
    try:
        tag, fields = _state["offered"], _state["offered_item"]
        if choice != policy.CHOICE_INSTALL or not tag or fields is None:
            if _take(reply):
                logging.info("[sparkle_mac] install of %s not confirmed: cancelling it", tag or "?")
                _call(reply, policy.CHOICE_SKIP)
                _update_abandoned()
            return

        def work():
            verdict = _ask(tag)
            wx.CallAfter(_ready_checked, reply, tag, fields, verdict)

        threading.Thread(target=work, daemon=True, name="winzapp-sparkle-install-check").start()
    except Exception:
        logging.exception("[sparkle_mac] ready choice: cancelling")
        if _take(reply):
            _call(reply, policy.CHOICE_SKIP)
            _update_abandoned()


def _ready_checked(reply, tag, fields, verdict):
    try:
        action, _, detail = policy.decide(fields, _repo(), ARCH, _running_tag(), verdict)
        choice = policy.install_choice(
            policy.CHOICE_INSTALL, verdict if action == policy.PROCEED else (False, detail))
    except Exception:
        logging.exception("[sparkle_mac] install check failed")
        choice, detail = policy.CHOICE_SKIP, "could not check this update"
    if not _take(reply):
        return      # already cancelled by a quit
    if choice == policy.CHOICE_INSTALL:
        logging.info("[sparkle_mac] last check OK: %s still holds (official tag, item checks)", tag)
        _state["installing"] = True
        _say(lambda t: t("update_installing"))
        _call(reply, policy.CHOICE_INSTALL)
        logging.info("[sparkle_mac] Install handed to Sparkle for %s", tag)
        return
    logging.error("[sparkle_mac] not installing %s: %s", tag, detail)
    _call(reply, policy.CHOICE_SKIP)
    _update_abandoned()
    _show_refusal(detail)


# -- 4. quitting -------------------------------------------------------------------

def cancel_pending_install():
    """WinZapp is quitting with "Ready to Install" unanswered: cancel the
    prepared installer, or it would install when this process ends
    (AppInstaller.m:403-410) without the last check. Sparkle sends the cancel
    from its main queue (SPUUIBasedUpdateDriver.m:420-425) and then ends the
    update cycle (SPUInstallerDriver.m:563-572, installerIsRequestingAbort...),
    which updater:didFinishUpdateCycleForUpdateCheck:error: reports: that is
    the confirmation waited for, at most CANCEL_CONFIRM_SECONDS (the
    hard-exit deadline of real_exit still applies)."""
    with _lock:
        reply, _state["outstanding"] = _state["outstanding"], None
    if reply is None:
        return
    logging.info("[sparkle_mac] quitting before the install was confirmed: cancelling it")
    _cycle_done.clear()
    _state["cancel_deadline"] = time.monotonic() + CANCEL_CONFIRM_SECONDS
    _call(reply, policy.CHOICE_SKIP)
    if not _wait_for_cancel():
        logging.error("[sparkle_mac] Sparkle did not confirm the cancel within %ss",
                      CANCEL_CONFIRM_SECONDS)


def _wait_for_cancel():
    """Wait until Sparkle ends the cancelled cycle, or the cancel's deadline:
    on the main thread by running its run loop in short steps (Sparkle sends
    the cancel from the main queue), elsewhere on the Event. True when
    confirmed, or when no cancel was sent."""
    deadline = _state["cancel_deadline"]
    if deadline is None:
        return True
    try:
        if threading.current_thread() is threading.main_thread():
            loop = NSRunLoop.mainRunLoop()
            while not _cycle_done.is_set() and time.monotonic() < deadline:
                loop.runMode_beforeDate_(NSDefaultRunLoopMode,
                                         NSDate.dateWithTimeIntervalSinceNow_(CANCEL_STEP))
            return _cycle_done.is_set()
        return _cycle_done.wait(max(0.0, deadline - time.monotonic()))
    except Exception:
        logging.exception("[sparkle_mac] waiting for the cancel")
        return False


def on_quit():
    """Every way WinZapp ends: an install still unconfirmed is cancelled, and
    unless Install was confirmed (then the slot stays held until this process
    is gone, and a dead owner is recovered) the update slot goes back, also
    when the quit lands during the download, before "Ready to Install".
    Nothing is reopened while quitting (_update_abandoned)."""
    try:
        with _lock:
            again, _state["quitting"] = _state["quitting"], True
        if again:
            # A second quit (a signal during the first one's wait): the
            # process must not end before the cancel already sent is
            # confirmed, so wait for that same cancel, as long as it may take.
            _wait_for_cancel()
            return
        cancel_pending_install()
        if not _state["installing"]:
            _release_update_slot()
    except Exception:
        logging.exception("[sparkle_mac] quit: could not tidy up the update")


def _chain_signals():
    """SIGINT/SIGTERM: main.py's handler ends in os._exit(0), past every quit
    path above, so it is wrapped once Sparkle runs (from the main thread,
    where signal handlers are set and run): on_quit() first, then whatever
    handled the signal before."""
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous = signal.getsignal(sig)

            def handler(signum, frame, previous=previous):
                on_quit()
                if callable(previous):
                    return previous(signum, frame)
                if previous == signal.SIG_IGN:
                    return None
                signal.signal(signum, signal.SIG_DFL)
                os.kill(os.getpid(), signum)

            signal.signal(sig, handler)
        except Exception:
            logging.exception("[sparkle_mac] could not wrap signal %s", sig)


def _user_driver_class():
    """WinZapp's subclass of SPUStandardUserDriver (public, not final:
    SPUStandardUserDriver.h:30), made once Sparkle is loaded."""
    if "driver_class" in _state:
        return _state["driver_class"]
    base = objc.lookUpClass("SPUStandardUserDriver")
    objc.registerMetaDataForSelector(
        b"SPUStandardUserDriver", b"showReadyToInstallAndRelaunch:", {"arguments": {2: _CHOICE_BLOCK}})
    objc.registerMetaDataForSelector(
        b"SPUStandardUserDriver", b"showUpdateFoundWithAppcastItem:state:reply:",
        {"arguments": {4: _CHOICE_BLOCK}})

    class WinZappSparkleUserDriver(base):
        def showUpdateFoundWithAppcastItem_state_reply_(self, item, state, reply):
            sup = objc.super(WinZappSparkleUserDriver, self)
            try:
                stage, user = int(state.stage()), bool(state.userInitiated())
            except Exception:
                stage, user = policy.STAGE_INSTALLING, False    # unknown: leave it alone
            on_update_found(item, stage, reply,
                            lambda r: sup.showUpdateFoundWithAppcastItem_state_reply_(item, state, r),
                            user_initiated=user)

        def showReadyToInstallAndRelaunch_(self, reply):
            sup = objc.super(WinZappSparkleUserDriver, self)
            on_ready_to_install(reply, lambda r: sup.showReadyToInstallAndRelaunch_(r))

    _state["driver_class"] = WinZappSparkleUserDriver
    return WinZappSparkleUserDriver


# -- the delegate ----------------------------------------------------------------

class WinZappSparkleDelegate(NSObject):
    """SPUUpdaterDelegate. Sparkle holds its delegate weakly; _state keeps
    it alive. Each method refuses when it fails."""

    @objc.typedSelector(b"Z@:@qo^@")
    def updater_mayPerformUpdateCheck_error_(self, updater, check, error):
        """Scheduled checks only: off with WinZapp's updates setting, held
        back while start-up or pairing owns the screen, and run by one account.
        A check the user asked for always runs."""
        try:
            if check != policy.CHECK_BACKGROUND:
                return True, None
            mw = _state["mw"]
            if not policy.updates_enabled(_settings()):
                return False, _error(policy.SILENT_ABORT, "updates are turned off in Settings")
            if not _window_ready(mw):
                wx.CallLater(NOT_NOW_RETRY_MS, _recheck, False)
                return False, _error(policy.SILENT_ABORT, "not now: start-up or pairing in progress")
            if not _holds_claim(mw):
                return False, _error(policy.SILENT_ABORT, "another account checks for updates")
            return True, None
        except Exception:
            logging.exception("[sparkle_mac] may check: refusing")
            return False, _error(policy.SILENT_ABORT, "update check failed")

    @objc.typedSelector(b"@@:@")
    def allowedChannelsForUpdater_(self, updater):
        try:
            return NSSet.setWithArray_(policy.allowed_channels(_settings()))
        except Exception:
            logging.exception("[sparkle_mac] channels: stable only")
            return NSSet.set()

    @objc.typedSelector(b"Z@:@")
    def updaterShouldPromptForPermissionToCheckForUpdates_(self, updater):
        return False            # Settings > General already asks this

    @objc.typedSelector(b"Z@:@@qo^@")
    def updater_shouldProceedWithUpdate_updateCheck_error_(self, updater, item, check, error):
        try:
            fields = _item(item)
            tag = policy.item_tag(fields["version"], fields["display_version"])
            action, tag, detail = policy.decide(fields, _repo(), ARCH, _running_tag(),
                                                _pop_verdict(tag))
            if action == policy.PROCEED:
                logging.info("[sparkle_mac] offering %s (official tag confirmed, appcast item checks pass)", tag)
                _state["offered"] = tag
                return True, None
            if action == policy.REFUSE:
                logging.error("[sparkle_mac] refusing %s: %s", tag or "an appcast item", detail)
                return False, _error(policy.REFUSED, detail)
            if not _state["checking"]:
                _check_offer(tag, check)
            elif check == policy.CHECK_USER:
                # Already being asked behind a scheduled check: its end comes
                # back as the check the user asked for.
                _state["user_waiting"] = True
            return False, _error(policy.SILENT_ABORT, "asking the official repository first")
        except Exception:
            logging.exception("[sparkle_mac] proceed: refusing")
            return False, _error(policy.REFUSED, "could not check this update")

    @objc.typedSelector(b"v@:@@@")
    def updater_willDownloadUpdate_withRequest_(self, updater, item, request):
        """A guard: the request must be the zip of the release that was
        checked, over https. Anything else (a delta, another URL) is sent to a
        URL that fails, so nothing is downloaded or installed."""
        try:
            fields = _item(item)
            tag = policy.item_tag(fields["version"], fields["display_version"])
            url = str(request.URL().absoluteString()) if request.URL() is not None else ""
            if tag and tag == _state["offered"] and url == policy.zip_url(_repo(), tag, ARCH):
                logging.info("[sparkle_mac] download started: %s", url)
                return
            logging.error("[sparkle_mac] refusing to download an unexpected update")
        except Exception:
            logging.exception("[sparkle_mac] download: refusing")
        try:
            request.setURL_(NSURL.URLWithString_(REFUSED_DOWNLOAD))
        except Exception:
            logging.exception("[sparkle_mac] could not redirect the refused download")

    @objc.typedSelector(b"v@:@@")
    def updater_didDownloadUpdate_(self, updater, item):
        logging.info("[sparkle_mac] download finished")

    @objc.typedSelector(b"v@:@@@")
    def updater_failedToDownloadUpdate_error_(self, updater, item, error):
        try:
            logging.error("[sparkle_mac] download failed: %s",
                          error.localizedDescription() if error is not None else "no reason given")
        except Exception:
            logging.error("[sparkle_mac] download failed")

    @objc.typedSelector(b"v@:@@")
    def updater_didExtractUpdate_(self, updater, item):
        # Despite its name, Sparkle calls this once its installer tool has
        # been launched and handed the download (SPUInstallerDriver.m:476-486
        # -> SPUCoreBasedUpdateDriver.m:235-252), before that tool extracts and
        # checks it; "Ready to Install" is what follows a successful check.
        logging.info("[sparkle_mac] Sparkle's installer started: extracting and checking the update")

    @objc.typedSelector(b"v@:@@")
    def updater_willInstallUpdate_(self, updater, item):
        logging.info("[sparkle_mac] Sparkle is installing the update")

    @objc.typedSelector(b"v@:@q@")
    def updater_didFinishUpdateCycleForUpdateCheck_error_(self, updater, check, error):
        """Every update cycle ends here (SPUUpdaterDelegate.h:475): the cancel
        confirmation cancel_pending_install() waits for, and the moment a
        download that failed or was cancelled, or an install Sparkle gave up
        after Install was confirmed, gives the update slot back and reopens
        the accounts closed for it."""
        try:
            if error is not None and error.code() not in (policy.SILENT_ABORT, policy.NO_UPDATE):
                logging.warning("[sparkle_mac] update cycle ended: %s", error.localizedDescription())
            if _state["outstanding"] is None and _state["slot"] is not None and (
                    not _state["installing"] or error is not None):
                # The download failed or was cancelled, or Sparkle gave up
                # after Install was confirmed: nothing will install.
                _update_abandoned()
        except Exception:
            logging.exception("[sparkle_mac] cycle end")
        finally:
            _cycle_done.set()


# -- the user driver's delegate: a scheduled update is announced -----------------

def announce_scheduled_update(item):
    """A scheduled check found an update that Sparkle's window will show only
    when WinZapp is next activated (SPUStandardUserDriver.m:263-271, 316-332).
    Spoken, as WinZapp's own updater speaks an update while its window is
    hidden, and posted as a notification VoiceOver reads, in words that say
    how to act on it (neither can answer the update window itself); activating
    it opens Sparkle's window
    (checkForUpdates brings a pending update into focus, SPUUpdater.m:
    checkForUpdates -> showUpdateInFocus)."""
    try:
        version = str(item.displayVersionString() or "")
        mw = _state["mw"]
        text = mw.i18n.t("mac_update_available_reminder").format(new_version=version)
        logging.info("[sparkle_mac] scheduled check found %s: announcing it", version)
        mw.output(text, interrupt=True)
        from . import notify_mac
        notify_mac.post_plain(UPDATE_NOTIFICATION, mw.i18n.t("update_available_title"), text,
                              on_open=lambda: _recheck(True))
    except Exception:
        logging.exception("[sparkle_mac] could not announce the update")


def _app_is_active():
    try:
        from AppKit import NSApp
        return bool(NSApp().isActive())
    except Exception:
        return False


def _show_update_in_focus():
    """Sparkle's own way to bring its current window forward and key
    (SPUUserDriver.h:279, showUpdateInFocus; it activates WinZapp if needed,
    SPUStandardUserDriver.m showUpdateInFocus)."""
    try:
        driver = _state["driver"]
        if driver is not None:
            driver.showUpdateInFocus()
    except Exception:
        logging.exception("[sparkle_mac] could not bring Sparkle's window forward")


def announce_ready_to_install():
    """Sparkle shows "Ready to Install" without making it key (it only asks
    for attention, SPUStandardUserDriver.m showReadyToInstallAndRelaunch:),
    so WinZapp's own window keeps VoiceOver and the user does not learn that
    Sparkle is waiting. It is always said. The window is brought forward and
    made key when that takes focus from no one: WinZapp is already the active
    app, or the user started this check (then they are waiting for it).
    After a scheduled check, with another app in front, focus is not taken
    from it: a notification says it, and activating it brings the window."""
    try:
        mw = _state["mw"]
        version = provenance.tag_version(_state["offered"]) or "?"
        text = mw.i18n.t("mac_update_ready_to_install").format(new_version=version)
        mw.output(text, interrupt=True)
        if _app_is_active() or _state["user_initiated"]:
            logging.info("[sparkle_mac] Ready to Install brought to the front")
            _show_update_in_focus()
            return
        logging.info("[sparkle_mac] Ready to Install announced (WinZapp is in the background)")
        from . import notify_mac
        notify_mac.post_plain(UPDATE_NOTIFICATION, mw.i18n.t("update_available_title"), text,
                              on_open=_show_update_in_focus)
    except Exception:
        logging.exception("[sparkle_mac] could not announce Ready to Install")


def _forget_announcement():
    try:
        from . import notify_mac
        notify_mac.remove_plain(UPDATE_NOTIFICATION)
    except Exception:
        logging.debug("[sparkle_mac] could not remove the reminder", exc_info=True)


class WinZappSparkleUserDriverDelegate(NSObject):
    """SPUStandardUserDriverDelegate: gentle scheduled update reminders
    (SPUStandardUserDriverDelegate.h:94-189). Sparkle keeps showing the
    update itself; WinZapp only makes sure a blind user hears about it."""

    @objc.typedSelector(b"Z@:")
    def supportsGentleScheduledUpdateReminders(self):
        return True

    @objc.typedSelector(b"Z@:@Z")
    def standardUserDriverShouldHandleShowingScheduledUpdate_andInImmediateFocus_(self, update, immediate):
        _state["immediate_focus"] = bool(immediate)
        return True

    @objc.typedSelector(b"v@:Z@@")
    def standardUserDriverWillHandleShowingUpdate_forUpdate_state_(self, handle, update, state):
        try:
            if state.userInitiated():
                return              # the user asked: Sparkle shows it at once, in front
            if _state["immediate_focus"] and _app_is_active():
                return              # shown now, in front of the user
            announce_scheduled_update(update)
        except Exception:
            logging.exception("[sparkle_mac] scheduled update reminder")

    @objc.typedSelector(b"v@:@")
    def standardUserDriverDidReceiveUserAttentionForUpdate_(self, update):
        _forget_announcement()

    @objc.typedSelector(b"v@:")
    def standardUserDriverWillFinishUpdateSession(self):
        _forget_announcement()


# -- WinZapp's entry points -------------------------------------------------------

def _sync_automatic_checks():
    """WinZapp's "check for updates automatically" is Sparkle's scheduled
    check: applied at start and whenever the setting changes."""
    updater = _state["updater"]
    if updater is None:
        return
    try:
        auto = policy.updates_enabled(_settings())
        if bool(updater.automaticallyChecksForUpdates()) != auto:
            updater.setAutomaticallyChecksForUpdates_(auto)
    except Exception:
        logging.exception("[sparkle_mac] could not apply the automatic check setting")


def _start(mw):
    """Load Sparkle and start its updater once; False when it cannot run.

    SPUUpdater with WinZapp's subclass of Sparkle's standard user driver is
    what SPUStandardUpdaterController wraps, minus the controller's alert when
    the updater fails to start: here a failure is logged and WinZapp's own
    updater takes over instead."""
    if _state["updater"] is not None:
        return True
    try:
        bundle = NSBundle.bundleWithPath_(framework_path())
        if bundle is None or not bundle.load():
            raise RuntimeError("Sparkle.framework did not load")
        objc.registerMetaDataForSelector(
            b"SPUUpdater", b"startUpdater:", {"arguments": {2: {"type_modifier": objc._C_OUT}}})
        host = NSBundle.mainBundle()
        _state["mw"] = mw
        delegate = WinZappSparkleDelegate.alloc().init()
        driver_delegate = WinZappSparkleUserDriverDelegate.alloc().init()
        driver = _user_driver_class().alloc().initWithHostBundle_delegate_(host, driver_delegate)
        updater = objc.lookUpClass("SPUUpdater").alloc() \
            .initWithHostBundle_applicationBundle_userDriver_delegate_(host, host, driver, delegate)
        started, error = updater.startUpdater_(None)
        if not started:
            raise RuntimeError(error.localizedDescription() if error is not None else "no reason given")
    except Exception:
        logging.exception("[sparkle_mac] Sparkle could not start; WinZapp's own updater is used")
        return False
    _state["updater"], _state["driver"], _state["delegate"] = updater, driver, delegate
    _state["driver_delegate"] = driver_delegate
    _sync_automatic_checks()
    _chain_signals()
    logging.info("[sparkle_mac] Sparkle started (feed %s, alpha %s)",
                 updater.feedURL(), bool(policy.allowed_channels(_settings())))
    return True


def install():
    """Route WinZapp's update entry points to Sparkle (menubar_mac and
    updater_mac have run: the menu item is shown, UpdateChecker points at the
    Mac releases, which stays the fallback), and the quit paths through
    on_quit()."""
    if not enabled():
        return
    from main_window import session_lifecycle, settings, updates, window_lifecycle
    orig_start = updates.UpdatesMixin._start_update_checker
    orig_force = updates.UpdatesMixin._on_force_update

    def start_update_checker(self, force=False):
        if not _start(self):
            return orig_start(self, force)
        if force:
            _state["updater"].checkForUpdates()

    def on_force_update(self, event):
        if not _start(self):
            return orig_force(self, event)
        _state["updater"].checkForUpdates()

    updates.UpdatesMixin._start_update_checker = start_update_checker
    updates.UpdatesMixin._on_force_update = on_force_update

    # Every way WinZapp ends: real_exit() and another account's IPC quit both
    # tear down through _perform_shutdown(); a Mac quit event (Dock, logout,
    # Sparkle's own) through _on_query_end_session().
    orig_shutdown = window_lifecycle.WindowLifecycleMixin._perform_shutdown
    orig_query = session_lifecycle.SessionLifecycleMixin._on_query_end_session

    def perform_shutdown(self, *args, **kwargs):
        on_quit()
        return orig_shutdown(self, *args, **kwargs)

    def on_query_end_session(self, event):
        on_quit()
        return orig_query(self, event)

    window_lifecycle.WindowLifecycleMixin._perform_shutdown = perform_shutdown
    session_lifecycle.SessionLifecycleMixin._on_query_end_session = on_query_end_session

    # The setting changed here (Settings dialog) or in another account.
    orig_choose = settings.SettingsMixin.choose_global_settings
    orig_pulled = settings.SettingsMixin._apply_pulled_global_settings

    def choose_global_settings(self, choices):
        try:
            return orig_choose(self, choices)
        finally:
            wx.CallAfter(_sync_automatic_checks)

    def apply_pulled_global_settings(self, pulled):
        try:
            return orig_pulled(self, pulled)
        finally:
            wx.CallAfter(_sync_automatic_checks)

    settings.SettingsMixin.choose_global_settings = choose_global_settings
    settings.SettingsMixin._apply_pulled_global_settings = apply_pulled_global_settings
    logging.info("[sparkle_mac] updates go through Sparkle")
