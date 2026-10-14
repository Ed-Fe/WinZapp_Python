"""
WinZapp - Usage guide window
============================
Shows the styled HTML guide (client/data/help_guide/<lang>.html) inside the
app, in an Edge/WebView2 control, instead of handing it to the default browser.

The page is the same one the browser used to open - sidebar navigation, search
and dark mode all live in its own HTML/JS - so nothing about the guide changes,
only where it is displayed. Edge is Chromium, so a screen reader reads it the
way it reads any web page (browse mode); that is why this is a WebView and not
a wx.html.HtmlWindow, which exposes almost nothing to the accessibility layer.

WebView2 is a runtime, not something WinZapp ships: `show_help_guide()` returns
False when it is missing (or anything about creating the control fails) and the
caller falls back to opening the file in the browser, exactly as before.
"""

import logging
import os
from pathlib import Path

import wx

from app_paths import resource_path

_FALLBACK_LANGUAGE = "pt-BR"


def find_help_guide_path(language, path_for=resource_path, exists=os.path.isfile):
    """Return the guide file for `language`, falling back to pt-BR, or None."""
    for candidate in (language, _FALLBACK_LANGUAGE):
        path = path_for("data", "help_guide", f"{candidate}.html")
        if exists(path):
            return path
    return None


def is_external_url(url):
    """True for a link that leaves the guide and belongs in the real browser.

    The guide is a local file whose internal links are `#anchor`s and the odd
    `about:blank`; anything on the web must not replace the guide inside a
    window with no address bar and no way back to the app's own content.
    """
    lowered = (url or "").strip().lower()
    return lowered.startswith(("http://", "https://", "mailto:"))


def edge_webview_available():
    """Whether the Edge/WebView2 backend can be created on this machine."""
    try:
        import wx.html2 as html2
        return html2.WebView.IsBackendAvailable(html2.WebViewBackendEdge)
    except Exception:
        logging.exception("[help_guide] WebView2 availability check failed")
        return False


class HelpGuideDialog(wx.Dialog):
    """Modal window holding the usage guide. Esc or the Close button dismiss it."""

    def __init__(self, parent, i18n, guide_path):
        import wx.html2 as html2

        super().__init__(
            parent,
            title=i18n.t("menu_help_guide").replace("&", ""),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX,
        )
        self._html2 = html2
        self._focused_after_load = False

        panel = wx.Panel(self)
        self._web = html2.WebView.New(panel, backend=html2.WebViewBackendEdge)
        if self._web is None:
            raise RuntimeError("WebView2 control could not be created")
        # A right-click menu offers Back/Reload/Inspect on a page that is meant
        # to behave like part of the app, not like a browser tab.
        self._web.EnableContextMenu(False)

        close_btn = wx.Button(panel, wx.ID_CANCEL, i18n.t("close"))
        close_btn.SetDefault()

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self._web, 1, wx.EXPAND)
        sizer.Add(close_btn, 0, wx.ALIGN_RIGHT | wx.ALL, 8)
        panel.SetSizer(sizer)

        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(outer)

        width, height = wx.GetDisplaySize()
        self.SetSize((int(width * 0.8), int(height * 0.85)))
        self.CenterOnParent()

        self.Bind(html2.EVT_WEBVIEW_NAVIGATING, self._on_navigating, self._web)
        self.Bind(html2.EVT_WEBVIEW_LOADED, self._on_loaded, self._web)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)

        self._web.LoadURL(Path(guide_path).resolve().as_uri())

    def _on_navigating(self, event):
        url = event.GetURL()
        if is_external_url(url):
            event.Veto()
            wx.LaunchDefaultBrowser(url)
            return
        event.Skip()

    def _on_loaded(self, event):
        # Put focus on the page once, when it first finishes loading, so the
        # screen reader lands in the document and starts reading it. Doing it
        # on every load would steal focus back from the guide's own search box.
        if not self._focused_after_load:
            self._focused_after_load = True
            self._web.SetFocus()
        event.Skip()

    def _on_char_hook(self, event):
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CANCEL)
            return
        event.Skip()


def show_help_guide(parent, i18n, guide_path):
    """Show the guide in-app. Returns False when the caller should use the browser."""
    if not edge_webview_available():
        logging.warning("[help_guide] WebView2 unavailable; falling back to browser.")
        return False
    try:
        dlg = HelpGuideDialog(parent, i18n, guide_path)
    except Exception:
        logging.exception("[help_guide] Could not build in-app guide; falling back.")
        return False
    try:
        dlg.ShowModal()
    finally:
        dlg.Destroy()
    return True


def open_help_guide(parent, i18n):
    """Help > Usage guide: the guide in the user's language, or pt-BR.

    Opens in the app's own window; when WebView2 is missing, in the default
    browser. Does nothing (and logs) when no guide file ships at all.
    """
    path = find_help_guide_path(i18n.language)
    if path is None:
        logging.warning(
            "[help_guide] No guide HTML found for %s or %s.",
            i18n.language, _FALLBACK_LANGUAGE,
        )
        return
    if not show_help_guide(parent, i18n, path):
        os.startfile(path)
