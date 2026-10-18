"""UnsentStatusMixin — part of StatusPanel (see status_tab/__init__.py).

A status post that failed, or that WinZapp could not confirm in time
(status_rules.STATUS_POST_UNCONFIRMED: it may have gone out), used to leave
nothing behind but a message box. It now stays in the Status list, right
after "My status", as one row per post, with a context menu and a key
(Ctrl+Shift+Y, configurable as status.ID_CTRL_SHIFT_Y) to try sending it
again, and the menu's Discard to drop it. Never sent again by itself: an
unconfirmed one may already be up, so trying again is the user's call.

The rows live in memory only (``_unsent_status_posts``): the file a media post
was made from is still on disk, but nothing about a failed post is worth
keeping across a restart.
"""

import os
import threading
import uuid

import wx

from ui.shortcut_bindings import refresh_popup_shortcuts

FAILED = "failed"
UNCONFIRMED = "unconfirmed"
TEXT = "text"
MEDIA = "media"


def unsent_post(kind, state, text="", path="", caption=""):
    """One unsent post, as the list keeps it."""
    return {"id": str(uuid.uuid4()), "kind": kind, "state": state,
            "text": text or "", "path": path or "", "caption": caption or ""}


def unsent_row_label(post, i18n) -> str:
    """"Status not sent: video clip.mp4, failed" — what, and how it ended.
    The text of a text status is not read out: it may be long, and the user
    wrote it a moment ago."""
    what = (os.path.basename(post["path"]) if post["kind"] == MEDIA
            else i18n.t("status_unsent_text"))
    state = i18n.t("status_unsent_unconfirmed" if post["state"] == UNCONFIRMED
                   else "status_unsent_failed")
    return i18n.t("status_unsent_row").format(what=what, state=state)


def can_retry(post, file_exists=os.path.isfile) -> bool:
    """A text always; a media post while its file is still there."""
    if post.get("kind") == TEXT:
        return bool(post.get("text"))
    return bool(post.get("path")) and file_exists(post["path"])


class UnsentStatusMixin:
    """The Status list's rows of posts not sent, and trying them again."""

    # ── Recorded by the post paths (status_composer.py), on the wx thread ──

    def _remember_unsent_status(self, post):
        posts = getattr(self, "_unsent_status_posts", None)
        if posts is None:
            posts = self._unsent_status_posts = []
        posts.append(post)
        self._redraw_unsent_rows()

    def _unsent_rows(self, i18n):
        """(label, id) of every unsent post, for StatusListMixin._populate_list()."""
        return [(unsent_row_label(post, i18n), post["id"])
                for post in getattr(self, "_unsent_status_posts", ())]

    def _unsent_post_at(self, row):
        post_id = getattr(self, "_status_row_unsent", {}).get(row)
        return next((post for post in getattr(self, "_unsent_status_posts", ())
                     if post["id"] == post_id), None)

    def _redraw_unsent_rows(self):
        """Rebuild the list with the rows as they are now, focus kept on the
        row it was on (or the last one, when that row went)."""
        if getattr(self, "_list_is_loading", False) or not hasattr(self, "_status_contacts"):
            return
        row = self._status_list.GetFocusedItem()
        self._populate_list(self._my_statuses, self._status_contacts)
        count = self._status_list.GetItemCount()
        if count and row > 0:
            row = min(row, count - 1)
            self._status_list.Focus(row)
            self._status_list.Select(row)

    # ── The menu and the key ──────────────────────────────────────────────

    def _on_status_list_context_menu(self, event):
        post = self._unsent_post_at(self._status_list.GetFocusedItem())
        if post is None:
            event.Skip()
            return
        i18n = self.main_window.i18n
        menu = wx.Menu()
        retry = menu.Append(wx.ID_ANY, f"{i18n.t('status_retry_post')}\tCtrl+Shift+Y")
        retry.Enable(can_retry(post))
        discard = menu.Append(wx.ID_ANY, i18n.t("status_discard_unsent"))
        self.Bind(wx.EVT_MENU, lambda e, p=post["id"]: self._retry_unsent_status(p), retry)
        self.Bind(wx.EVT_MENU, lambda e, p=post["id"]: self._discard_unsent_status(p), discard)
        refresh_popup_shortcuts(self, 'status', menu)
        self.PopupMenu(menu)
        menu.Destroy()

    def _on_accel_retry_status_post(self, event=None):
        post = self._unsent_post_at(self._status_list.GetFocusedItem())
        if post is None:
            self.main_window.output(self.main_window.i18n.t("status_retry_not_available"),
                                    interrupt=True)
            return
        self._retry_unsent_status(post["id"])

    # ── Trying again, discarding ──────────────────────────────────────────

    def _retry_unsent_status(self, post_id):
        post = next((p for p in getattr(self, "_unsent_status_posts", ()) if p["id"] == post_id), None)
        if post is None:
            return
        if not can_retry(post):
            self.main_window.output(self.main_window.i18n.t("status_retry_file_missing"),
                                    interrupt=True)
            return
        # One post at a time, the same guard as Post (status_composer.py).
        if not self._begin_status_post():
            return
        # Out of the list now: a new failure puts it back as a new row.
        self._unsent_status_posts.remove(post)
        self._redraw_unsent_rows()
        if post["kind"] == TEXT:
            target, args = self._send_text_status_bg, (post["text"],)
        else:
            target, args = self._send_all_media_statuses_bg, ([post["path"]], post["caption"])
        threading.Thread(target=target, args=args, daemon=True).start()

    def _discard_unsent_status(self, post_id):
        posts = getattr(self, "_unsent_status_posts", [])
        self._unsent_status_posts = [p for p in posts if p["id"] != post_id]
        self._redraw_unsent_rows()
        self.main_window.output(self.main_window.i18n.t("status_unsent_discarded"), interrupt=True)
