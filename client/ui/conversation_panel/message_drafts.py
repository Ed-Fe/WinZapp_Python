"""The message field's text stays with its conversation (core/message_drafts.py).

Left behind by navigate_to_conversation() and _close_conversation_core() before
the conversation changes, and put back once the new one is set. The text
being edited is not a draft: edit mode is left on the way out, as Esc would.
"""

from core import message_drafts
from core.message_drafts import MessageDrafts


class MessageDraftsMixin:
    """Per-conversation drafts of the message field."""

    def _message_drafts(self) -> MessageDrafts:
        drafts = getattr(self, "_message_drafts_store", None)
        if drafts is None:
            drafts = self._message_drafts_store = MessageDrafts()
        return drafts

    def _draft_key(self, conversation):
        jid = (conversation or {}).get("remoteJid", "") if isinstance(conversation, dict) else ""
        if not jid:
            return ""
        normalize = getattr(self.main_window, "_normalize_jid", None)
        return normalize(jid) if callable(normalize) else jid

    def _draft_mode(self) -> str:
        return message_drafts.mode(getattr(self.main_window, "settings", None))

    def _stash_message_draft(self) -> None:
        """Leaving the conversation: keep what the field holds as its draft,
        or throw it away, as Settings says; then empty the field. Nothing to
        keep while a message is being edited. With KEEP_IN_FIELD the field is
        left as it is, as before drafts existed."""
        mode = self._draft_mode()
        if mode == message_drafts.KEEP_IN_FIELD:
            return
        key = self._draft_key(self.conversation)
        if getattr(self, "_editing_message_id", None) is not None:
            self._on_cancel_edit()
        elif mode == message_drafts.DISCARD:
            self._message_drafts().take(key)
        elif key:
            self._message_drafts().stash(
                key, self.message_field.GetValue(),
                getattr(self, "_pending_mentions", ()),
                getattr(self, "_pending_mention_display_names", None))
        self._put_in_message_field("")

    def _restore_message_draft(self) -> None:
        """Opening the conversation: put its draft back in the field, with its
        mentions; an empty field when it has none, or when drafts are thrown
        away. With KEEP_IN_FIELD the field is left as it is."""
        mode = self._draft_mode()
        if mode == message_drafts.KEEP_IN_FIELD:
            return
        draft = self._message_drafts().take(self._draft_key(self.conversation))
        if draft is None or mode == message_drafts.DISCARD:
            self._put_in_message_field("")
            return
        self._pending_mentions = list(draft.mentions)
        self._pending_mention_display_names = dict(draft.mention_names)
        self._rebuild_mention_pills()
        self._put_in_message_field(draft.text)

    def _put_in_message_field(self, text: str) -> None:
        """Set the field without telling the contact "typing..." and without a
        spelling cue for text nobody just typed (on_change_message_field()
        reads _restoring_draft)."""
        if self.message_field.GetValue() == text:
            return
        self._restoring_draft = True
        try:
            self.message_field.SetValue(text)
            self.message_field.SetInsertionPointEnd()
        finally:
            self._restoring_draft = False
