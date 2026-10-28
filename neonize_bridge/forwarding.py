"""Forwarding a stored message that has no media: text, location and contact
cards, rebuilt from the record the client reads (msgmap.py writes it).

A location keeps its point, its name and its address; a live location goes
as the point where it was last seen, as WhatsApp's own forward does. A
contact card goes as one `contactMessage`, a set of cards as a
`contactsArrayMessage` with the set's name. Anything else is None: the route
answers 501, as before.
"""
import re

_CARD = re.compile(r"BEGIN:VCARD.*?END:VCARD", re.S | re.I)
_NAME = re.compile(r"^FN[;:](?:[^:\r\n]*:)?(.*)$", re.M | re.I)


def _forwarded():
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import ContextInfo
    return ContextInfo(isForwarded=True, forwardingScore=1)


def card_name(card) -> str:
    match = _NAME.search(card)
    return match.group(1).strip() if match else ""


def plain_forward(original):
    """The Message that forwards the stored record `original`, or None."""
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import (
        ContactMessage, ContactsArrayMessage, ExtendedTextMessage, LocationMessage, Message)
    kind = original.get("type")
    if kind == "chat" and original.get("body"):
        return Message(extendedTextMessage=ExtendedTextMessage(
            text=original["body"], contextInfo=_forwarded()))
    if kind in ("location", "liveLocation"):
        lat, lng = original.get("lat"), original.get("lng")
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (lat, lng)):
            return None
        name = (original.get("body") or "") if kind == "location" else ""
        place = original.get("loc") or ""
        location = LocationMessage(degreesLatitude=lat, degreesLongitude=lng,
                                   contextInfo=_forwarded())
        if name:
            location.name = name
        if place and place != name and kind == "location":
            location.address = place
        return Message(locationMessage=location)
    if kind == "vcard":
        cards = _CARD.findall(original.get("body") or "")
        if not cards:
            return None
        if len(cards) == 1:
            return Message(contactMessage=ContactMessage(
                displayName=original.get("displayName") or card_name(cards[0]),
                vcard=cards[0], contextInfo=_forwarded()))
        return Message(contactsArrayMessage=ContactsArrayMessage(
            displayName=original.get("displayName") or "",
            contacts=[ContactMessage(displayName=card_name(c), vcard=c) for c in cards],
            contextInfo=_forwarded()))
    return None
