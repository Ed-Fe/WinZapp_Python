"""The other accounts and the update slot of a Sparkle update, without Cocoa.

Every account is its own process. Before Sparkle prepares its installer the
other accounts quit and WinZapp claims update_coord's update slot (sparkle_mac
explains why that order); when the update does not install they are opened
again. This is the decision logic of that, as plain functions: each takes the
client collaborator it needs (update_coord's lease listing and slot, ipc's quit
request, the process spawner) as an argument, so it is tested on every
platform with fakes (tests/test_macos_update_accounts.py) and sparkle_mac
passes the real ones. The module imports nothing from the client, objc or wx.
"""

import logging
import time

LOG = "[sparkle_mac]"


def other_accounts(account_id, leases):
    """The account ids other than *account_id* with a live runtime lease.
    *leases* is node_coord.live_node_leases(), already filtered by the liveness
    rule; a corrupt record, one without an id and this account are left out."""
    if not account_id:
        return []
    others = [lease.get("account_id") for lease in leases if not lease.get("_corrupt")]
    return sorted({a for a in others if a and a != account_id})


def quit_accounts(others, request_quit):
    """The peer walk of MainWindow.quit_all_accounts(), without exiting: each
    account closes its WhatsApp session and confirms before the next. One that
    fails does not stop the others."""
    for other in others:
        try:
            logging.info("%s asking account %s to quit before the update", LOG, other)
            request_quit(other)
        except Exception:
            logging.exception("%s request_quit failed for %s", LOG, other)
    if others:
        logging.info("%s other accounts asked to quit: %d", LOG, len(others))


def claim_slot(try_begin, attempts, wait, sleep=time.sleep):
    """The update slot token, or None while another account still runs.
    *try_begin*() is update_coord.try_begin_update with its liveness rule: a
    token, or None when an account is alive. The quitting accounts drop their
    leases as they exit, so *attempts* tries are made, *wait* seconds apart.
    An exception from *try_begin* is a refusal at once, not a retry."""
    for _ in range(attempts):
        try:
            token = try_begin()
        except Exception:
            logging.exception("%s try_begin_update failed — not updating", LOG)
            return None
        if token is not None:
            return token
        sleep(wait)
    return None


def release_slot(token, end_update):
    """Give the slot back (*end_update*(token)); nothing to give back for no
    token. Never raises."""
    if not token:
        return
    try:
        end_update(token)
    except Exception:
        logging.exception("%s end_update failed", LOG)


def accounts_to_reopen(closed, running):
    """Of the accounts closed for the update, those that are not running now
    (one that never quit still is), in the order they were closed."""
    running = set(running)
    return [a for a in closed if a not in running]


def reopen_accounts(accounts, spawn, announce):
    """Start again each account in *accounts* (*spawn*(account) launches one in
    the background) and *announce*() once. Nothing for an empty list. Never
    raises: a failure is logged and the rest are not attempted."""
    if not accounts:
        return
    try:
        for account in accounts:
            logging.info("%s reopening account %s", LOG, account)
            spawn(account)
        announce()
    except Exception:
        logging.exception("%s reopening the other accounts failed", LOG)


def pop_verdict(verdicts, tag, now, ttl):
    """The offer-time answer stored for *tag*, removed from *verdicts*, or None
    when there is none or it is older than *ttl* seconds (asked again)."""
    entry = verdicts.pop(tag, None)
    if entry is None or now - entry[0] > ttl:
        return None
    return entry[1]
