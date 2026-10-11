"""PollMessagesMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Attaching a poll (attachment menu → NewPollDialog → a pending row + the send
queue) and opening a received poll (Enter on the row → PollDialog). The poll
rules themselves — validation, option counts, the spoken row — are core/polls.py.
"""

import time
import uuid

import wx

from core.message_queue import PendingMessage
from core.polls import poll_content
from ui.conversation_panel.typing_row import append_message_row, message_row_count


class PollMessagesMixin:
    """Sending polls and showing a poll's options."""

    def _on_attach_poll(self, event=None):
        from ui.dialogs.new_poll_dialog import NewPollDialog

        remote_jid = (self.conversation or {}).get("remoteJid", "")
        if not remote_jid:
            return
        dlg = NewPollDialog(self.main_window)
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            question, options, selectable = dlg.question, dlg.options, dlg.selectable
        finally:
            dlg.Destroy()

        local_id = str(uuid.uuid4())
        virtual_msg = {
            "_local_pending": True,
            "_local_id":      local_id,
            "key": {"id": local_id, "fromMe": True, "remoteJid": remote_jid},
            "messageType": "pollCreationMessage",
            "message": {
                "pollCreationMessage": {
                    "name": question,
                    "options": [{"name": opt} for opt in options],
                    "selectableOptionsCount": selectable,
                }
            },
            "messageTimestamp": int(time.time()),
            "pushName": "",
        }
        self._clear_empty_placeholder()
        self._sorted_messages.append(virtual_msg)
        append_message_row(self, self._render_message_line(virtual_msg))
        last = message_row_count(self) - 1   # the row just sent, not the typing row
        if last >= 0:
            self.messages_list.EnsureVisible(last)
        pm = PendingMessage(
            local_id, remote_jid,
            poll={"name": question, "options": options, "selectableCount": selectable},
        )
        self.main_window.message_queue.enqueue(pm)
        self.main_window.mark_conversation_as_read(remote_jid)

        self._register_virtual_msg(virtual_msg)
        self.main_window._schedule_set_chats()

    def _show_poll_dialog(self, msg: dict) -> None:
        """Enter on a poll row: the question and every option with its votes."""
        from ui.dialogs.poll_dialog import PollDialog

        dlg = PollDialog(self.main_window, poll_content(msg.get("message") or {}))
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()
