"""ChatSelectionMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Moved verbatim out of ui/conversations.py. Methods run with ``self`` bound to
the ConversationsPanel instance, so every attribute set in
ConversationsPanel.__init__/init_UI is available here.
"""

import wx
from ui.dialogs.clear_chat_confirm import confirm_clear_chat


class ChatSelectionMixin:
    """Multi-selection in the conversations list, its key handling and the bulk
    chat actions.
    """


    # ── Selection helpers (conversations list) ──────────────────────────────

    def _select_chat_at(self, idx: int) -> bool:
        """Add the chat at *idx* to self.selected_chats. Returns whether it
        was added (i.e. wasn't already selected)."""
        if not (0 <= idx < len(self.chats_list)):
            return False
        jid = self.chats_list[idx].get("remoteJid", "")
        if not jid or jid in self.selected_chats:
            return False
        self.selected_chats.add(jid)
        return True

    def _all_chat_jids(self) -> list:
        return [c.get("remoteJid", "") for c in self.chats_list if c.get("remoteJid", "")]

    def _chat_selection_visible(self) -> bool:
        """Whether any chat currently listed in chats_list is selected.

        The gate for the conversations list's selection mode (issue #99), and
        deliberately narrower than `bool(self.selected_chats)`: chats_list is
        reassigned to the *filtered* list every time the conversation filter
        RadioBox or the search box changes (MainWindow.add_chats_to_ui, which
        rebuilds it from the unfiltered _all_chats_list on every pass), and
        nothing clears selected_chats when it does. So a user who selects two
        chats and then switches to "Não lidas" sees no selection and was told
        nothing, yet plain Space would still have quietly added a third chat to
        a selection they believe does not exist. Hiding every selected chat now
        takes Space back to its native behaviour, which is what the user
        perceives; clearing the filter brings the mode back.

        The mass actions deliberately still act on the whole set — a user who
        selects and then filters expects them to hit everything they picked,
        and that behaviour predates the selection mode. This only gates what
        the unmodified Space key does.
        """
        if not self.selected_chats:
            return False
        return any(
            c.get("remoteJid", "") in self.selected_chats for c in self.chats_list
        )

    def _toggle_chat_selection(self, idx: int) -> bool:
        """Toggle the chat at *idx* in self.selected_chats, repaint the list
        and announce the change. Shared by Ctrl+Space and, once a selection
        exists, plain Space (_on_conv_list_key_down).

        Returns whether anything was actually toggled — plain Space hands the
        key back to the control when it wasn't (no focused row, or a row with
        no jid), rather than consuming a keystroke with no sound, speech or
        effect. Ctrl+Space keeps swallowing it, as it always has.

        The was_active/is_active bookkeeping below reads
        _chat_selection_visible(), not the raw set, because that is what
        _on_conv_list_key_down() gates plain Space on — and the announcement
        has to name the mode the gate actually enforces. Reading the raw set
        here left the two disagreeing: with one selected chat hidden by the
        filter, Ctrl+Space on a visible one announced no mode change (the raw
        set was already non-empty), deselecting it again announced none
        either, and the next plain Space then fell through to the control with
        nothing to show for it — the silent dead key, one surface over.
        """
        if not (0 <= idx < len(self.chats_list)):
            return False
        jid = self.chats_list[idx].get("remoteJid", "")
        if not jid:
            return False
        was_active = self._chat_selection_visible()
        if jid in self.selected_chats:
            self.selected_chats.remove(jid)
            self.main_window.add_chats_to_ui()
            self.main_window.output(
                self._selection_mode_announcement(
                    self.main_window.i18n.t("unselected"),
                    was_active, self._chat_selection_visible()),
                interrupt=True)
        else:
            self.selected_chats.add(jid)
            self.main_window.add_chats_to_ui()
            self.selection_sound.play()
            self.main_window.output(
                self._selection_mode_announcement(
                    self.main_window.i18n.t("selected"),
                    was_active, self._chat_selection_visible()),
                interrupt=True)
        return True

    def _bulk_shortcuts_enabled(self) -> bool:
        """Settings > User Interface > "Substituir atalhos por ações em massa
        ao selecionar conversas e mensagens" (default on). When enabled and a
        selection exists, the single-item shortcuts (forward, save, clear,
        delete, ...) act on the whole selection instead."""
        return self.main_window.settings.get("user_interface", {}).get(
            "bulk_action_shortcuts", True
        )

    def _selection_mode_enabled(self) -> bool:
        """Settings > User Interface > "Usar Espaço para marcar/desmarcar
        enquanto houver algo marcado" (default on). When enabled and the
        surface already has a selection, plain Space keeps selecting instead of
        doing its normal job (issue #99)."""
        return self.main_window.settings.get("user_interface", {}).get(
            "space_selects_in_selection_mode", True
        )

    def _escape_clears_selection_enabled(self) -> bool:
        """Settings > User Interface > "Esc desmarca as mensagens marcadas
        antes de fechar a conversa" (default on) — see
        _on_escape_conversation()."""
        return self.main_window.settings.get("user_interface", {}).get(
            "escape_clears_selection", True
        )

    def _selection_mode_announcement(self, base: str, was_active: bool, is_active: bool) -> str:
        """Append the selection-mode transition to *base* when the mode just
        turned on or off, and return the combined line.

        One string rather than a second output() call: every announcement here
        goes through output(..., interrupt=True), so speaking twice would cut
        "Selecionado" off mid-word with "Modo de seleção ativado".

        *base* is already-translated text and the two booleans are passed
        explicitly, so the forward dialog can use this against its own local
        set — the mode is derived from whether a selection exists, never
        stored.

        Deliberately not called from the mass actions (forward, save, delete,
        ...): they clear the selection as a side effect of acting on it and
        already announce their own result, and "5 mensagens encaminhadas. Modo
        de seleção desativado" is noise. The derived mode still ends correctly
        there, since it only ever reads the set.
        """
        if not self._selection_mode_enabled():
            return base
        if is_active and not was_active:
            return f"{base}. {self.main_window.i18n.t('selection_mode_on')}"
        if was_active and not is_active:
            return f"{base}. {self.main_window.i18n.t('selection_mode_off')}"
        return base

    def _on_conv_list_key_down(self, event):
        """Ctrl+Space toggles the focused chat's membership in
        self.selected_chats (the mass actions act on that set). Plain Space
        does the same once a selection already exists ("selection mode",
        issue #99) and otherwise keeps its old non-meaning here. Shift+Up/Down
        extend the selection to the previous/next row; Shift+Home/Shift+End select
        every row above/below the focused one and move focus to the
        first/last row; Ctrl+Shift+Space selects every chat, or clears the
        selection if everything is already selected. Opening a conversation
        stayed on Enter / double-click.
        Ctrl+P pins/unpins, Ctrl+Shift+Q archives/unarchives."""
        key   = event.GetKeyCode()
        ctrl  = event.ControlDown()
        shift = event.ShiftDown()
        idx   = self.conversations_list.GetFocusedItem()
        total = len(self.chats_list)

        if shift and key in (wx.WXK_DOWN, wx.WXK_NUMPAD_DOWN):
            target = (idx + 1) if idx >= 0 else 0
            if target < total:
                # Every announcement on this list derives the mode from
                # _chat_selection_visible(), the same predicate plain Space is
                # gated on below — see _toggle_chat_selection().
                was_active = self._chat_selection_visible()
                self.conversations_list.Focus(target)
                self.conversations_list.Select(target, True)
                self.conversations_list.EnsureVisible(target)
                if self._select_chat_at(target):
                    self.main_window.add_chats_to_ui()
                    self.selection_sound.play()
                    self.main_window.output(
                        self._selection_mode_announcement(
                            self.main_window.i18n.t("selected"),
                            was_active, self._chat_selection_visible()),
                        interrupt=True)
            return

        if shift and key in (wx.WXK_UP, wx.WXK_NUMPAD_UP):
            target = (idx - 1) if idx >= 0 else 0
            if target >= 0:
                was_active = self._chat_selection_visible()
                self.conversations_list.Focus(target)
                self.conversations_list.Select(target, True)
                self.conversations_list.EnsureVisible(target)
                if self._select_chat_at(target):
                    self.main_window.add_chats_to_ui()
                    self.selection_sound.play()
                    self.main_window.output(
                        self._selection_mode_announcement(
                            self.main_window.i18n.t("selected"),
                            was_active, self._chat_selection_visible()),
                        interrupt=True)
            return

        if shift and key in (wx.WXK_HOME, wx.WXK_NUMPAD_HOME, wx.WXK_END, wx.WXK_NUMPAD_END):
            to_end = key in (wx.WXK_END, wx.WXK_NUMPAD_END)
            if total > 0:
                idx0 = idx if idx >= 0 else 0
                lo, hi = (idx0, total - 1) if to_end else (0, idx0)
                was_active = self._chat_selection_visible()
                selected_any = False
                for i in range(lo, hi + 1):
                    if self._select_chat_at(i):
                        selected_any = True
                target = total - 1 if to_end else 0
                self.conversations_list.Focus(target)
                self.conversations_list.Select(target, True)
                self.conversations_list.EnsureVisible(target)
                if selected_any:
                    self.main_window.add_chats_to_ui()
                    self.selection_sound.play()
                    self.main_window.output(
                        self._selection_mode_announcement(
                            self.main_window.i18n.t("selected"),
                            was_active, self._chat_selection_visible()),
                        interrupt=True)
            return

        if ctrl and shift and key == wx.WXK_SPACE:
            all_jids = self._all_chat_jids()
            was_active = self._chat_selection_visible()
            if all_jids and all(j in self.selected_chats for j in all_jids):
                self.selected_chats.clear()
                self.main_window.add_chats_to_ui()
                self.main_window.output(
                    self._selection_mode_announcement(
                        self.main_window.i18n.t("all_unselected"),
                        was_active, self._chat_selection_visible()),
                    interrupt=True)
            elif all_jids:
                self.selected_chats.update(all_jids)
                self.main_window.add_chats_to_ui()
                self.selection_sound.play()
                self.main_window.output(
                    self._selection_mode_announcement(
                        self.main_window.i18n.t("all_selected"),
                        was_active, self._chat_selection_visible()),
                    interrupt=True)
            return

        # Plain Space keeps selecting once a selection exists (issue #99).
        # With nothing selected it has no meaning here, so it keeps falling
        # through to the native control exactly as before — and so it does when
        # the toggle itself refuses (no focused row, or a row with no jid),
        # rather than swallowing the key with nothing to show for it.
        if (key == wx.WXK_SPACE and not ctrl and not shift
                and self._selection_mode_enabled() and self._chat_selection_visible()):
            if self._toggle_chat_selection(idx):
                return
            event.Skip()
            return

        if ctrl and not shift and key == wx.WXK_SPACE:
            self._toggle_chat_selection(idx)
        elif key in (wx.WXK_PAGEUP, wx.WXK_NUMPAD_PAGEUP):
            self._jump_list_by(self.conversations_list, -self._page_jump_size())
        elif key in (wx.WXK_PAGEDOWN, wx.WXK_NUMPAD_PAGEDOWN):
            self._jump_list_by(self.conversations_list, self._page_jump_size())
        elif ctrl and key == ord("P"):
            idx = self.conversations_list.GetFocusedItem()
            if 0 <= idx < len(self.chats_list):
                jid = self.chats_list[idx].get("remoteJid", "")
                if jid:
                    if self.main_window.is_chat_pinned(jid):
                        self._on_menu_unpin(jid)
                    else:
                        self._on_menu_pin(jid)
        # Ctrl+Shift+Q, not plain Ctrl+Q — archiving isn't reversible from a
        # single accidental keystroke the way pinning is, and plain Ctrl+Q
        # sits right next to other single-Ctrl combos a user can easily
        # fat-finger while just navigating the list.
        elif ctrl and shift and key == ord("Q"):
            idx = self.conversations_list.GetFocusedItem()
            if 0 <= idx < len(self.chats_list):
                jid = self.chats_list[idx].get("remoteJid", "")
                if jid:
                    if self.main_window.is_chat_archived(jid):
                        self._on_menu_unarchive(jid)
                    else:
                        self._on_menu_archive(jid)
        else:
            event.Skip()

    def _selected_chat_from_list(self):
        selected = self.conversations_list.GetFirstSelected()
        if selected < 0:
            selected = self.conversations_list.GetFocusedItem()
        if 0 <= selected < len(self.chats_list):
            return self.chats_list[selected]
        return None

    def _on_accel_conversation_data_list(self, event):
        chat = self._selected_chat_from_list()
        if chat:
            self._show_conversation_data(chat=chat)

    def _on_accel_toggle_read_list(self, event):
        if self._bulk_shortcuts_enabled() and self.selected_chats:
            first_jid = next(iter(self.selected_chats))
            first_chat = next(
                (c for c in self.chats_list if c.get("remoteJid", "") == first_jid), None
            )
            if first_chat and int(first_chat.get("unreadCount") or 0) > 0:
                self._on_mass_mark_read_chats(event)
            else:
                self._on_mass_mark_unread_chats(event)
            return
        chat = self._selected_chat_from_list()
        if not chat:
            return
        jid = chat.get("remoteJid", "")
        if not jid:
            return
        if int(chat.get("unreadCount") or 0) > 0:
            self._on_menu_mark_read(jid)
        else:
            self._on_menu_mark_unread(jid)

    def _on_accel_mute_list(self, event):
        chat = self._selected_chat_from_list()
        if not chat:
            return
        jid = chat.get("remoteJid", "")
        if not jid:
            return
        self._popup_mute_menu(jid, self.conversations_list)

    def _on_accel_block_list(self, event):
        chat = self._selected_chat_from_list()
        if not chat:
            return
        jid = chat.get("remoteJid", "")
        if not jid or jid.endswith("@g.us") or self.main_window._is_self_jid(jid):
            return
        self._on_menu_block(chat, jid, self.main_window.is_contact_blocked(jid))

    def _on_accel_clear_list(self, event):
        if self._bulk_shortcuts_enabled() and self.selected_chats:
            self._on_mass_clear_chats(event)
            return
        chat = self._selected_chat_from_list()
        if chat:
            jid = chat.get("remoteJid", "")
            if jid:
                self._on_menu_clear_chat(jid)

    def _on_accel_archive_list(self, event):
        # No bulk "unarchive" action exists in the mass-actions submenu, so
        # the shortcut always archives when a selection exists — matching
        # what "Ações em massa > Arquivar conversas selecionadas" does.
        if self._bulk_shortcuts_enabled() and self.selected_chats:
            self._on_mass_archive_chats(event)
            return
        chat = self._selected_chat_from_list()
        if not chat:
            return
        jid = chat.get("remoteJid", "")
        if not jid:
            return
        if self.main_window.is_chat_archived(jid):
            self._on_menu_unarchive(jid)
        else:
            self._on_menu_archive(jid)

    def _on_accel_lock_list(self, event):
        """Ctrl+Shift+T: lock the focused conversation (the context menu's
        "Lock chat"; sets the vault up first if it was never configured)."""
        chat = self._selected_chat_from_list()
        jid = chat.get("remoteJid", "") if chat else ""
        if jid:
            self.main_window.lock_chat(jid)

    def _on_accel_pin_list(self, event):
        """Play/stop the recorded-audio preview while the voice recording is
        paused; otherwise pin/unpin the focused conversation. Both share
        this one accelerator (Ctrl+P) — mutually exclusive contexts, same
        pattern as _on_ctrl_shift_p uses for Ctrl+Shift+P."""
        if self._is_recording and self._recording_paused:
            self._toggle_play_recorded_audio(event)
            return
        chat = self._selected_chat_from_list()
        if not chat:
            return
        jid = chat.get("remoteJid", "")
        if not jid:
            return
        if self.main_window.is_chat_pinned(jid):
            self._on_menu_unpin(jid)
        else:
            self._on_menu_pin(jid)

    def _run_bulk_chat_action(self, handler, event):
        """Chat-list twin of _run_bulk_message_action(): the dedicated
        mass-action shortcuts below are inert without a selection, mirroring
        how the chat list's "Ações em massa" submenu isn't built until
        conversations are selected — and announce that rather than doing
        nothing at all, which reads as a broken shortcut to a screen-reader
        user."""
        if not self.selected_chats:
            self.main_window.output(
                self.main_window.i18n.t("bulk_no_chat_selection"), interrupt=True
            )
            return
        handler(event)

    def _on_accel_bulk_clear_chats(self, event):
        """Ctrl+Alt+Shift+L: clear every selected conversation."""
        self._run_bulk_chat_action(self._on_mass_clear_chats, event)

    def _on_accel_bulk_delete_chats(self, event):
        """Ctrl+Shift+Delete: delete every selected conversation."""
        self._run_bulk_chat_action(self._on_mass_delete_chats, event)

    def _on_accel_bulk_archive_chats(self, event):
        """Ctrl+Alt+Shift+A: archive every selected conversation."""
        self._run_bulk_chat_action(self._on_mass_archive_chats, event)

    def _on_accel_bulk_read_chats(self, event):
        """Ctrl+Alt+Shift+R: mark every selected conversation as read."""
        self._run_bulk_chat_action(self._on_mass_mark_read_chats, event)

    def _on_accel_bulk_unread_chats(self, event):
        """Ctrl+Alt+Shift+U: mark every selected conversation as unread."""
        self._run_bulk_chat_action(self._on_mass_mark_unread_chats, event)


    # ── Mass action handlers ────────────────────────────────────────────────
    # Act on the Space-toggled selections (self.selected_chats /
    # self.selected_messages), reached from the "mass actions" submenu both
    # context menus grow while a selection exists.

    def _on_mass_clear_chats(self, event):
        i18n = self.main_window.i18n
        if not self.selected_chats: return
        count = len(self.selected_chats)
        confirmed, keep_starred = confirm_clear_chat(
            self,
            i18n.t("clear_confirm_msg_bulk").format(count=count),
            i18n.t("clear_chat_bulk_title"),
            i18n.t("clear_chat_keep_starred"),
            yes_label=i18n.t("yes_button"),
            no_label=i18n.t("no_button"),
        )
        if not confirmed:
            return
        for jid in list(self.selected_chats):
            self.main_window.clear_chat(jid, keep_starred=keep_starred)
            self._reset_view_after_chat_cleared(jid)
        self.selected_chats.clear()
        self.main_window.add_chats_to_ui()
        self.main_window.output(i18n.t("success_clear"), interrupt=True)

    def _on_mass_delete_chats(self, event):
        i18n = self.main_window.i18n
        if not self.selected_chats: return
        count = len(self.selected_chats)
        if wx.MessageBox(
            i18n.t("delete_confirm_msg_bulk").format(count=count),
            i18n.t("delete_chat_bulk_title"),
            wx.YES_NO | wx.ICON_QUESTION, self,
        ) != wx.YES:
            return
        for jid in list(self.selected_chats):
            self.main_window.delete_chat(jid)
        self.selected_chats.clear()
        self.main_window.add_chats_to_ui()
        self.main_window.output(i18n.t("success_delete"), interrupt=True)

    def _on_mass_archive_chats(self, event):
        i18n = self.main_window.i18n
        if not self.selected_chats: return
        for jid in list(self.selected_chats):
            self.main_window.archive_chat(jid, True)
        self.selected_chats.clear()
        self.main_window.add_chats_to_ui()
        self.main_window.output(i18n.t("success_archive"), interrupt=True)

    def _on_mass_mark_read_chats(self, event):
        if not self.selected_chats: return
        # One paced batch, not one /send-seen per chat at once — see
        # MainWindow.mark_conversations_as_read().
        self.main_window.mark_conversations_as_read(list(self.selected_chats), force=True)
        self.selected_chats.clear()
        self.main_window.add_chats_to_ui()

    def _on_mass_mark_unread_chats(self, event):
        if not self.selected_chats: return
        for jid in list(self.selected_chats):
            self.main_window.mark_conversation_as_unread(jid)
        self.selected_chats.clear()
        self.main_window.add_chats_to_ui()
