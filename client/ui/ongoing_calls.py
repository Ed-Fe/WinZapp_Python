"""Visible Calls-tab discovery, stale-reply rejection and explicit native joins."""
import logging
import threading

import wx

from core.ongoing_calls import ongoing_entries, include_current_call


class OngoingCallsMixin:
    _ONGOING_POLL_MS = 10_000

    def _init_ongoing_calls(self, sizer):
        self._ongoing_entries = []
        self._ongoing_unavailable = False
        self._ongoing_watch = None
        self._ongoing_timer = None
        self._ongoing_inflight = None
        self._ongoing_join_btn = wx.Button(self, label=self.main_window.i18n.t("group_call_join_button"))
        self._ongoing_join_btn.Bind(wx.EVT_BUTTON, self._join_ongoing_call)
        sizer.Add(self._ongoing_join_btn, 0, wx.LEFT | wx.BOTTOM, 5)
        self._ongoing_join_btn.Hide()
        self._ongoing_participants_btn = wx.Button(self, label=self.main_window.i18n.t("call_participants_button"))
        self._ongoing_participants_btn.Bind(wx.EVT_BUTTON, self._show_ongoing_participants)
        sizer.Add(self._ongoing_participants_btn, 0, wx.LEFT | wx.BOTTOM, 5)
        self._ongoing_participants_btn.Hide()
        self.Bind(wx.EVT_SHOW, self._on_ongoing_visibility)

    def _on_ongoing_visibility(self, event):
        if event.GetEventObject() is self and not event.IsShown():
            self._ongoing_watch = None
            self._ongoing_entries = []
            if self._ongoing_timer is not None:
                self._ongoing_timer.Stop()
                self._ongoing_timer = None
        event.Skip()

    def _watch_ongoing_calls(self):
        if self._ongoing_watch is None:
            self._ongoing_watch = object()
            # A cached row from a previous visit must not remain joinable
            # while the fresh native request is pending.
            self._ongoing_entries = []
            self._populate_all()
        self._poll_ongoing_calls(self._ongoing_watch)

    def _poll_ongoing_calls(self, watch):
        if self._ongoing_timer is not None:
            self._ongoing_timer.Stop()
            self._ongoing_timer = None
        if (not self or self._ongoing_watch is not watch or not self.IsShown()
                or getattr(self.main_window, "_shutting_down", False)):
            return
        if self._ongoing_inflight is not None:
            self._schedule_ongoing_poll(watch)
            return
        self._ongoing_inflight = watch

        def worker():
            entries, unavailable = [], False
            try:
                mw = self.main_window
                response = mw._raise_for_call_response(
                    mw._post_call_control("ongoing", {}, timeout=20), "ongoing")
                entries = ongoing_entries(response.json())
            except Exception:
                unavailable = True
                logging.warning("[calls_tab] ongoing call check unavailable")
            wx.CallAfter(self._finish_ongoing_poll, watch, entries, unavailable)

        threading.Thread(target=worker, daemon=True, name="ongoing-calls").start()

    def _schedule_ongoing_poll(self, watch):
        if self and self._ongoing_watch is watch and self.IsShown():
            self._ongoing_timer = wx.CallLater(
                self._ONGOING_POLL_MS, self._poll_ongoing_calls, watch)

    def _finish_ongoing_poll(self, watch, entries, unavailable):
        if self._ongoing_inflight is watch:
            self._ongoing_inflight = None
        if (not self or self._ongoing_watch is not watch or not self.IsShown()
                or getattr(self.main_window, "_shutting_down", False)):
            return
        self._ongoing_entries = entries
        self._ongoing_unavailable = unavailable
        self._populate_all()
        self._schedule_ongoing_poll(watch)

    def _ongoing_rows_for_tab(self, tab):
        from core.call_log import TAB_ALL, TAB_ONGOING
        if tab not in (TAB_ALL, TAB_ONGOING):
            return []
        locked = getattr(self.main_window, "is_chat_locked", lambda _jid: False)
        rows = include_current_call(getattr(self, "_ongoing_entries", []),
            getattr(self.main_window, "_active_voice_call", None),
            getattr(self.main_window, "_voice_call_last_announced_state", ""))
        return [entry for entry in rows if not locked(entry["jid"])]

    def _selected_ongoing(self):
        entry = self.focused_entry()
        if entry is None or not entry.get("ongoing"):
            return None
        if getattr(self.main_window, "is_chat_locked", lambda _jid: False)(entry["jid"]):
            return None
        # Refuse a selected stale row even if a delayed history refresh kept it.
        return next((row for row in self._ongoing_rows_for_tab(self.current_tab())
                     if row["ongoing"]["id"] == entry["ongoing"]["id"]), None)

    def _update_ongoing_join_button(self):
        entry = self._selected_ongoing()
        button = self._ongoing_join_btn
        if entry is None and button.HasFocus():
            self.current_list().SetFocus()
        if entry is not None:
            call = entry["ongoing"]
            active = getattr(self.main_window, "_active_voice_call", None) or {}
            key = ("calls_return_to_current" if active.get("call_id") == call["id"] else
                   "group_call_join_video_button" if call["isVideo"] else "group_call_join_button")
            label = self.main_window.i18n.t(key)
            if button.GetLabel() != label:
                button.SetLabel(label)
        if button.IsShown() != (entry is not None):
            button.Show(entry is not None)
        participants = self._ongoing_participants_btn
        if entry is None and participants.HasFocus():
            self.current_list().SetFocus()
        label = self.main_window.i18n.t("call_participants_button")
        if participants.GetLabel() != label:
            participants.SetLabel(label)
        if participants.IsShown() != (entry is not None):
            participants.Show(entry is not None)

    def _show_ongoing_participants(self, _event=None):
        entry = self._selected_ongoing()
        if entry is not None:
            self.main_window.show_call_participants(entry["ongoing"]["id"], entry["jid"], parent=self)

    def _join_ongoing_call(self, _event=None):
        entry = self._selected_ongoing()
        if entry is None:
            return
        call, mw = entry["ongoing"], self.main_window
        active = getattr(mw, "_active_voice_call", None) or {}
        if active.get("call_id") == call["id"]:
            mw._sync_voice_call_bar()
            mw._focus_call_window()
            return
        mw._start_call(call["chatJid"], mw.chat_display_name(entry["jid"]),
                       is_group=True, is_video=call["isVideo"], join_call_id=call["id"])

    def _ongoing_context_menu(self):
        entry = self._selected_ongoing()
        if entry is None:
            return
        self._update_ongoing_join_button()
        menu = wx.Menu()
        item = menu.Append(wx.ID_ANY, self._ongoing_join_btn.GetLabel()+"\tEnter")
        self.Bind(wx.EVT_MENU, self._join_ongoing_call, item)
        opened = menu.Append(wx.ID_ANY, self.main_window.i18n.t("calls_open_conversation"))
        self.Bind(wx.EVT_MENU, lambda e: self.main_window.navigate_to_conversation_jid(entry["jid"]), opened)
        self.PopupMenu(menu)
        menu.Destroy()
