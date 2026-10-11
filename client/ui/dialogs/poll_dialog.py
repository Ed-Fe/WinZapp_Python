"""
WinZapp – Poll Dialog
=====================
Opened with Enter on a poll message. Shows the question, whether one or several
options may be chosen, and the options with their votes, most voted first (the
order WhatsApp's own desktop uses). A plain wx.ListCtrl, one row per option, so
the screen reader says "name, votes: N" and nothing else.
"""

import wx

from core.polls import allows_multiple, option_label, options_by_votes, poll_options


class PollDialog(wx.Dialog):
    def __init__(self, main_window, poll: dict):
        i18n = main_window.i18n
        super().__init__(
            main_window,
            title=i18n.t("poll_dialog_title"),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        question = wx.TextCtrl(
            panel, value=(poll or {}).get("name") or "",
            style=wx.TE_READONLY | wx.TE_MULTILINE,
        )
        sizer.Add(wx.StaticText(panel, label=i18n.t("poll_dialog_question_label")),
                  0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        sizer.Add(question, 0, wx.EXPAND | wx.ALL, 8)

        hint = i18n.t("poll_allows_several" if allows_multiple(poll) else "poll_allows_one")
        sizer.Add(wx.StaticText(panel, label=hint), 0, wx.LEFT | wx.RIGHT, 8)

        self._list = wx.ListCtrl(
            panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.LC_NO_HEADER
        )
        self._list.InsertColumn(0, "", width=340)
        self._list.Freeze()
        try:
            for option in options_by_votes(poll_options(poll)):
                self._list.InsertItem(self._list.GetItemCount(), option_label(i18n, option))
        finally:
            self._list.Thaw()
        if self._list.GetItemCount():
            self._list.Focus(0)
            self._list.Select(0)
        sizer.Add(self._list, 1, wx.EXPAND | wx.ALL, 8)

        sizer.Add(wx.Button(panel, wx.ID_CANCEL, label=i18n.t("close")),
                  0, wx.ALIGN_CENTER | wx.BOTTOM, 8)
        panel.SetSizer(sizer)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(outer)
        self.SetMinSize((380, 320))
        self.Fit()
        self.CentreOnParent()
        self._list.SetFocus()
