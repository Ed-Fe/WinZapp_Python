"""Who sees my Status (`POST privacy/status {setting, list}`).

WhatsApp Web keeps this setting on the device and tells the other devices
through the app state; it sends no query to the server (read over CDP on
2026-10-26: WAWebStatusSetAndSyncPrivacy -> WAWebStatusPrivacySettingSync).
The mutation: collection `regular_high`, index ["status_privacy"], version 7,
value `statusPrivacy {mode, userJID}`, where mode is CONTACTS, DENY_LIST or
ALLOW_LIST and userJID the people of the list in their legacy form
('<digits>@c.us', '<digits>@lid'); with CONTACTS the list is empty.

The body and the answers follow WinZapp's WPPConnect route
(privacyController.ts): `{"response": {"setting"}}`, 400
"invalid_status_privacy", 502 "status_privacy_failed", 503
"privacy_unavailable". A neonize build without app state mutations answers 501
"status_privacy_unsupported", as before. Not run against a real account.
"""
import asyncio
import logging
import re

from aiohttp import web

import app_state_mirror

log = logging.getLogger("bridge")

COLLECTION = "regular_high"
INDEX = "status_privacy"
VERSION = 7
MAX_LIST = 2000
PERSON = re.compile(r"\d{5,20}@(c\.us|lid)\Z")
# StatusPrivacyAction.StatusDistributionMode, repeated so the pure functions
# do not import neonize
MODES = {"allow-list": 0, "deny-list": 1, "contact": 2}
SETTINGS = {mode: setting for setting, mode in MODES.items()}


def parse_request(body):
    """(setting, people) from the body, or None when it is not valid."""
    body = body if isinstance(body, dict) else {}
    setting, people = body.get("setting"), body.get("list", [])
    if setting not in MODES or not isinstance(people, list) or len(people) > MAX_LIST:
        return None
    if not all(isinstance(p, str) and PERSON.fullmatch(p) for p in people):
        return None
    return setting, ([] if setting == "contact" else list(dict.fromkeys(people)))


def privacy_fields(setting, people) -> dict:
    """The fields of the StatusPrivacyAction."""
    return {"mode": MODES[setting], "userJID": list(people)}


def setting_of(mirror) -> str:
    """The setting the mirror holds ('' when it holds none)."""
    from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import SyncActionValue
    for _index, _version, raw in mirror.entries(INDEX):
        value = SyncActionValue.FromString(raw)
        if value.HasField("statusPrivacy"):
            return SETTINGS.get(value.statusPrivacy.mode, "")
    return ""


def register_routes(r, *, session):
    """Wire the route to the aiohttp router `r`."""

    def failure(code, status):
        return web.json_response({"status": "error", "code": code}, status=status)

    def apply(s, setting, people):
        from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import (
            StatusPrivacyAction, SyncActionValue)
        value = SyncActionValue(statusPrivacy=StatusPrivacyAction(
            **privacy_fields(setting, people)))
        app_state_mirror.send(s, COLLECTION, [([INDEX], VERSION, value, False)])
        return setting_of(app_state_mirror.mirror_of(s)) or setting

    async def set_status_privacy(req):
        s = session(req)
        try:
            body = await req.json()
        except ValueError:
            body = {}
        parsed = parse_request(body)
        if parsed is None:
            return failure("invalid_status_privacy", 400)
        if not app_state_mirror.supported():
            return failure("status_privacy_unsupported", 501)
        if s.client is None or s.status != "CONNECTED":
            return failure("privacy_unavailable", 503)
        try:
            now = await asyncio.get_running_loop().run_in_executor(None, apply, s, *parsed)
        except Exception:
            log.exception("privacy/status")
            return failure("status_privacy_failed", 502)
        return web.json_response({"status": "success", "response": {"setting": now}})

    r.add_post("/api/{tok}/privacy/status", set_status_privacy)
