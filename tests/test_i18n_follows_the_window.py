"""Every I18n instance speaks the language the window shows.

The language setting is install-wide: another account changing it reaches this
window's settings copy on its next save, while the window keeps the language it
shows until the user applies one (MainWindow._apply_pulled_global_settings()).
The tray, the notification manager, the WebSocket client and a few dialogs keep
I18n instances of their own and refresh them with get_language(), which read
the settings -- so they switched by themselves: toasts and tray menus in the
new language over a window still in the old one. The updater did the same on
the window's own instance, switching every string drawn after it.
"""

import inspect

import updater
from core.i18n import I18n


class _Window:
    def __init__(self, language):
        self.settings = {"general": {"language": language}}


def _window_showing(language):
    window = _Window(language)
    window.i18n = I18n(window)
    window.i18n.get_language()
    return window


class TestAHelperFollowsTheWindow:
    def test_not_the_settings_another_account_changed(self):
        window = _window_showing("pt-BR")
        window.settings["general"]["language"] = "en-US"  # pulled

        helper = I18n(window)

        assert helper.get_language() == "pt-BR"
        assert helper.language == "pt-BR"

    def test_and_moves_when_the_window_applies_a_language(self):
        window = _window_showing("pt-BR")
        helper = I18n(window)
        helper.get_language()

        window.settings["general"]["language"] = "es-ES"
        window.i18n.get_language()  # the Settings dialog's OK, an import

        assert helper.get_language() == "es-ES"

    def test_before_the_window_has_one_it_reads_the_settings(self):
        """Early startup: Connect is built before MainWindow.i18n exists."""
        window = _Window("pl")

        assert I18n(window).get_language() == "pl"


class TestTheWindowsOwnInstance:
    def test_reads_the_settings_when_asked(self):
        window = _window_showing("pt-BR")
        window.settings["general"]["language"] = "en-US"

        assert window.i18n.get_language() == "en-US"

    def test_the_updater_does_not_ask_it_behind_the_windows_back(self):
        source = inspect.getsource(updater)
        assert "i18n.get_language()" not in source
