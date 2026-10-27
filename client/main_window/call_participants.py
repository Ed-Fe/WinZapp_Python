"""Explicit participant views for the active call and the Calls tab."""
from core.call_matching import is_placeholder_call_id
from core.call_participants import active_participants_available


class CallParticipantsMixin:
    def _show_active_call_participants(self, _event=None):
        active = getattr(self, "_active_voice_call", None) or {}
        if not active_participants_available(active, getattr(self, "_voice_call_last_announced_state", "")):
            return
        self.show_call_participants(active.get("call_id"), active.get("peer_jid"),
                                    parent=getattr(self, "voice_call_window", None))

    def show_call_participants(self, call_id, chat_jid, *, parent=None):
        if not call_id or is_placeholder_call_id(call_id) or getattr(self, "_shutting_down", False):
            return
        allowed = lambda: not getattr(self, "is_chat_locked", lambda _jid: False)(chat_jid)
        if not allowed():
            return
        from ui.dialogs.call_participants import CallParticipantsDialog
        dialog = CallParticipantsDialog(parent or self, self, call_id,
                                        self.chat_display_name(chat_jid), allowed)
        try:
            dialog.ShowModal()
        finally:
            dialog._dispose_participants()
            dialog.Destroy()
