"""Transcribing several selected messages in one go (issue #469): the pure parts.

The panel's bulk_transcription mixin owns the worker, the speech and the
window; what is decided here has no wx and no MainWindow, so it is tested
directly:

* **the plan** — which of the selected messages are run, in the order they
  appear in the conversation (chronological), which are skipped because they
  hold nothing to transcribe (text, images, documents that are not audio or
  video, GIFs), and which already have a transcription for the selected model
  and are reused instead of run again;
* **the tally** — how many were transcribed, skipped and failed, which is what
  the closing sentence says;
* **the combined text** — every transcription, oldest first, each headed by its
  sender and its date and time the way "Copy selected messages" writes them
  (core.message_copy_format), for the window that shows them all together.

Nothing here logs: the texts are the content of private conversations.
"""

from dataclasses import dataclass, field

from core.message_copy_format import format_copied_message
from core.transcription import message_audio
from core.transcription import stored as stored_transcription


@dataclass(frozen=True)
class BulkPlan:
    #: Messages to run, oldest first.
    to_run: tuple = ()
    #: Messages that already hold a transcription for the selected model.
    already: tuple = ()
    #: How many selected messages hold nothing to transcribe.
    skipped: int = 0

    @property
    def total(self) -> int:
        return len(self.to_run) + len(self.already)


def plan(messages, selected_ids, selection=None, is_separator=None) -> BulkPlan:
    """The plan for `selected_ids`, in the order of `messages` (the panel's
    `_sorted_messages`, oldest first)."""
    to_run, already, skipped = [], [], 0
    wanted = set(selected_ids or ())
    for msg in messages or ():
        if not isinstance(msg, dict):
            continue
        if is_separator is not None and is_separator(msg):
            continue
        if (msg.get("key") or {}).get("id") not in wanted:
            continue
        if not message_audio.is_transcribable(msg):
            skipped += 1
        elif stored_transcription.saved_transcription(msg, selection) is not None:
            already.append(msg)
        else:
            to_run.append(msg)
    return BulkPlan(tuple(to_run), tuple(already), skipped)


@dataclass
class BulkTally:
    transcribed: int = 0
    failed: int = 0
    skipped: int = 0
    #: (msg, text) in the order they were met — the combined window's content.
    texts: list = field(default_factory=list)


def combined_text(entries, sender_of, timestamp_of, datetime_format) -> str:
    """Every (msg, text) of `entries` as one block, oldest first: each one
    "<date> <time> - <sender>: <text>", separated by a blank line."""
    blocks = []
    for msg, text in entries:
        if not text:
            continue
        blocks.append(format_copied_message(
            timestamp_of(msg), sender_of(msg), text, datetime_format))
    return "\n\n".join(blocks)
