"""Routes left over after the areas: what neonize cannot do (501, with the
reason), the Meta AI terms, the platform of a message, and the wiring of the
routes WinZapp's neonize build made possible (raw queries and app state
mutations, neonize_patches/): custom lists (custom_lists.py), joining and
leaving a community's groups and deactivating it (community_membership.py),
editing and deleting a channel (channel_admin.py), who sees my Status
(status_privacy.py) and removing a contact (contact_removal.py). Each of those
answers 501 itself on an older build.

Still impossible:

- battery level: multi-device does not deliver the phone's battery;
- a screenshot: there is no browser page to capture;
- accepting the Meta AI terms: the stage the library asks for is not confirmed
  for this notice, and a wrong consent cannot be undone.

`unhealthy` is left out on purpose: in the reference server it answers 503 and
ends the process (it only serves to test an orchestrator), and the client does not use it.
`profile/:phone` was also left out: it is an obsolete route (`getNumberProfile`,
@deprecated) that the client does not call and whose format is not defined anywhere
it can be checked.

The pure functions are at the top (tested in test_remaining.py); `register_routes` wires
the routes.
"""
import logging

from aiohttp import web

import channel_admin
import community_membership
import contact_removal
import custom_lists
import maintenance
import status_privacy

log = logging.getLogger("bridge")

# (method, route, code, reason). The `code` is what the client reads to decide the
# message: "unavailable" is what the reference server uses when there is no
# better answer (communityJoinController.ts).
IMPOSSIBLE = (
    ("GET", "get-battery-level", "battery_unsupported",
     "a linked device does not receive the phone battery level"),
    ("GET", "take-screenshot", "screenshot_unsupported",
     "neonize has no browser page to capture"),
    ("POST", "meta-ai-terms/accept", "meta_ai_terms_unsupported",
     "accepting the Meta AI terms is not verified for this neonize build"),
)

# The Meta AI terms notice that WhatsApp Web uses (deviceController.ts,
# META_AI_NOTICE_ID_FALLBACK). Accepting it (AcceptTOSNotice) is left out: the stage
# the library asks for is not confirmed for this notice, and recording a wrong
# consent on the user's account cannot be undone.
META_AI_NOTICE = "20250502"


def message_platform(serial) -> str:
    """'android', 'iphone', 'web' or 'unknown' from the message id, by the same
    rules as wa-js (chat/functions/getPlatformFromMessage.ts, based on
    Baileys): an id longer than 21 characters is android, '3A...' is iphone,
    '3EB0...' is web. `serial` is 'fromMe_chat_id[_participant]' (or just the id)."""
    text = str(serial or "")
    parts = text.split("_")
    short_id = parts[2] if len(parts) >= 3 else text
    if len(short_id) > 21:
        return "android"
    if short_id.startswith("3A"):
        return "iphone"
    if short_id.startswith("3EB0"):
        return "web"
    return "unknown"


def meta_ai_terms_response(state) -> dict:
    """The `response` of meta-ai-terms: `state` in capitals, as the client reads it."""
    return {"noticeId": META_AI_NOTICE, "state": state}


def register_routes(r, *, session, ok, jid_from_text, jid_text, destination_of=None,
                    store_sent=None, send_media=None):
    """Wire the remaining routes to the aiohttp router `r`."""
    maintenance.register_routes(r, session=session, ok=ok, destination_of=destination_of,
                                store_sent=store_sent, send_media=send_media)
    community_membership.register_routes(r, session=session)
    channel_admin.register_routes(r, session=session)
    status_privacy.register_routes(r, session=session)
    contact_removal.register_routes(r, session=session, jid_from_text=jid_from_text)
    custom_lists.register_routes(r, session=session, jid_from_text=jid_from_text,
                                 jid_text=jid_text)

    def error(code, status, message=""):
        return web.json_response(
            {"status": "error", "code": code, "message": message}, status=status)

    def impossible(code, reason):
        async def handle(req):
            return error(code, 501, reason)
        return handle

    async def meta_ai_terms(req):
        # whatsmeow does not read the state of a terms notice; "UNKNOWN" is what the
        # client understands as "I do not know", without blocking the send.
        return ok(status="success", response=meta_ai_terms_response("UNKNOWN"))

    async def platform_from_message(req):
        # Like the reference server: the body is the word itself, in JSON.
        return web.json_response(message_platform(req.match_info["messageId"]))

    r.add_get("/api/{tok}/meta-ai-terms", meta_ai_terms)
    r.add_get("/api/{tok}/get-platform-from-message/{messageId}", platform_from_message)
    for method, route, code, reason in IMPOSSIBLE:
        r.add_route(method, "/api/{tok}/" + route, impossible(code, reason))
