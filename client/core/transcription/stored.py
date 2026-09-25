"""A transcription kept with its message, and the rules that keep it there.

A transcription takes minutes; doing it twice for the same voice note is the
waste this module exists to prevent. It lives inside the message's own record,
under `TRANSCRIPTION_KEY`, which puts it inside `message_json` — the column the
database encrypts with the per-install Fernet key. That is the whole privacy
argument: a column of its own would have been the text of a private
conversation in the clear, readable by anyone holding `messages.db`.

The key sits at the **top level of the record**, beside `_local_pending` and
the other local-only fields, never inside `message`: the `message` payload is
what a reply copies into its `quotedMessage` and what prune/slim helpers
rewrite, and a transcription carried along with it would end up stored under
somebody else's message.

What makes it hard is that nothing on the server knows about it, and every
resync writes the server's copy over the record — in the database
(`INSERT OR REPLACE` of the whole row) and in memory (the conversation's
records replaced by the API's). `MEASURED_SECONDS_KEY` hit exactly this and
settled the shape: one rule, applied on both sides, at the single point every
write passes through. The rule here is one step stricter, because unlike a
measured duration a transcription can be *deleted*:

* **Every value carries the time it was decided (`at`), and the later decision
  wins.** A copy arriving without the key inherits what is stored; a copy
  arriving with it wins only if it is at least as recent. Deleting writes a
  **tombstone** (`{"deleted": True, "at": ...}`) instead of removing the key,
  so a stale copy still holding the text — the conversation panel's, a star
  toggled on the old dict, a sync that read the records before the delete —
  loses to it instead of bringing the transcription back. Removing the key
  would have turned every one of those copies into "a copy with the key wins".

* **Only a message that can hold speech carries one.** `audioMessage` and
  `documentMessage` — by type, not by mimetype, because a message's type never
  changes while one copy of a document may lack the mimetype another had, and
  dropping a transcription because of that would be a silent loss.

* **Only a withdrawn message loses it.** A message deleted for everyone comes
  back under its own id as a `protocolMessage` (`websocket_client`'s
  `"revoked"`, `MainWindow._apply_remote_revoke()`), and the text of what its
  sender withdrew is not something to keep. Any other copy that is not audio
  or a document says nothing about the transcription either way. A known kind
  (`OTHER_KINDS`) cannot be the same message, so it is left as it came, with no
  read; a type WPPConnect passes through unmapped (a `ciphertext` placeholder
  of a note not yet decrypted, say) may well be the voice note itself, so the
  stored value is kept exactly as it is — neither the copy's key adopted nor
  the stored one dropped. Dropping on "not audio" alone would have erased a
  transcription for good the first time such a placeholder was written.

* **Merging two copies keeps the later decision.** The same message can sit
  under two chats (an `@lid` and its phone number) until they are merged, and
  a merge deletes one of the two. `fold_transcription()` is what the survivor
  takes from the one being dropped — in the database
  (`merge_or_rename_chat()`) and in memory (`_merge_lid_into_phone()`,
  `deduplicate_chats()`).

* **The time of a decision only moves forward.** `at` comes from the wall
  clock, and a clock corrected backwards between two decisions would make the
  newer one lose — a delete undone by the next sync. `next_decision_time()`
  dates every new decision after the one it replaces.

What is deliberately *not* kept: a full resync (F5) or a logout clears the
database with `import_from_dict(clear_first=True)`, and every transcription
goes with it, exactly as the downloaded voice notes in `voice_messages/` do —
they were made from those files, and a wipe that kept them would keep the
text of conversations the user asked to forget. And a message that enters
memory through `on_historical_message()` or `fetch_older_messages()` arrives
from the server without the key: the row on disk keeps it (the write goes
through the rule), but the menu offers "Transcrever" rather than "Ver" until
the conversation is reopened and read back from the database. Reading every
such message back on the UI thread to avoid that would cost more than the
second run it could save.

Nothing here imports anything heavier than the backend's result type, and
`core.database` imports this module — which is why it is not in
`core.utils`, whose own imports start with `requests`.
"""

from __future__ import annotations

from core.transcription.backend import TranscriptionResult

#: Where the transcription lives in a message record. Private (leading
#: underscore) like `_measured_seconds`: WhatsApp never sends such a field, so
#: an incoming copy can never collide with it.
TRANSCRIPTION_KEY = "_transcription"

#: The types a transcription can be attached to — see the module docstring for
#: why by type and not by message_audio.is_transcribable()'s mimetype check.
CARRYING_TYPES = ("audioMessage", "documentMessage")

#: What a message deleted for everyone becomes — the only copy that drops the
#: key.
WITHDRAWN_TYPE = "protocolMessage"

#: The kinds websocket_client maps a message to that can never be a voice note
#: or a document seen differently: a message's type never changes, so a copy
#: of one of these is another message, and writing it needs no read of the
#: stored row. Anything outside this set and CARRYING_TYPES — a type passed
#: through unmapped — may be.
OTHER_KINDS = frozenset((
    "conversation", "extendedTextMessage", "imageMessage", "videoMessage",
    "stickerMessage", "contactMessage", "locationMessage", "liveLocationMessage",
    "pollCreationMessage", "buttonsMessage", "listMessage", "templateMessage",
    "groupNotification", "reactionMessage",
))


# ── What is stored ───────────────────────────────────────────────────────────


def record_from_result(result, at) -> dict:
    """The stored form of a finished `TranscriptionResult`.

    Everything the window needs to say the truth when it is opened again: the
    text, the detected language and how sure the model was (the language and
    confidence notes), the model and backend (said with the date), and
    `vad_used` — a transcription made without the voice filter has to keep
    warning about it on every reopening, since it is the one degradation a
    listener cannot hear. The segments are not kept: nothing reads them yet,
    and they would roughly double the size of every stored transcription.
    """
    probability = getattr(result, "language_probability", None)
    return {
        "text": getattr(result, "text", "") or "",
        "language": getattr(result, "language", None),
        "language_probability": float(probability) if isinstance(probability, (int, float))
        and not isinstance(probability, bool) else None,
        "model_id": getattr(result, "model_id", "") or "",
        "backend": getattr(result, "backend", "") or "",
        "vad_used": getattr(result, "vad_used", True) is not False,
        "at": float(at),
    }


def tombstone(at) -> dict:
    """What deleting leaves behind: no text, only the time of the decision."""
    return {"deleted": True, "at": float(at)}


def decision_time(value):
    """When `value` was decided, or None when it is not a stored decision."""
    if not isinstance(value, dict):
        return None
    at = value.get("at")
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return None
    return float(at)


def next_decision_time(now, *previous):
    """When a new decision is dated: `now`, or just after the latest of
    `previous` if the clock says otherwise.

    The later decision wins everywhere (`newer_decision()`), so a clock set
    back between two decisions — corrected after running fast, or simply
    wrong — would make the newer one lose to the older, and a sync still
    holding the older copy would bring a deleted text back. Per message, the
    order of decisions is what matters, not the hour.
    """
    at = float(now)
    for value in previous:
        before = decision_time(value)
        if before is not None and before >= at:
            at = before + 1e-3
    return at


def saved_transcription(msg):
    """The transcription stored on `msg`, or None — a tombstone is None too."""
    if not isinstance(msg, dict):
        return None
    value = msg.get(TRANSCRIPTION_KEY)
    if decision_time(value) is None or value.get("deleted"):
        return None
    text = value.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    return value


def as_result(value) -> TranscriptionResult:
    """A stored transcription as the result type narration and the window read.

    `vad_used` is believed only when it says True: a stored value that lost the
    field is treated like one made without the filter, because the warning is
    cheap and its absence is the one mistake the listener cannot notice.
    """
    probability = value.get("language_probability")
    if isinstance(probability, bool) or not isinstance(probability, (int, float)):
        probability = None
    return TranscriptionResult(
        text=value.get("text") or "",
        language=value.get("language") or None,
        language_probability=probability,
        duration_seconds=None,
        backend=value.get("backend") or "",
        model_id=value.get("model_id") or "",
        vad_used=value.get("vad_used") is True,
    )


# ── Which copy wins ──────────────────────────────────────────────────────────


def _message_type(msg):
    """`msg`'s type — `messageType`, or the payload's own key without it."""
    msg_type = msg.get("messageType")
    if msg_type:
        return msg_type
    payload = msg.get("message")
    if isinstance(payload, dict):
        for known in CARRYING_TYPES + (WITHDRAWN_TYPE,):
            if known in payload:
                return known
    return ""


def can_carry_transcription(msg) -> bool:
    """Whether `msg`'s type is one a transcription may be attached to."""
    if not isinstance(msg, dict):
        return False
    return _message_type(msg) in CARRYING_TYPES


def is_withdrawn(msg) -> bool:
    """Whether `msg` is a message its sender deleted for everyone."""
    return isinstance(msg, dict) and _message_type(msg) == WITHDRAWN_TYPE


def may_hold_transcription(msg) -> bool:
    """Whether a copy like `msg` may be the message a transcription belongs to.

    Audio and documents, and any type passed through unmapped; never a
    withdrawn message, nor a known other kind. What decides whether writing
    `msg` needs the stored row read first.
    """
    if not isinstance(msg, dict):
        return False
    msg_type = _message_type(msg)
    if msg_type in CARRYING_TYPES:
        return True
    return msg_type != WITHDRAWN_TYPE and msg_type not in OTHER_KINDS


def newer_decision(incoming, stored):
    """Which of two values of the key to keep: the later decision.

    A tie goes to `incoming` — the same decision written twice, where either
    answer is the same one. Anything that is not a stored decision counts as
    absent, so junk under the key can never outrank a real value.
    """
    incoming_at = decision_time(incoming)
    stored_at = decision_time(stored)
    if incoming_at is None:
        return stored if stored_at is not None else None
    if stored_at is None or incoming_at >= stored_at:
        return incoming
    return stored


def with_known_transcription(msg, stored_msg):
    """`msg` as it should be written over `stored_msg` — the database's rule.

    Returns `msg` itself when nothing changes, and a shallow copy otherwise, so
    the caller's dict (often a record the UI is holding) is never mutated by a
    write.
    """
    if not isinstance(msg, dict):
        return msg
    if is_withdrawn(msg):
        if TRANSCRIPTION_KEY not in msg:
            return msg
        cleaned = dict(msg)
        del cleaned[TRANSCRIPTION_KEY]
        return cleaned
    if not may_hold_transcription(msg):
        return msg
    incoming = msg.get(TRANSCRIPTION_KEY)
    stored = stored_msg.get(TRANSCRIPTION_KEY) if isinstance(stored_msg, dict) else None
    if not can_carry_transcription(msg):
        # A type passed through unmapped: the stored value stays exactly as
        # it is, whatever this copy holds — see the module docstring.
        if decision_time(stored) is None:
            return msg
        kept = stored
    else:
        kept = newer_decision(incoming, stored)
    if kept is incoming and (kept is not None or TRANSCRIPTION_KEY not in msg):
        return msg
    merged = dict(msg)
    if kept is None:
        merged.pop(TRANSCRIPTION_KEY, None)
    else:
        merged[TRANSCRIPTION_KEY] = kept
    return merged


def carry_over_transcriptions(new_msgs, old_msgs) -> int:
    """The same rule for the records held in memory. Returns how many
    transcriptions it carried — texts only, a tombstone carried is not one.

    A resync replaces a conversation's records with the server's copies, which
    never hold the key; without this the transcription would survive on disk
    and vanish from the screen at the first sync — "Ver transcrição" gone from
    the menu until the next restart. Mutates `new_msgs` in place, the way
    `carry_over_video_durations()` does.
    """
    known = {}
    for m in old_msgs or ():
        if not isinstance(m, dict):
            continue
        mid = (m.get("key") or {}).get("id") if isinstance(m.get("key"), dict) else None
        value = m.get(TRANSCRIPTION_KEY)
        if not mid or decision_time(value) is None:
            continue
        known[mid] = newer_decision(value, known.get(mid))
    if not known:
        return 0
    changed = 0
    for m in new_msgs or ():
        if not may_hold_transcription(m):
            continue
        mid = (m.get("key") or {}).get("id") if isinstance(m.get("key"), dict) else None
        if mid not in known:
            continue
        current = m.get(TRANSCRIPTION_KEY)
        kept = newer_decision(current, known[mid])
        if kept is not current:
            m[TRANSCRIPTION_KEY] = kept
            if not kept.get("deleted"):
                changed += 1
    return changed


def fold_transcription(dst_record, src_record) -> bool:
    """Give `dst_record` what `src_record` knows, before `src_record` is dropped.

    For merges: the same message filed under two chats, one copy about to be
    deleted. The survivor keeps the later decision of the two — a text only
    the dropped copy had, or a tombstone that must keep beating it — by the
    same `newer_decision()` every write goes through. Mutates `dst_record` in
    place, because in memory it is the dict the conversation panel may be
    holding, and returns whether anything changed, which is what tells the
    database side the surviving row needs writing.
    """
    if not isinstance(dst_record, dict) or not isinstance(src_record, dict):
        return False
    if not may_hold_transcription(dst_record):
        return False
    current = dst_record.get(TRANSCRIPTION_KEY)
    kept = newer_decision(current, src_record.get(TRANSCRIPTION_KEY))
    if kept is None or kept is current:
        return False
    dst_record[TRANSCRIPTION_KEY] = kept
    return True


# ── Finding the message again ────────────────────────────────────────────────


def find_record(records, msg_id):
    """The record in `records` that is message `msg_id`, or None.

    By `key.id` first; failing that, by `_local_id`, which is what an own
    message was known by while it was still being sent. A transcription takes
    minutes, and a message sent meanwhile has changed its id under the flow
    that captured the old one.
    """
    if not msg_id:
        return None
    fallback = None
    for record in records or ():
        if not isinstance(record, dict):
            continue
        key = record.get("key") if isinstance(record.get("key"), dict) else {}
        if key.get("id") == msg_id:
            return record
        if fallback is None and record.get("_local_id") == msg_id:
            fallback = record
    return fallback


def is_unsent(record) -> bool:
    """Whether `record` is an own message that has no WhatsApp id yet.

    Such a message is stored under a local UUID that is replaced when the send
    is confirmed — and its echo can arrive as a separate record that replaces
    it outright — so a transcription written under the UUID could end up on a
    row nothing reads again. It is refused instead, and the window says so.
    """
    if not isinstance(record, dict):
        return True
    if record.get("_local_pending"):
        return True
    local_id = record.get("_local_id")
    key = record.get("key") if isinstance(record.get("key"), dict) else {}
    return bool(local_id) and key.get("id") == local_id


def set_on_copies(copies, msg_id, value) -> int:
    """Put `value` under the key on every record in `copies` that is `msg_id`.

    The same message can be held by several dicts at once — the chat's
    records, its `lastMessage`, the conversation panel's own lists after a
    resync swapped the chat under it — and every one of them may be written
    back to the database by some later path. Returns how many distinct dicts
    were changed.
    """
    seen = set()
    for record in copies or ():
        if not isinstance(record, dict) or id(record) in seen:
            continue
        key = record.get("key") if isinstance(record.get("key"), dict) else {}
        if msg_id and key.get("id") == msg_id:
            record[TRANSCRIPTION_KEY] = value
            seen.add(id(record))
    return len(seen)


# ── What storing answered ────────────────────────────────────────────────────

#: MainWindow.store_message_transcription()'s answers. Only SAVE_STORED means
#: the transcription will be there next time; the other two are said in the
#: result window, so that nobody expects to find it again and does not.
SAVE_STORED = "stored"
SAVE_UNSENT = "unsent"
SAVE_MISSING = "missing"
