"""PollInlineMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

A single-answer poll can be voted on without opening it: while its row is
focused in the message list, the first Tab lands on a list of its options, and
ticking one casts the vote at once (unlike the poll window, which waits for the
"Vote" button). The list exists only while such a row is focused, so it never
adds a Tab stop anywhere else. Polls that accept several answers keep the
window, where the user can tick a few options before confirming.
"""

import wx

from core.polls import (
    allows_multiple,
    option_label,
    options_by_votes,
    poll_content,
    poll_options,
    vote_poll_ids,
)


def inline_poll_applies(msg: dict) -> bool:
    """Whether *msg* is a confirmed single-answer poll that can be voted on."""
    if not isinstance(msg, dict) or msg.get("_local_pending"):
        return False
    poll = poll_content(msg.get("message") or {})
    return bool(poll) and allows_multiple(poll) is False and bool(poll_options(poll))


class PollInlineMixin:
    """The options list that follows a focused single-answer poll."""

    def _create_inline_poll_list(self, parent, sizer):
        """Build the (hidden) options list right after the message list."""
        self._poll_inline_msg = None
        self._poll_inline_order = []
        self._poll_inline_loading = False
        lst = wx.ListCtrl(parent, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.LC_NO_HEADER)
        lst.EnableCheckBoxes(True)
        lst.InsertColumn(0, "", width=360)
        lst.SetName(self.main_window.i18n.t("new_poll_options_list_label").rstrip(":"))
        lst.Bind(wx.EVT_LIST_ITEM_CHECKED, self._on_inline_poll_checked)
        lst.Bind(wx.EVT_LIST_ITEM_UNCHECKED, self._on_inline_poll_checked)
        sizer.Add(lst, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 5)
        lst.Hide()
        self._poll_inline_list = lst

    def _update_inline_poll(self, idx: int) -> None:
        """Show the options list for the focused row if it is a single-answer
        poll, hide it otherwise. Called whenever the focused message changes."""
        lst = getattr(self, "_poll_inline_list", None)
        if lst is None:
            return
        msg = None
        if 0 <= idx < len(self._sorted_messages):
            candidate = self._sorted_messages[idx]
            if not self._is_separator(candidate) and inline_poll_applies(candidate):
                msg = candidate
        if msg is None:
            if lst.IsShown():
                lst.Hide()
                self.conversation_panel.Layout()
            self._poll_inline_msg = None
            return
        if msg is not self._poll_inline_msg:
            self._poll_inline_msg = msg
            self._fill_inline_poll(msg)
        else:
            self._reload_inline_poll()
        if not lst.IsShown():
            lst.Show()
            self.conversation_panel.Layout()

    def _fill_inline_poll(self, msg: dict) -> None:
        lst = self._poll_inline_list
        options = options_by_votes(poll_options(poll_content(msg.get("message") or {})))
        self._poll_inline_order = [opt["name"] for opt in options]
        self._poll_inline_loading = True
        lst.Freeze()
        try:
            lst.DeleteAllItems()
            for opt in options:
                index = lst.InsertItem(lst.GetItemCount(), option_label(self.main_window.i18n, opt))
                lst.CheckItem(index, opt["mine"])
        finally:
            lst.Thaw()
            self._poll_inline_loading = False
        lst.SetMinSize((-1, min(len(options), 6) * 24 + 8))

    def _reload_inline_poll(self) -> None:
        """New vote counts for the shown poll, without touching the ticks."""
        msg, lst = self._poll_inline_msg, self._poll_inline_list
        if msg is None:
            return
        by_name = {o["name"]: o for o in poll_options(poll_content(msg.get("message") or {}))}
        self._poll_inline_loading = True
        lst.Freeze()
        try:
            for index, name in enumerate(self._poll_inline_order):
                option = by_name.get(name)
                if option is None:
                    continue
                text = option_label(self.main_window.i18n, option)
                if lst.GetItemText(index) != text:
                    lst.SetItemText(index, text)
        finally:
            lst.Thaw()
            self._poll_inline_loading = False

    def _on_inline_poll_checked(self, event) -> None:
        event.Skip()
        if self._poll_inline_loading or self._poll_inline_msg is None:
            return
        lst, index = self._poll_inline_list, event.GetIndex()
        checked = lst.IsItemChecked(index)
        # Single answer: choosing one option replaces the previous choice.
        if checked:
            self._poll_inline_loading = True
            try:
                for other in range(lst.GetItemCount()):
                    if other != index and lst.IsItemChecked(other):
                        lst.CheckItem(other, False)
            finally:
                self._poll_inline_loading = False
        names = [self._poll_inline_order[index]] if checked else []
        msg = self._poll_inline_msg
        ids = vote_poll_ids(poll_options(poll_content(msg.get("message") or {})), names)
        i18n = self.main_window.i18n

        def done(ok: bool):
            self.main_window.speak_output.output(
                i18n.t("poll_vote_sent" if ok else "poll_vote_failed")
            )

        self._send_poll_vote(msg, ids, done)
