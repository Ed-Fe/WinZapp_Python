"""Calls in the bridge: what whatsmeow can do with a call.

It receives the offer and the end of a call (CallOffer, CallTerminate) and knows
how to reject a call that is ringing (RejectCall). It does NOT answer, does not
place calls and does not carry audio or video (there is no WebRTC/VoIP media in
it), so the other `call/*` routes answer 501 instead of pretending to have worked.

The formats of the events are the ones the client reads in core/websocket_client.py
(`incomingcall` and `callstate`); those of the refusal, the ones in
main_window/calls.py (`/call/reject` with {"callId"} or {} and a response with
`via`/`call.state`).

The pure functions are at the top (tested in test_calls.py); `register_routes`
wires the routes and `register_events` wires neonize's events to a session.
"""
import asyncio
import logging
import time

from aiohttp import web

import system_messages
import msgmap
from redaction import redact

log = logging.getLogger("bridge")

# A ring lasts about 2 minutes at most; what is far past that is no longer
# ringing and would only take memory.
RING_TTL = 600

# Routes that whatsmeow has no way to fulfil (there is no way to answer, dial,
# hold or inspect a call). `call/group/offer` is not in COBERTURA.md but the
# WPPConnect server defines it, and the reason is the same.
IMPOSSIBLE_ROUTES = (
    "call/accept", "call/audio/enable", "call/diagnostics", "call/end",
    "call/ensure-ended", "call/offer", "call/group/offer", "call/status",
    "call/upgrade-video",
)
IMPOSSIBLE_REASON = "calls cannot be placed, answered or carried through neonize (whatsmeow)"


def attribute(node, name) -> str:
    """The first attribute `name` of the node or of any descendant ('' if none).
    `node` is a neonize Node (Tag, Attrs[name/text], Nodes)."""
    for a in node.Attrs:
        if a.name == name and a.text:
            return a.text
    for child in node.Nodes:
        found = attribute(child, name)
        if found:
            return found
    return ""


def has_tag(node, tag) -> bool:
    """Whether the node or any descendant has the tag (a video offer brings <video/>)."""
    if node.Tag == tag:
        return True
    return any(has_tag(child, tag) for child in node.Nodes)


def _jid_text(jid) -> str:
    if jid is None or not getattr(jid, "User", ""):
        return ""
    return msgmap.jid_wpp(f"{jid.User}@{jid.Server}")


def caller(meta) -> str:
    """The id of whoever called, in WPPConnect's format. Prefers the number (@c.us)
    over the @lid when whatsmeow delivers both; the client knows how to resolve both."""
    creator = _jid_text(meta.callCreator)
    alternative = _jid_text(meta.callCreatorAlt)
    if creator.endswith("@lid") and alternative and not alternative.endswith("@lid"):
        return alternative
    return creator or alternative


def offer_event(e, now) -> dict:
    """neonize CallOffer -> the `data` of `incomingcall` (and of `callstate`)."""
    meta = e.basicCallMeta
    group = msgmap.jid_wpp(attribute(e.data, "group-jid"))
    return {
        "event": "offer", "state": "INCOMING_RING",
        "peerJid": caller(meta), "id": meta.callID,
        "isVideo": has_tag(e.data, "video"),
        "isGroup": bool(group), "groupJid": group,
        "timestamp": msgmap.seconds(meta.timestamp), "observedAt": int(now),
        # an offer that stayed queued while the app was away: the client ignores it
        "offerReceivedWhileOffline": bool(attribute(e.data, "offline")),
    }


def end_state(reason) -> tuple:
    """CallTerminate reason -> (event, state) that the client understands. Any
    state outside ringing already makes the client silence the alert; the
    refinement only changes what it records."""
    m = str(reason or "").lower()
    if "timeout" in m:
        return "timeout", "NOT_ANSWERED"
    if "reject" in m or "decline" in m:
        return "ended", "REJECTED"
    if "accept" in m:
        return "ended", "HANDLED_REMOTELY"
    return "ended", "ENDED"


def end_event(e, now) -> dict:
    """neonize CallTerminate -> the `data` of `incomingcall` and `callstate`."""
    meta = e.basicCallMeta
    event, state = end_state(e.reason)
    group = msgmap.jid_wpp(attribute(e.data, "group-jid"))
    return {
        "event": event, "state": state,
        "peerJid": caller(meta), "id": meta.callID,
        "isVideo": has_tag(e.data, "video"),
        "isGroup": bool(group), "groupJid": group,
        "outgoing": False,
        "timestamp": msgmap.seconds(meta.timestamp), "observedAt": int(now),
    }


def refine_end(data, accepted) -> dict:
    """An end with no reason ("ENDED") of a call that SOMEONE ANSWERED before is a
    call answered on another device (the phone): it is not the call the author
    canceled, and the client counts that one as missed. `accepted` is what
    `Pending` kept from the CallAccept: (server instant, local instant) or None.
    Returns `data` with the state and the duration (seconds of the call) fixed;
    any other end, including the one that brings a reason, stays as it came."""
    if data["state"] != "ENDED" or not accepted:
        return data
    return {**data, "state": "HANDLED_REMOTELY",
            "callDuration": call_duration(accepted, data["timestamp"], data["observedAt"])}


def call_duration(accepted, server_ended, local_ended) -> int:
    """Seconds between the CallAccept and the CallTerminate: by the server's
    instants when both exist (the clock here may be wrong), otherwise by the
    local ones. Never negative."""
    server, local = accepted
    if server and server_ended:
        return max(0, int(server_ended) - int(server))
    return max(0, int(local_ended) - int(local))


def call_chat(db, meta, group) -> str:
    """The key (whatsmeow format) of the chat where the call record goes: the
    group, or the peer. The peer may be stored by number or by LID; the form
    that already has a chat is used, and in its absence the number."""
    if group:
        return msgmap.jid_whatsmeow(group)
    forms = [msgmap.jid_whatsmeow(j) for j in (_jid_text(meta.callCreator),
                                               _jid_text(meta.callCreatorAlt)) if j]
    forms.sort(key=lambda j: j.endswith("@lid"))          # the number first
    for form in forms:
        if db.chat_exists(form):
            return form
    return forms[0] if forms else ""


def record_end(s, e, data, was_ringing, now):
    """The end of a received call becomes the `call_log` record in the chat (what
    WhatsApp Web shows as "Missed voice call"). `data` is the `end_event`;
    `was_ringing` says whether the offer was still ringing here. Does not
    record the call I made myself (from another device): it cannot be known
    who it was with just from the event. False if nothing was recorded."""
    outcome = system_messages.call_outcome(data["state"], was_ringing)
    creator = data["peerJid"]
    mine = {msgmap.jid_wpp(getattr(s, "me", "")), msgmap.jid_wpp(getattr(s, "lid", ""))} - {""}
    if outcome is None or not data["id"] or not creator or creator in mine:
        return False
    chat = call_chat(s.db, e.basicCallMeta, data["groupJid"])
    if not chat:
        return False
    w = system_messages.call_record(
        call_id=data["id"], chat_w=msgmap.jid_wpp(chat), creator_w=creator,
        outcome=outcome, video=data["isVideo"],
        ts=data["timestamp"] or int(now), duration=data.get("callDuration", 0))
    return system_messages.store_and_emit(s, chat, w, now)


class Pending:
    """Calls that are ringing: id -> the JID of whoever called, EXACTLY as it came
    in the offer (RejectCall requires that)."""

    def __init__(self):
        self._rings = {}
        self._accepted = {}

    def accept(self, call_id, server_ts, now):
        """The CallAccept: someone (not this client, which does not answer) answered."""
        self._accepted = {i: t for i, t in self._accepted.items()
                          if now - t[1] < RING_TTL}
        self._accepted[call_id] = (server_ts, now)

    def accepted(self, call_id):
        """(server instant, local instant) of the CallAccept, or None."""
        return self._accepted.get(call_id)

    def remember(self, call_id, creator, now):
        self._rings = {i: t for i, t in self._rings.items()
                       if now - t[1] < RING_TTL}
        self._rings[call_id] = (creator, now)

    def forget(self, call_id):
        self._rings.pop(call_id, None)
        self._accepted.pop(call_id, None)

    def creator_of(self, call_id):
        found = self._rings.get(call_id)
        return found[0] if found else None

    def to_refuse(self, body) -> list:
        """The ids to refuse for the `call/reject` body: the requested `callId`, or
        (empty body, the app only knows a call is ringing) all that ring. Empty
        list if the request matches none."""
        requested = str((body or {}).get("callId") or "")
        if requested:
            return [requested] if requested in self._rings else []
        return list(self._rings)


def refusal_response(ids) -> dict:
    """The `response` of `call/reject`: the client only reads `via` and `call.state`."""
    return {"via": "neonize", "call": {"state": "REJECTED"}, "rejected": len(ids)}


def register_events(cli, s):
    """Wires the call offer and end of the neonize client `cli` to the session `s`
    (needs `s.name` and `s.emit`). Called on neonize's thread."""
    import neonize.events as nev

    s.ringing_calls = Pending()

    def notify(data):
        # incomingcall opens/closes the alert; callstate follows the call in progress
        # (WPPConnect emits both for each change, see createSessionUtil.ts)
        s.emit("incomingcall", {"session": s.name, "data": data})
        s.emit("callstate", {"session": s.name, "data": data})

    @cli.event(nev.CallOfferEv)
    def _offer(c, e):
        try:
            data = offer_event(e, time.time())
            if not data["id"]:
                return
            s.ringing_calls.remember(data["id"], e.basicCallMeta.callCreator, time.time())
            log.info("call received (video=%s group=%s)", data["isVideo"], data["isGroup"])
            notify(data)
        except Exception:
            log.exception("call offer")

    @cli.event(nev.CallAcceptEv)
    def _answered(c, e):
        try:
            if e.basicCallMeta.callID:
                s.ringing_calls.accept(
                    e.basicCallMeta.callID, msgmap.seconds(e.basicCallMeta.timestamp), time.time())
        except Exception:
            log.exception("call answered")

    @cli.event(nev.CallTerminateEv)
    def _end(c, e):
        try:
            data = end_event(e, time.time())
            data = refine_end(data, s.ringing_calls.accepted(data["id"]))
            was_ringing = s.ringing_calls.creator_of(data["id"]) is not None
            s.ringing_calls.forget(data["id"])
            log.info("call ended (%s)", data["state"])
            notify(data)
        except Exception:
            log.exception("call end")
            return
        try:
            record_end(s, e, data, was_ringing, time.time())
        except Exception:
            log.exception("call record %s", redact(data["id"]))


def register_routes(r, *, session, ok):
    """Wire the call routes to the aiohttp router `r`."""

    def error(message, status=500):
        return web.json_response({"status": "error", "message": str(message)}, status=status)

    async def reject(req):
        s = session(req)
        try:
            body = await req.json()
        except Exception:
            body = {}
        if s.client is None or s.status != "CONNECTED":
            return error("Disconnected", 400)
        ringing = getattr(s, "ringing_calls", None)
        ids = ringing.to_refuse(body) if ringing is not None else []
        if not ids:
            return error("call not found", 404)
        loop = asyncio.get_running_loop()
        try:
            for call_id in ids:
                await loop.run_in_executor(
                    None, s.client.reject_call, ringing.creator_of(call_id), call_id)
                ringing.forget(call_id)
        except Exception as e:
            log.exception("reject call")
            return error(e)
        return ok(status="success", response=refusal_response(ids))

    async def impossible(req):
        return error(IMPOSSIBLE_REASON, 501)

    r.add_post("/api/{tok}/call/reject", reject)
    r.add_post("/api/{tok}/reject-call", reject)
    for route in IMPOSSIBLE_ROUTES:
        r.add_post("/api/{tok}/" + route, impossible)
        r.add_get("/api/{tok}/" + route, impossible)
