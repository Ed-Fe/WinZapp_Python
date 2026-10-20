"""Settings > Advanced: options most people never need, kept out of their way.

* the log level of log.log (core/log_level.py), applied at once;
* a fixed port for the bundled API (core/local_api_port.py), from the next
  start;
* showing every WPPConnect Server release, not only the approved ones
  (core/wpp_upstream.py). Install-wide, like the other update switches, so
  SettingsDialog loads and writes it with them (_load_install_wide_values(),
  _apply_install_wide_values()); this page only builds the box.
"""

import wx

from core import local_api_port, log_level

#: The label key of each level, in core.log_level.LEVELS order.
LEVEL_LABEL_KEYS = {
    "debug": "advanced_log_level_debug",
    "info": "advanced_log_level_info",
    "warning": "advanced_log_level_warning",
    "error": "advanced_log_level_error",
}


def level_labels(t) -> list:
    """The Choice's items, translated with *t*."""
    return [t(LEVEL_LABEL_KEYS[name]) for name in log_level.LEVELS]


def level_index(settings) -> int:
    """The Choice selection for the configured level."""
    return log_level.LEVELS.index(log_level.level_name(settings))


class AdvancedTabMixin:
    """The Advanced page of `SettingsDialog`."""

    def _build_advanced_page(self) -> None:
        i18n = self.main_window.i18n
        self._advanced_page = wx.Panel(self._notebook)
        sizer = wx.BoxSizer(wx.VERTICAL)

        self._log_level_label = wx.StaticText(
            self._advanced_page, label=i18n.t("advanced_log_level_label"))
        sizer.Add(self._log_level_label, 0, wx.LEFT | wx.TOP | wx.RIGHT, 8)
        self._log_level_choice = wx.Choice(
            self._advanced_page, choices=level_labels(i18n.t))
        sizer.Add(self._log_level_choice, 0, wx.ALL, 8)

        self._fixed_port_check = wx.CheckBox(
            self._advanced_page, label=i18n.t("advanced_fixed_port_check"))
        sizer.Add(self._fixed_port_check, 0, wx.ALL, 8)
        self._fixed_port_label = wx.StaticText(
            self._advanced_page, label=i18n.t("advanced_fixed_port_label"))
        sizer.Add(self._fixed_port_label, 0, wx.LEFT | wx.TOP | wx.RIGHT, 8)
        self._fixed_port_field = wx.TextCtrl(self._advanced_page, style=wx.TE_DONTWRAP)
        sizer.Add(self._fixed_port_field, 0, wx.EXPAND | wx.ALL, 8)
        self._fixed_port_check.Bind(
            wx.EVT_CHECKBOX, lambda event: (self._sync_fixed_port_field(), event.Skip()))

        self._wpp_all_updates_check = wx.CheckBox(
            self._advanced_page, label=i18n.t("advanced_wpp_all_updates_check"))
        sizer.Add(self._wpp_all_updates_check, 0, wx.ALL, 8)

        self._advanced_page.SetSizer(sizer)
        self._notebook.AddPage(self._advanced_page, i18n.t("tab_advanced"))

    def _sync_fixed_port_field(self) -> None:
        """The fixed port is the bundled API's: with the custom API on (the
        Connection page) it does nothing, so both controls are off."""
        custom = getattr(self, "_custom_api_check", None)
        bundled = not (custom is not None and custom.GetValue())
        self._fixed_port_check.Enable(bundled)
        self._fixed_port_field.Enable(bundled and self._fixed_port_check.GetValue())

    def _load_advanced_values(self) -> None:
        settings = self.main_window.settings
        self._log_level_choice.SetSelection(level_index(settings))
        port = local_api_port.fixed_port(settings)
        self._fixed_port_check.SetValue(port is not None)
        self._fixed_port_field.SetValue(str(port) if port is not None else "")
        self._sync_fixed_port_field()

    def _validate_advanced_values(self) -> bool:
        """A ticked fixed port must be a usable one; the field is focused and
        the reason said otherwise."""
        if not self._fixed_port_check.GetValue() or not self._fixed_port_check.IsEnabled():
            # Unticked, or off because the custom API is on: nothing to check.
            return True
        if local_api_port.parse_port(self._fixed_port_field.GetValue()) is not None:
            return True
        i18n = self.main_window.i18n
        wx.MessageBox(
            i18n.t("invalid_port"),
            i18n.t("error").format(app_name=self.main_window.app_name),
            wx.OK | wx.ICON_ERROR, self)
        self._notebook.SetSelection(self._notebook.FindPage(self._advanced_page))
        self._fixed_port_field.SetFocus()
        return False

    def _apply_advanced_values(self) -> None:
        """Save the per-account half of the page and apply the log level now."""
        section = self.main_window.settings.setdefault(log_level.SECTION, {})
        index = self._log_level_choice.GetSelection()
        section[log_level.KEY] = (
            log_level.LEVELS[index] if 0 <= index < len(log_level.LEVELS) else log_level.DEFAULT)
        port = (local_api_port.parse_port(self._fixed_port_field.GetValue())
                if self._fixed_port_check.GetValue() else None)
        self.main_window.settings.setdefault(local_api_port.SECTION, {})[
            local_api_port.KEY] = port or 0
        log_level.apply_log_level(self.main_window.settings)

    def _refresh_advanced_labels(self) -> None:
        i18n = self.main_window.i18n
        self._notebook.SetPageText(
            self._notebook.FindPage(self._advanced_page), i18n.t("tab_advanced"))
        self._log_level_label.SetLabel(i18n.t("advanced_log_level_label"))
        for index, label in enumerate(level_labels(i18n.t)):
            self._log_level_choice.SetString(index, label)
        self._fixed_port_check.SetLabel(i18n.t("advanced_fixed_port_check"))
        self._fixed_port_label.SetLabel(i18n.t("advanced_fixed_port_label"))
        self._wpp_all_updates_check.SetLabel(i18n.t("advanced_wpp_all_updates_check"))
