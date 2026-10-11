"""Clearing the saved transcriptions of one model, from the Transcription tab.

A transcription is kept per model (core/transcription/stored.py): changing the
model asks for a new one, and the old model's text stays for the day the user
goes back to it. This button deletes the text a model made — in every chat — for
the user who wants it gone, or the disk space back. Other models' results are
untouched.

It is not one of the tab's job-driven actions: nothing is downloaded and no
progress dialog is needed, only a confirmation, the database write (done by the
main window, on its transcription write queue) and one spoken sentence. It
sits under the model's action group as a button of its own because a group
button is one word, and no one word says what this one deletes.
"""

import wx

from core.transcription import model_names
from core.transcription import preferences as transcription_preferences
from core.transcription import stored as stored_transcription


class TranscriptionCacheMixin:
    """The "clear this model's saved transcriptions" button, for `SettingsDialog`."""

    def _build_transcription_clear_cache_button(self, page, sizer) -> None:
        i18n = self.main_window.i18n
        self._transcription_clear_cache_btn = wx.Button(page, label=i18n.t("transcription_clear_cache_btn"))
        self._transcription_clear_cache_btn.Bind(
            wx.EVT_BUTTON, self._on_transcription_clear_cache
        )
        sizer.Add(self._transcription_clear_cache_btn, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self._sync_transcription_clear_cache_button()

    def _refresh_transcription_clear_cache_label(self) -> None:
        if getattr(self, "_transcription_clear_cache_btn", None) is None:
            return
        self._transcription_clear_cache_btn.SetLabel(
            self.main_window.i18n.t("transcription_clear_cache_btn")
        )

    def _transcription_clear_cache_target(self):
        """The model the button acts on, or None: "Automatic" names no model."""
        model_id = self._selected_transcription_model()
        if model_id is None or model_id == transcription_preferences.AUTO:
            return None
        return model_id

    def _sync_transcription_clear_cache_button(self) -> None:
        button = getattr(self, "_transcription_clear_cache_btn", None)
        if button is None:
            return
        button.Enable(
            not self._transcription_job_running
            and self._transcription_clear_cache_target() is not None
        )

    def _on_transcription_clear_cache(self, event) -> None:
        model_id = self._transcription_clear_cache_target()
        if model_id is None or self._transcription_job_running:
            return
        i18n = self.main_window.i18n
        text = i18n.t("transcription_clear_cache_confirm").format(
            model=model_names.display_name(i18n, model_id) or ""
        )
        if wx.MessageBox(
                text, i18n.t("settings_title"), wx.YES_NO | wx.ICON_QUESTION, self
        ) != wx.YES:
            return
        backend_id = self._selected_transcription_backend()
        button = self._transcription_clear_cache_btn
        # One clear at a time, and the button says so while the database works.
        button.Disable()

        def _done(count):
            # Spoken even if Settings was closed meanwhile: the database has
            # answered, and it is still true and useful.
            if self:
                self._sync_transcription_clear_cache_button()
            self.main_window.speak_output.output(
                i18n.t(stored_transcription.clear_cache_message_key(count)).format(
                    count=count or 0), interrupt=True)

        self.main_window.clear_model_transcriptions(model_id, backend_id, _done)
