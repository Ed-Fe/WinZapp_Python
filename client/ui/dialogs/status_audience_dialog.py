"""Picking who a Status is hidden from, or shared only with.

Opened from File > WhatsApp settings > Privacy (whatsapp_settings_dialog.py)
when "My contacts except..." or "Only share with..." is chosen. The people come
from the contacts WinZapp already synchronised with WhatsApp, one row per person
(ui.dialogs.contact_list_picker.build_own_contact_rows, the same rows as the
other contact pickers).

A check list rather than the shared multi-select ContactListPicker: an audience
is built over several searches ("Ana", then "Bruno"...), and a multi-select list
rebuilt on every keystroke forgets what was picked before the filter changed.
Here the choice is kept by person, outside the list, so filtering never adds or
drops anyone; Space ticks a row, and NVDA reads its checked state.

Someone already in the list on WhatsApp but not among the synchronised contacts
keeps a row of their own (shown by number), so opening and confirming the
dialog never drops a person silently.
"""

import wx

from core.utils import contact_dedup_key, contact_search_matches, format_number
from ui.dialogs.contact_list_picker import build_own_contact_rows


def audience_rows(main_window, current: "list | tuple") -> list:
    """(key, label, jid) for every pickable person, contacts first (sorted by
    name, as the other pickers), then the people of *current* that are not
    among them. *key* identifies the person across JID forms."""
    rows = []
    seen = set()
    for name, phone, entry in build_own_contact_rows(main_window):
        jid = entry["remoteJid"]
        key = contact_dedup_key(main_window, jid)
        if key in seen:
            continue
        seen.add(key)
        label = name if name == phone else f"{name}, {phone}"
        rows.append((key, label, jid))
    for jid in current or ():
        key = contact_dedup_key(main_window, jid)
        if key in seen:
            continue
        seen.add(key)
        rows.append((key, main_window.i18n.t("wa_privacy_status_unknown_contact").format(
            number=format_number(jid)), jid))
    return rows


class StatusAudienceDialog(wx.Dialog):
    """Returns, after ShowModal() == wx.ID_OK, the chosen people as JIDs
    (``chosen_jids()``)."""

    def __init__(self, parent, main_window, title: str, current: "list | tuple"):
        self._mw = main_window
        i18n = main_window.i18n
        super().__init__(parent, title=title,
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._rows = audience_rows(main_window, current)
        current_keys = {contact_dedup_key(main_window, jid) for jid in current or ()}
        self._checked = {key for key, _label, _jid in self._rows if key in current_keys}
        self._shown = []  # keys of the rows currently in the list, in order

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(wx.StaticText(self, label=i18n.t("group_search_label")), 0,
                  wx.LEFT | wx.TOP | wx.RIGHT, 8)
        self._search = wx.TextCtrl(self, style=wx.TE_DONTWRAP)
        self._search.Bind(wx.EVT_TEXT, lambda event: self._render())
        self._search.Bind(wx.EVT_KEY_DOWN, self._on_search_key)
        sizer.Add(self._search, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        sizer.Add(wx.StaticText(self, label=i18n.t("wa_privacy_status_list_label")), 0,
                  wx.LEFT | wx.TOP | wx.RIGHT, 8)
        self._list = wx.CheckListBox(self)
        self._list.Bind(wx.EVT_CHECKLISTBOX, self._on_check)
        sizer.Add(self._list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        self._count = wx.StaticText(self, label="")
        sizer.Add(self._count, 0, wx.ALL, 8)

        buttons = wx.StdDialogButtonSizer()
        ok = wx.Button(self, wx.ID_OK, i18n.t("ok"))
        ok.SetDefault()
        buttons.AddButton(ok)
        buttons.AddButton(wx.Button(self, wx.ID_CANCEL, i18n.t("cancel")))
        buttons.Realize()
        sizer.Add(buttons, 0, wx.ALL | wx.ALIGN_RIGHT, 8)

        self.SetSizer(sizer)
        self.SetMinSize((360, 420))
        self.SetSize((440, 520))
        self.CentreOnParent()
        self._render()
        self._search.SetFocus()

    def _render(self):
        query = self._search.GetValue()
        self._list.Freeze()
        try:
            self._list.Clear()
            self._shown = []
            for key, label, _jid in self._rows:
                if not contact_search_matches(query, label, label):
                    continue
                index = self._list.Append(label)
                self._list.Check(index, key in self._checked)
                self._shown.append(key)
            if not self._shown:
                # Never an empty, silent list (same as the other pickers).
                self._list.Append(self._mw.i18n.t("no_contacts"))
        finally:
            self._list.Thaw()
        if self._shown:
            self._list.SetSelection(0)
        self._update_count()

    def _update_count(self):
        self._count.SetLabel(self._mw.i18n.t("wa_privacy_status_count").format(
            count=len(self._checked)))

    def _on_check(self, event):
        index = event.GetInt()
        if not 0 <= index < len(self._shown):
            # The "no contacts" row is not a person.
            self._list.Check(index, False)
            return
        key = self._shown[index]
        if self._list.IsChecked(index):
            self._checked.add(key)
        else:
            self._checked.discard(key)
        self._update_count()

    def _on_search_key(self, event):
        if event.GetKeyCode() in (wx.WXK_DOWN, wx.WXK_UP) and self._shown:
            self._list.SetFocus()
            return
        event.Skip()

    def chosen_jids(self) -> list:
        return [jid for key, _label, jid in self._rows if key in self._checked]
