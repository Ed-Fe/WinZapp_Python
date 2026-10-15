"""Which conversations the automatic transcription and description may act on.

The automatic options (local transcription, core/transcription/auto_queue.py,
and the online transcription and description, core/ai_media/auto.py) are
install-wide switches; this is the per-conversation override on top of them,
set in the conversation's data window. Two boxes, both checked unless the
user cleared them:

* ``transcribe``: voice messages and audio, and the sound track of videos;
* ``describe``: pictures, stickers and videos described by an AI provider.

The setting ``conversation_auto_ai`` maps a normalized JID to the boxes that
were *cleared*: ``{"<jid>": {"transcribe": False}}``. A conversation with
nothing cleared has no entry, so the map stays as small as the exceptions, and
a box that was never touched follows whatever the global option says.
"""

SETTING = "conversation_auto_ai"
TRANSCRIBE = "transcribe"
DESCRIBE = "describe"


def allowed(settings, jid, which) -> bool:
    """Whether the automatic option `which` may act on conversation `jid`."""
    section = settings.get(SETTING) if isinstance(settings, dict) else None
    entry = section.get(jid) if isinstance(section, dict) else None
    return not (isinstance(entry, dict) and entry.get(which) is False)


def set_allowed(settings, jid, which, value) -> None:
    """Record the box for `jid`; an entry with nothing cleared is removed."""
    section = settings.get(SETTING)
    if not isinstance(section, dict):
        section = settings[SETTING] = {}
    entry = section.get(jid)
    entry = dict(entry) if isinstance(entry, dict) else {}
    if value:
        entry.pop(which, None)
    else:
        entry[which] = False
    if entry:
        section[jid] = entry
    else:
        section.pop(jid, None)
