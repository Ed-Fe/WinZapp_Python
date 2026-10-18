"""Announcement comments have their own identity and send verdict, outside the outbox."""

import math

from core.utils import is_phone_like, parse_bool_flag


class CommunityCommentsError(Exception):
    """A static code, never a native error containing credentials or message text."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def can_open_comments(chat, message):
    if not isinstance(chat, dict) or not isinstance(message, dict):
        return False
    if not str(chat.get("remoteJid", "")).endswith("@g.us"):
        return False
    metadata = chat.get("groupMetadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    # Older cached chats can lack this flag. The browser verifies the group
    # before enabling the composer; an ordinary restricted group never qualifies.
    if parse_bool_flag(metadata.get("defaultSubgroup")) is False:
        return False
    key = message.get("key")
    return isinstance(key, dict) and bool(key.get("id"))


def parse_comments(payload, parent_id):
    if not isinstance(payload, dict) or payload.get("status") != "success":
        raise CommunityCommentsError("unavailable")
    rows = payload.get("response")
    if not isinstance(rows, list):
        raise CommunityCommentsError("unavailable")
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise CommunityCommentsError("unavailable")
        timestamp = row.get("timestamp")
        if (not isinstance(row.get("id"), str) or not row["id"]
                or row.get("parentMsgId") != parent_id
                or row.get("type") not in ("comment", "revoked", "ciphertext")
                or isinstance(timestamp, bool) or not isinstance(timestamp, (int, float))
                or not math.isfinite(timestamp) or timestamp < 0
                or not isinstance(row.get("chatId"), str)
                or row["chatId"] != parent_id.split("_", 2)[1]
                or (row.get("author") is not None and not isinstance(row["author"], str))
                or (row.get("authorName") is not None and not isinstance(row["authorName"], str))
                or (row.get("fromMe") is not None and not isinstance(row["fromMe"], bool))
                or (row.get("type") == "comment" and not isinstance(row.get("body", ""), str))):
            raise CommunityCommentsError("unavailable")
        if row["id"] in seen:
            raise CommunityCommentsError("unavailable")
        seen.add(row["id"])
        result.append({
            "id": row["id"], "parentMsgId": parent_id, "chatId": row["chatId"],
            "author": row.get("author") or "", "timestamp": timestamp,
            "authorName": row.get("authorName") or "", "fromMe": row.get("fromMe") is True,
            "type": row["type"], "body": row.get("body", "") if row["type"] == "comment" else "",
        })
    return sorted(result, key=lambda row: (row["timestamp"], row["id"]))


def comment_send_confirmed(status, payload):
    return (status == 201 and isinstance(payload, dict)
            and payload.get("status") == "success"
            and isinstance(payload.get("response"), dict)
            and payload["response"].get("messageSendResult") == "OK")


def comment_author_label(row, main_window, jid):
    author = main_window._normalize_jid(row.get("author") or "")
    if row.get("fromMe") is True or getattr(main_window, "_is_self_jid", lambda _jid: False)(author):
        return main_window.self_reference_label()
    unnamed = main_window.i18n.t("unnamed_participant")
    local = (main_window._resolve_jid_name(author, jid, resolve_missing=False) if author else "")
    for name in (local, row.get("authorName")):
        if (isinstance(name, str) and name.strip() and name != unnamed
                and not name.endswith(("@lid", "@c.us", "@s.whatsapp.net", "@g.us"))
                and not is_phone_like(name)):
            return name.strip()
    return unnamed


def comment_count(value):
    """An explicit count only; missing metadata must not become a false zero."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def parse_reply_count_event(data):
    """An `announcement-replies` socket event as {chat, parent, key_id, count},
    or None when it is not one. *parent* is the serialized parent message id
    (fromMe_chat_id[_participant]); *key_id* is the id part a stored message
    carries as key.id."""
    if not isinstance(data, dict):
        return None
    parent, chat, count = data.get("id"), data.get("chatId"), data.get("replyCount")
    if (not isinstance(parent, str) or not isinstance(chat, str) or not chat.endswith("@g.us")
            or isinstance(count, bool) or not isinstance(count, int) or count < 0):
        return None
    parts = parent.split("_", 3)
    if len(parts) < 3 or parts[1] != chat or not parts[2]:
        return None
    return {"chat": chat, "parent": parent, "key_id": parts[2], "count": count}


def apply_reply_count(chat, key_id, count):
    """Store *count* on the cached copy of the announcement *key_id* in *chat*.
    True when a stored message changed."""
    wrapper = (chat.get("messages") or {}) if isinstance(chat, dict) else {}
    records = (wrapper.get("messages") or {}).get("records") if isinstance(wrapper, dict) else None
    changed = False
    for row in records if isinstance(records, list) else ():
        if (isinstance(row, dict) and (row.get("key") or {}).get("id") == key_id
                and row.get("replyCount") != count):
            row["replyCount"] = count
            changed = True
    return changed
