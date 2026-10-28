"""Validate a live roster and format names without exposing WhatsApp IDs."""
import re
from core.call_matching import is_placeholder_call_id

_PERSON = re.compile(r"\d+@(?:lid|s\.whatsapp\.net|c\.us)")


def active_participants_available(active, state):
    call_id = active.get("call_id") if isinstance(active, dict) else None
    return (isinstance(call_id, str) and bool(call_id) and not is_placeholder_call_id(call_id)
            and state in ("ACTIVE", "REJOINING"))


def parse_participants(body, call_id):
    data = body.get("response") if isinstance(body, dict) else None
    if (not isinstance(data, dict) or data.get("id") != call_id
            or type(data.get("available")) is not bool
            or not isinstance(data.get("participants"), list)
            or len(data["participants"]) > 1024):
        raise ValueError("Invalid participants response")
    if not data["available"]:
        if data["participants"]:
            raise ValueError("Ended call has participants")
        return None
    rows, seen = [], set()
    for item in data["participants"]:
        if (not isinstance(item, dict) or not isinstance(item.get("jid"), str)
                or not _PERSON.fullmatch(item["jid"]) or type(item.get("isSelf")) is not bool
                or not isinstance(item.get("name"), str)):
            raise ValueError("Invalid participant")
        jid = item["jid"].replace("@c.us", "@s.whatsapp.net")
        key = "self" if item["isSelf"] else jid
        if key in seen:
            continue
        seen.add(key)
        rows.append({**item, "jid": jid, "key": key})
    return rows


def participant_rows(participants, i18n, name_of):
    rows = []
    for person in participants:
        try:
            name = (i18n.t("call_participants_you") if person["isSelf"] else
                    name_of(person["jid"], person["name"]))
        except Exception:
            name = person["name"]
        if not isinstance(name, str) or not name.strip() or _PERSON.fullmatch(name.strip()):
            name = i18n.t("unknown_contact")
        rows.append((person["key"], " ".join(name.split())[:511]))
    return rows
