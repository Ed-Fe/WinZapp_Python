"""The provider window's model note must be reachable with Tab.

It was a StaticText: Tab skips one, so a screen reader user moving from the
buttons to the model checkbox never heard that Mistral's audio and PDFs do not
use the model chosen there. Real native controls: CI only, never on an NVDA
user's desktop.
"""
import pytest
import wx

from app_settings import AppSettings
from core.ai_credentials import CredentialStore
from core.ai_media import config as ai_config
from core.i18n import I18n
from tests.conftest import hidden_frame, destroy_now
from ui.dialogs.ai_settings_page import AIProviderDialog

pytestmark = pytest.mark.wxgui


@pytest.fixture
def context(wx_app, tmp_path, monkeypatch):
    frame = hidden_frame()
    frame.settings = {"general": {"language": "en-US"}}
    frame.i18n = I18n(frame)
    frame._app_settings = AppSettings(str(tmp_path))
    monkeypatch.setattr("ui.dialogs.ai_settings_page.global_dir", lambda: str(tmp_path))
    yield frame, tmp_path
    destroy_now(frame)


def provider_window(frame, path, provider):
    values = {"key": "", "deleted": False, "model": ai_config.PROVIDERS[provider].model, "enabled": True}
    return AIProviderDialog(frame, frame, provider, values, CredentialStore(path), False)


def test_the_note_is_a_read_only_field_tab_reaches_before_the_model_checkbox(context):
    frame, path = context
    window = provider_window(frame, path, "mistral")
    try:
        note = window.model_note
        assert isinstance(note, wx.TextCtrl)
        assert note.GetValue() == frame.i18n.t(ai_config.PROVIDERS["mistral"].model_note)
        assert note.GetWindowStyleFlag() & wx.TE_READONLY
        assert note.AcceptsFocusFromKeyboard()
        assert note.GetNextSibling() is window.automatic
    finally:
        window.Destroy()


def test_a_provider_without_a_note_gets_no_field(context):
    frame, path = context
    window = provider_window(frame, path, "openai")
    try:
        assert window.model_note is None
    finally:
        window.Destroy()
