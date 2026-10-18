"""Which conversations the automatic transcription and description may act on.

The automatic options (local transcription, core/transcription/auto_queue.py,
and the online transcription and description, core/ai_media/auto.py) are
install-wide switches; this is the per-conversation layer on top of them,
set in the conversation's data window. Two boxes per conversation:

* ``transcribe``: voice messages and audio, and the sound track of videos;
* ``describe``: pictures, stickers and videos described by an AI provider.

What a box shows before the user touches it is decided by the **scope**
(Settings > Transcriptions & Descriptions, ``conversation_auto_ai_scope``):

* ``all`` (default): every conversation, except the ones the user turned off;
* ``selected``: no conversation, except the ones the user turned on — the
  way to have the options on for a handful of chats without visiting the
  rest to clear them.

``conversation_auto_ai`` maps a normalized JID to the boxes the user set
explicitly: ``{"<jid>": {"transcribe": False}}``. A box equal to the scope's
default is not stored, so the map stays as small as the exceptions.
"""

SETTING = "conversation_auto_ai"
SCOPE_SETTING = "conversation_auto_ai_scope"
SCOPE_ALL = "all"
SCOPE_SELECTED = "selected"
TRANSCRIBE = "transcribe"
DESCRIBE = "describe"


def scope(settings) -> str:
    """The stored scope; anything unknown reads as "all"."""
    value = settings.get(SCOPE_SETTING) if isinstance(settings, dict) else None
    return SCOPE_SELECTED if value == SCOPE_SELECTED else SCOPE_ALL


def default_allowed(settings) -> bool:
    """What a conversation nobody touched allows."""
    return scope(settings) == SCOPE_ALL


def allowed(settings, jid, which) -> bool:
    """Whether the automatic option `which` may act on conversation `jid`."""
    section = settings.get(SETTING) if isinstance(settings, dict) else None
    entry = section.get(jid) if isinstance(section, dict) else None
    if isinstance(entry, dict) and isinstance(entry.get(which), bool):
        return entry[which]
    return default_allowed(settings)


def set_allowed(settings, jid, which, value) -> None:
    """Record the box for `jid`; one equal to the scope's default is dropped."""
    section = settings.get(SETTING)
    if not isinstance(section, dict):
        section = settings[SETTING] = {}
    entry = section.get(jid)
    entry = dict(entry) if isinstance(entry, dict) else {}
    if bool(value) == default_allowed(settings):
        entry.pop(which, None)
    else:
        entry[which] = bool(value)
    if entry:
        section[jid] = entry
    else:
        section.pop(jid, None)


# ── Automatic transcription: one box per kind, inherited until chosen ────────
#
# Asked for: in a conversation's data window, a box for the automatic local
# transcription (Whisper) and one for the automatic online one (AI provider),
# each showing — until the user sets it for that conversation — what the
# install-wide option says right now, and each able to turn the automatic
# transcription ON for the conversation even while that option is off.
# Unlike `allowed()`, a choice is therefore always stored, even when it equals
# what the conversation would inherit: "chosen once" is the whole difference.
#
# The shared "transcribe" key above is the box both used to be; an entry that
# holds it still speaks for both until one of the two is set.

TRANSCRIBE_LOCAL = "transcribe_local"
TRANSCRIBE_ONLINE = "transcribe_online"
_TRANSCRIPTION_BOXES = (TRANSCRIBE_LOCAL, TRANSCRIBE_ONLINE)


def transcription_choice(settings, jid, which):
    """What the user chose for `jid` (True/False), or None when the box was
    never set and so inherits."""
    section = settings.get(SETTING) if isinstance(settings, dict) else None
    entry = section.get(jid) if isinstance(section, dict) else None
    if not isinstance(entry, dict):
        return None
    for key in (which, TRANSCRIBE):
        if isinstance(entry.get(key), bool):
            return entry[key]
    return None


def transcription_wanted(settings, jid, which, globally_on) -> bool:
    """Whether the automatic transcription `which` runs on `jid`: the
    conversation's own choice, else the install-wide option `globally_on`
    (within the scope, as for every conversation box)."""
    choice = transcription_choice(settings, jid, which)
    if choice is not None:
        return choice
    return bool(globally_on) and default_allowed(settings)


def set_transcription(settings, jid, which, value) -> None:
    """Record `which` for `jid` as an explicit choice. The old shared box
    becomes the other one's choice, so it keeps meaning what it meant."""
    if which not in _TRANSCRIPTION_BOXES:
        raise ValueError(which)
    section = settings.get(SETTING)
    if not isinstance(section, dict):
        section = settings[SETTING] = {}
    entry = section.get(jid)
    entry = dict(entry) if isinstance(entry, dict) else {}
    legacy = entry.pop(TRANSCRIBE, None)
    if isinstance(legacy, bool):
        other = TRANSCRIBE_ONLINE if which == TRANSCRIBE_LOCAL else TRANSCRIBE_LOCAL
        entry.setdefault(other, legacy)
    entry[which] = bool(value)
    section[jid] = entry
