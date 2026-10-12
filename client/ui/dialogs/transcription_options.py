"""The two on/off options at the end of the Local Transcription tab's language
block: keep the model loaded in memory, and transcribe received voice messages
on their own.

Kept out of transcription_tab.py, which is far past the size a mixin should
have. Each box mirrors one key of settings.json's "transcription" section, and
is written the way tests/test_settings_checkboxes_wiring.py can follow (a
literal section and key on both the load and the save side).
"""

import wx

from core.transcription import residency


class TranscriptionOptionsMixin:
    """`keep_model_loaded` and `auto_transcribe_received` for `SettingsDialog`."""

    def _build_transcription_options(self, page, sizer) -> None:
        i18n = self.main_window.i18n
        self._transcription_keep_loaded_check = wx.CheckBox(
            page, label=i18n.t("transcription_keep_loaded_check")
        )
        sizer.Add(self._transcription_keep_loaded_check, 0, wx.ALL, 8)
        self._transcription_auto_check = wx.CheckBox(
            page, label=i18n.t("transcription_auto_transcribe_check")
        )
        sizer.Add(self._transcription_auto_check, 0, wx.ALL, 8)

    def _load_transcription_options(self) -> None:
        section = self.main_window.settings.get("transcription", {})
        self._transcription_keep_loaded_check.SetValue(
            bool(section.get("keep_model_loaded", False))
        )
        self._transcription_auto_check.SetValue(
            bool(section.get("auto_transcribe_received", False))
        )

    def _apply_transcription_options(self) -> None:
        section = self.main_window.settings.setdefault("transcription", {})
        section["keep_model_loaded"] = self._transcription_keep_loaded_check.GetValue()
        section["auto_transcribe_received"] = self._transcription_auto_check.GetValue()
        # Takes effect now, not at the next launch: turning it on cancels the
        # pending release, turning it off starts the idle clock.
        residency.residency.configure(residency.keep_loaded_from_settings(
            self.main_window.settings
        ))

    def _refresh_transcription_options_labels(self) -> None:
        i18n = self.main_window.i18n
        self._transcription_keep_loaded_check.SetLabel(
            i18n.t("transcription_keep_loaded_check")
        )
        self._transcription_auto_check.SetLabel(
            i18n.t("transcription_auto_transcribe_check")
        )
