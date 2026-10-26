"""Routes left over after the areas: what neonize 0.5.2 cannot do (501,
with the reason), the Meta AI terms and the platform of a message.

What was checked in the neonize 0.5.2 package (client.py, _binder.py, the proto and
the goneonize source) and does not exist in it, so it answers 501 instead of pretending:

- joining a community group, canceling the request and deactivating the community:
  no function (whatsmeow only has LinkGroup/UnlinkGroup between parent and child);
- changing who sees my Status (`privacy/status`): only `get_status_privacy` exists.
  `SyncActionValue.statusPrivacy` exists in the proto, but the index and
  collection of the state notice are unknown, and a wrong guess would say "private"
  to someone who is not;
- removing a contact: `send_app_state` takes `PatchInfo` with `MutationInfo`
  (Index, Version, Value), with no operation field, and whatsmeow's `EncodePatch`
  always writes `SyncdMutation_SET`; removing is the REMOVE operation;
- editing or deleting a channel: whatsmeow has the query `mutationUpdateNewsletter`
  but does not expose it, and `sendMexIQ` is private; there is no raw IQ in neonize;
- battery level: multi-device does not deliver the phone's battery.

`unhealthy` is left out on purpose: in the reference server it answers 503 and
ends the process (it only serves to test an orchestrator), and the client does not use it.
`profile/:phone` was also left out: it is an obsolete route (`getNumberProfile`,
@deprecated) that the client does not call and whose format is not defined anywhere
it can be checked.

The pure functions are at the top (tested in test_remaining.py); `register_routes` wires
the routes. Nothing here was tested against a real account.
"""
import logging

from aiohttp import web

log = logging.getLogger("bridge")

# (method, route, code, reason). The `code` is what the client reads to decide the
# message: "unavailable" is what the reference server uses when there is no
# better answer (communityJoinController.ts).
IMPOSSIBLE = (
    ("POST", "community-subgroups/{communityId}/{subgroupId}/join", "unavailable",
     "neonize has no way to join a community group"),
    ("POST", "community-subgroups/{communityId}/{subgroupId}/cancel-request", "unavailable",
     "neonize has no way to cancel a request to join a community group"),
    ("POST", "deactivate-community", "unavailable",
     "neonize has no way to deactivate a community"),
    ("POST", "privacy/status", "status_privacy_unsupported",
     "neonize can read, but not change, who sees my Status"),
    ("POST", "remove-contact", "contact_operation_unsupported",
     "neonize app state patches can only set an entry, not remove it"),
    ("PUT", "newsletter/{id}", "newsletter_unsupported",
     "neonize cannot edit a channel"),
    ("DELETE", "newsletter/{id}", "newsletter_unsupported",
     "neonize cannot delete a channel"),
    ("GET", "get-battery-level", "battery_unsupported",
     "a linked device does not receive the phone battery level"),
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


def register_routes(r, *, session, ok):
    """Wire the remaining routes to the aiohttp router `r`."""

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
