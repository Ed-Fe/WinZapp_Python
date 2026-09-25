"""transcription_flow.py — transcribing one message, from the keypress to the text.

`ConversationsPanel` only wires this in (a context-menu item and Alt+Shift+T);
everything between "the user asked" and "the text is in front of them" is
here, and everything slow is further down, in `core.transcription.message_run`,
which runs on a worker thread behind the progress dialog part 5c-2 built.

What the user hears, and what only appears. A finished transcription can carry
a headline and up to three caveats — about sixty words in a row, read out over
whatever the user does next. So:

* **Spoken**: the headline ("finished", "no speech was found", the error), and
  the voice-filter warning *always*. That warning is the one degradation a
  listener cannot detect — without the filter Whisper can end a note with a
  sentence nobody said, in the same voice as the real ones — so it may not
  depend on the user tabbing to it.
* **Shown only**: the language and confidence caveats. They sit in the result
  window's own read-only field, above the text, where Shift+Tab reaches them,
  and the spoken headline ends with a four-word pointer to them ("there are
  warnings above the text") when there are any — the pointer costs less
  attention than the notes themselves and still says they exist.

Each failure is reported exactly once. The media download goes through
`ConversationsPanel._download_media_to_disk()`, which says nothing, rather
than `_ensure_media_on_disk()`, which says it itself — with a message box
posted through `wx.CallAfter` that would surface on top of the modal progress
dialog, and a second sentence from here would then be the same failure told
twice. The sentences for "offline" and "the download failed" are the ones the
rest of the app already uses for media, chosen here from `run.media_status`.

Speech lands *after* the focus move it belongs to. Every outcome ends by
putting the focus somewhere — the message the user came from, the message
field, the result window — and a screen reader cancels its speech when the
focus changes. So the focus moves first, and the sentence follows through
`wx.CallAfter`; in the result window it is spoken from inside its own modal
loop for the same reason (see transcription_result.py).

The re-run on the processor is the one place a second progress dialog opens.
It is a new run the user explicitly agreed to after being told why the first
failed, not the same wait continuing, and the question has to be asked with no
progress dialog on screen — a Yes/No box on top of a modal whose only button
is Cancel would leave the user two dialogs deep in the middle of a run.

A finished transcription is kept with its message (part 7,
`core.transcription.stored`), and from then on the message offers it instead
of the wait: Alt+Shift+T and "Ver transcrição" open the same result window
with the stored text, saying when and with which model it was made, and
repeating every note that still applies — the voice-filter warning above all,
since a transcription made without the filter is exactly as untrustworthy the
tenth time it is read. "Transcrever novamente" is the run above; its result
replaces the stored one. "Apagar transcrição" asks first, because it throws
away minutes of work with one key. A run that finishes after the sender deleted
the message for everyone is neither kept nor shown: one sentence says so, and
no window opens (`WITHDRAWN_I18N_KEY`).

Nothing here logs the message, its id, the contact or a path: the same rule as
the whole transcription package, checked by `tests/test_transcription_flow.py`.
"""

import time
from datetime import datetime

import wx

from app_paths import data_path
from core.locale_format import get_datetime_format
from core.transcription import (
    audio_prep,
    errors,
    job as job_module,
    management,
    message_audio,
    message_run,
    narration,
    preferences,
    stored as stored_transcription,
)
from core.utils import is_phone_like
from ui.dialogs.transcription_progress import TranscriptionProgressDialog
from ui.dialogs.transcription_result import TranscriptionResultDialog, default_file_name

#: The phase this layer adds, in the same shape as narration.PHASE_I18N_KEYS.
_OWN_PHASE_I18N_KEYS = {
    message_run.PHASE_DOWNLOADING_MEDIA: "transcription_phase_downloading_audio",
}

#: The app's existing media-download sentences, reused rather than translated
#: again: "wait for the connection" and "the link may have expired" are what a
#: user already hears when opening or saving the same media.
_MEDIA_STATUS_I18N_KEYS = {
    message_run.MEDIA_OFFLINE: "media_download_offline",
    message_run.MEDIA_FAILED: "media_download_failed",
}

#: The failures whose answer lives on the Transcription tab — download a model,
#: pick another, repair the one that is there — and which are therefore
#: offered with a shortcut to it rather than only said.
_SETTINGS_OFFER_CODES = (errors.MODEL_NOT_INSTALLED, errors.MODEL_CORRUPTED)

AUTO_MODEL_UNAVAILABLE_I18N_KEY = "transcription_model_unavailable_auto"
HAS_NOTES_I18N_KEY = "transcription_result_has_notes"

#: The headline of a stored transcription opened again, with and without the
#: model — a stored value always has one, but a sentence ending in "with the
#: model ." is what a damaged one would otherwise say.
SAVED_OPENED_I18N_KEY = "transcription_saved_opened"
SAVED_OPENED_NO_MODEL_I18N_KEY = "transcription_saved_opened_no_model"

#: The note for a fresh result that was not kept, by what storing answered.
NOT_SAVED_I18N_KEYS = {
    stored_transcription.SAVE_UNSENT: "transcription_not_saved_unsent",
    stored_transcription.SAVE_MISSING: "transcription_not_saved_missing",
}

#: What is said, instead of opening the result window, when the message was
#: deleted for everyone while it was being transcribed (SAVE_WITHDRAWN).
WITHDRAWN_I18N_KEY = "transcription_discarded_withdrawn"

#: Every key this module asks for besides the ones narration/errors/preferences
#: own. The i18n test reads this rather than a list of its own.
FLOW_I18N_KEYS = (
    tuple(_OWN_PHASE_I18N_KEYS.values())
    + (
        AUTO_MODEL_UNAVAILABLE_I18N_KEY,
        HAS_NOTES_I18N_KEY,
        "transcription_no_audio",
        "transcription_starting",
        "transcription_open_settings_question",
        "transcription_result_title",
        SAVED_OPENED_I18N_KEY,
        SAVED_OPENED_NO_MODEL_I18N_KEY,
        WITHDRAWN_I18N_KEY,
        "transcription_delete_question",
        "transcription_deleted",
        "transcription_delete_failed",
    )
    + tuple(NOT_SAVED_I18N_KEYS.values())
)


# ── Pure decisions (no wx.App needed to test them) ───────────────────────────


def phase_status_text(i18n, phase, run):
    """The line for entering `phase`, or None when the phase says nothing.

    **Called from the `on_phase` callback itself**, on the thread that entered
    the phase, and that is what makes the device sentence true: job.py fills
    in `device` / `device_reason` immediately before announcing the loading
    phase, and read any earlier they are None — which narration turns into no
    sentence at all rather than into "you asked for the processor" on a
    machine with a working graphics card. Folded into the same line as the
    phase so the dialog shows it and the screen reader says it in one breath.
    """
    key = narration.phase_i18n_key(phase) or _OWN_PHASE_I18N_KEYS.get(phase)
    if key is None:
        return None
    parts = [i18n.t(key)]
    if phase == job_module.PHASE_LOADING_MODEL and run is not None:
        for note in narration.device_announcement(run.device, run.device_reason, run.model_id):
            parts.append(i18n.t(note.i18n_key).format(**note.values))
    return " ".join(parts)


def split_result_notes(announcement, notes):
    """(spoken, shown) — which caveats are said out loud and which the window shows.

    `spoken` is the voice-filter warning when there is one, and only that; see
    the module docstring for why it alone may not wait to be found. `shown` is
    every note except the one the headline already is (narration's no-speech
    overlap), including the spoken warning — the window has to hold everything
    the user was told, for a second reading.
    """
    shown = tuple(n for n in notes if n.i18n_key != announcement.i18n_key)
    spoken = tuple(n for n in shown if n.i18n_key == narration.VAD_UNAVAILABLE_I18N_KEY)
    return spoken, shown


def result_speech(i18n, announcement, notes, window):
    """The one sentence said when a transcription finishes.

    `window` is whether a result window will show the notes; without one (an
    empty result) there is nowhere to point to, so no pointer is added.
    """
    spoken, shown = split_result_notes(announcement, notes)
    parts = [i18n.t(announcement.i18n_key).format(**announcement.values)]
    parts.extend(i18n.t(n.i18n_key).format(**n.values) for n in spoken)
    if window and len(shown) > len(spoken):
        parts.append(i18n.t(HAS_NOTES_I18N_KEY))
    return " ".join(parts)


def model_problem_i18n_key(error_code, resolution, settings):
    """The sentence for "there is no model to run this with".

    Three situations reach it, and one sentence would be false for two of
    them: nothing fits this machine (or it could not be measured) —
    preferences' own keys; the user chose a model that is not downloaded —
    errors' own key, which says "the model you chose"; and the automatic
    choice landed on a model that is not downloaded, where "the model you
    chose" names a choice nobody made.
    """
    if error_code == errors.MODEL_CORRUPTED:
        return errors.error_i18n_key(errors.MODEL_CORRUPTED)
    if resolution is None:
        return errors.error_i18n_key(errors.MODEL_NOT_INSTALLED)
    if resolution.model_id is None:
        return preferences.MODEL_NONE_I18N_KEYS.get(
            resolution.model_none_reason, preferences.MODEL_NONE_I18N_KEYS[
                preferences.MODEL_NONE_NOTHING_FITS]
        )
    stored = preferences.read_section(settings)[preferences.SETTING_MODEL]
    replaced = any(s.setting == preferences.SETTING_MODEL for s in resolution.substitutions)
    if stored == preferences.AUTO or replaced:
        return AUTO_MODEL_UNAVAILABLE_I18N_KEY
    return errors.error_i18n_key(errors.MODEL_NOT_INSTALLED)


def insertion_text(text, before, after):
    """`text` as it should go into the message field between `before` and `after`.

    Only spaces are added, and only where the text would otherwise run into a
    word the user had typed: inserting "see you at five" after "ok" must not
    produce "oksee you at five". After the text, only before a letter or a
    digit — a space in front of the comma the user already typed would be
    one more thing to fix by ear.
    """
    text = (text or "").strip()
    if not text:
        return ""
    if before and not before.isspace():
        text = " " + text
    if after and after.isalnum():
        text = text + " "
    return text


def title_name(panel, msg):
    """Who the message is from, by name — never a JID.

    The conversation's own name as the panel shows it, and in a group the
    participant who sent it first, since "a message in the family group" does
    not say whose voice it is.

    Only a participant who resolves to a *name*. `_get_participant_name()`
    falls back to a formatted phone number, and for an `@lid` nobody has
    mapped yet to the bare digits of the JID; either one in the title is a
    string NVDA reads digit by digit on every focus of the window, and the
    same digits would go on into the file name the Save dialog offers. The
    group's name alone says less, but everything it says is readable.
    """
    chat = getattr(panel, "conversation_name", "") or ""
    key = msg.get("key") or {}
    remote = key.get("remoteJid") or ""
    if remote.endswith("@g.us") and not key.get("fromMe"):
        participant = key.get("participant") or msg.get("participant") or ""
        if participant:
            who = panel._get_participant_name(participant, msg)
            if who and "@" not in who and not is_phone_like(who):
                return f"{who}, {chat}" if chat else who
    return chat


def saved_when(i18n, at) -> str:
    """When a stored transcription was made, in the user's own regional format.

    The same pattern the conversation uses for a message's date
    (`get_datetime_format()` over the language file's `datetime_fmt`), so the
    two read alike.
    """
    try:
        return datetime.fromtimestamp(at).strftime(get_datetime_format(i18n.t("datetime_fmt")))
    except (OverflowError, OSError, ValueError, TypeError):
        return ""


def saved_announcement(i18n, value) -> management.Announcement:
    """The spoken headline for opening `value`, a stored transcription.

    When and with which model go in the headline, not among the notes: the
    notes field is announced as "there are warnings above the text", and the
    date is not a warning — putting it there would make that pointer fire on
    every reopening and teach the user to ignore it.
    """
    when = saved_when(i18n, stored_transcription.decision_time(value))
    model = str(value.get("model_id") or "")
    if model:
        return management.Announcement(
            SAVED_OPENED_I18N_KEY, management.OUTCOME_DONE, {"when": when, "model": model}
        )
    return management.Announcement(
        SAVED_OPENED_NO_MODEL_I18N_KEY, management.OUTCOME_DONE, {"when": when}
    )


def open_transcription_settings(main_window):
    """Open Settings directly on the Transcription tab.

    Selecting the tab is the dialog's own `show_transcription_tab()`, not
    done from here: selecting it the ordinary way, before `ShowModal()`,
    enters the tab with no window on screen and spends its one-time warning
    where nobody hears it — the dialog's docstring says how.
    """
    from ui.dialogs.settings_dialog import SettingsDialog

    dlg = SettingsDialog(main_window)
    try:
        dlg.show_transcription_tab()
    finally:
        dlg.Destroy()


# ── The flow ─────────────────────────────────────────────────────────────────


class MessageTranscriptionFlow:
    """One transcription of one message. Built on the wx thread, used once.

    Methods run on the wx thread except the two marked otherwise, which the
    run calls from its worker threads and which may only cross back with
    `wx.CallAfter` (pinned by a test).
    """

    def __init__(self, panel, msg):
        self._panel = panel
        self._main_window = panel.main_window
        self._i18n = self._main_window.i18n
        self._msg = msg
        self._msg_id = (msg.get("key") or {}).get("id", "") if isinstance(msg, dict) else ""
        # The chat the message is shown in, as the panel's own writes use it
        # (`_persist_message_local_flags()`): that is the jid its row is
        # stored under, which a group member's or an @lid's key need not be.
        conversation = getattr(panel, "conversation", None)
        self._jid = conversation.get("remoteJid", "") if isinstance(conversation, dict) else ""
        #: The progress dialog on screen, while there is one.
        self._dialog = None
        #: The run currently behind it — read by `_on_phase` for the device.
        self._run = None

    def start(self):
        """Transcribe the message, report, and put the focus back."""
        if not message_audio.is_transcribable(self._msg):
            # Nothing opens: the user pressed a shortcut on a message that
            # holds no speech, and the answer is one short sentence.
            self._main_window.output(self._i18n.t("transcription_no_audio"))
            return

        handover = None
        declined = False
        try:
            run, result, error = self._run_behind_dialog(self._make_first_run)
            handover = getattr(run, "prepared_handover", None)
            if error is not None and handover is not None:
                offer = narration.cpu_retry_note(error, run.device)
                if offer is not None:
                    if self._ask_retry(self._i18n.t(offer.i18n_key)):
                        first = run
                        run, result, error = self._run_behind_dialog(
                            lambda on_progress, on_finished: self._make_retry_run(
                                first, on_progress, on_finished
                            )
                        )
                    else:
                        declined = True
        finally:
            # The converted audio of the whole recording, handed over by a run
            # that failed on the card. Ours whether the re-run happened, was
            # declined, or was never offered — and gone before the result
            # window opens, since nothing past this point needs it.
            audio_prep.discard(handover)

        if declined:
            # The question already said what failed and why; the user has
            # answered it. Saying the failure again now would be the same
            # report twice.
            self._focus_message()
            return
        self._report(run, result, error)

    # ── Running ──────────────────────────────────────────────────────────────

    def _run_behind_dialog(self, make_run):
        """(run, result, error) — one run, behind one progress dialog."""
        dialog = TranscriptionProgressDialog(
            self._main_window,
            self._i18n,
            self._main_window.speak_output,
            make_run,
            self._i18n.t("transcription_starting"),
        )
        self._dialog = dialog
        try:
            dialog.run()
            return dialog.job, dialog.result, dialog.error
        finally:
            self._dialog = None
            dialog.Destroy()

    def _make_first_run(self, on_progress, on_finished):
        mw = self._main_window
        run = message_run.MessageTranscription(
            self._msg,
            mw.settings,
            mw.key,
            data_path("voice_messages"),
            data_path("media"),
            # `_app_settings`, with the underscore — the attribute the window
            # really has. SettingsDialog._transcription_app_settings() records
            # what the other spelling cost: a folder that was never read back.
            stored_models_dir=preferences.stored_models_dir(getattr(mw, "_app_settings", None)),
            ui_language=getattr(self._i18n, "language", ""),
            find_ffmpeg=mw._find_api_ffmpeg,
            is_online=lambda: bool(getattr(mw, "_wa_connected", False)),
            fetch_media=self._panel._download_media_to_disk,
            on_phase=self._on_phase,
            on_progress=on_progress,
            on_finished=on_finished,
        )
        self._run = run
        return run

    def _make_retry_run(self, previous, on_progress, on_finished):
        run = message_run.MessageTranscription.retry_on_cpu(
            previous,
            on_phase=self._on_phase,
            on_progress=on_progress,
            on_finished=on_finished,
        )
        self._run = run
        return run

    # ── Called on the run's threads ──────────────────────────────────────────
    # Nothing below this line may touch a wx control, the panel or the speech
    # output directly — only through wx.CallAfter.

    def _on_phase(self, phase):
        text = phase_status_text(self._i18n, phase, self._run)
        if text:
            wx.CallAfter(self._show_status, text)

    # ── Back on the wx thread ────────────────────────────────────────────────

    def _show_status(self, text):
        dialog = self._dialog
        if dialog:
            dialog.set_status(text)

    def _ask_retry(self, question):
        """Yes/No on redoing the run on the processor.

        The sentence is the dialog's text and is not spoken as well: every
        screen reader reads a message box as it opens, and speaking it too
        would say the same thing twice, the second copy cut by the first.
        """
        self._play_error_sound()
        dlg = wx.MessageDialog(
            self._main_window, question, self._i18n.t("transcription_progress_title"),
            wx.YES_NO | wx.ICON_QUESTION,
        )
        try:
            return dlg.ShowModal() == wx.ID_YES
        finally:
            dlg.Destroy()

    def _report(self, run, result, error):
        i18n = self._i18n
        if error is not None:
            code = getattr(error, "code", None)
            media_status = getattr(run, "media_status", None)
            if code == errors.MEDIA_NOT_DOWNLOADED and media_status in _MEDIA_STATUS_I18N_KEYS:
                self._say_after_focus(i18n.t(_MEDIA_STATUS_I18N_KEYS[media_status]), failed=True)
                return
            if code in _SETTINGS_OFFER_CODES:
                key = model_problem_i18n_key(
                    code, getattr(run, "resolution", None), self._main_window.settings
                )
                self._offer_settings(i18n.t(key))
                return
            announcement = narration.outcome_announcement(None, error)
            self._say_after_focus(
                i18n.t(announcement.i18n_key).format(**announcement.values),
                failed=announcement.outcome == management.OUTCOME_FAILED,
            )
            return

        announcement = narration.outcome_announcement(result, None)
        notes = narration.result_notes(
            result,
            preferences.preferred_language(
                self._main_window.settings, getattr(i18n, "language", "")
            ),
        )
        if result is None or result.is_empty:
            # No window for an empty result: an empty text box is
            # indistinguishable from a bug to someone who cannot see it. The
            # sentence says there was no speech, and the filter warning still
            # goes with it — a note of pure noise transcribed without the
            # filter is exactly where the invented sentence would have been.
            # Nor is it stored: there is nothing to reuse, and a second run
            # that heard nothing must not replace a first one that did.
            self._say_after_focus(result_speech(i18n, announcement, notes, window=False))
            return

        answer = self._store(result)
        if answer == stored_transcription.SAVE_WITHDRAWN:
            # Not shown either, only said. The sender deleted the message for
            # everyone while it was being transcribed, and WinZapp takes a
            # withdrawn message's content off the screen the moment the
            # revoke arrives, playable audio included
            # (MainWindow._apply_remote_revoke()). The result window would
            # put that content back in another form — one that can be saved
            # to a file or inserted into the message field. The minutes of
            # work are lost, and the sentence says why.
            self._say_after_focus(i18n.t(WITHDRAWN_I18N_KEY))
            return
        not_saved = NOT_SAVED_I18N_KEYS.get(answer)
        if not_saved:
            # Said in the window, among the notes, because it changes what the
            # user can expect: this text will not be there to reopen.
            notes = tuple(notes) + (narration.Note(not_saved),)
        self._open_result_window(result, announcement, notes)

    def _store(self, result):
        """Keep `result` with the message; storing's SAVE_* answer.

        Dated after the decision the message already holds, whatever the
        clock says — "Transcrever novamente" after a clock was set back must
        still be the later decision (`stored_transcription.next_decision_time`).
        """
        previous = self._msg.get(stored_transcription.TRANSCRIPTION_KEY) if isinstance(self._msg, dict) else None
        value = stored_transcription.record_from_result(
            result, stored_transcription.next_decision_time(time.time(), previous)
        )
        return self._main_window.store_message_transcription(self._jid, self._msg_id, value)

    def show_saved(self):
        """Open the stored transcription — no model, no wait, same window."""
        value = stored_transcription.saved_transcription(self._msg)
        if value is None:
            # Nothing stored after all (deleted from another copy between the
            # menu opening and the click): the ordinary path, which is what the
            # same key does on a message with nothing stored.
            self.start()
            return
        result = stored_transcription.as_result(value)
        notes = narration.result_notes(
            result,
            preferences.preferred_language(
                self._main_window.settings, getattr(self._i18n, "language", "")
            ),
        )
        self._open_result_window(result, saved_announcement(self._i18n, value), notes)

    def delete_saved(self):
        """Ask, delete the stored transcription, and say how it went."""
        i18n = self._i18n
        dlg = wx.MessageDialog(
            self._main_window,
            i18n.t("transcription_delete_question"),
            i18n.t("transcription_progress_title"),
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
        )
        try:
            answer = dlg.ShowModal()
        finally:
            dlg.Destroy()
        # Back on the message now, whatever the answer: the deletion answers
        # from a background thread, and a focus move made when it does would
        # pull the user back from wherever they went in the meantime.
        self._focus_message()
        if answer != wx.ID_YES:
            return
        self._main_window.delete_message_transcription(self._jid, self._msg_id, self._after_delete)

    def _after_delete(self, ok):
        """On the wx thread, once the database has answered."""
        if ok:
            self._main_window.output(self._i18n.t("transcription_deleted"))
            return
        self._play_error_sound()
        self._main_window.output(self._i18n.t("transcription_delete_failed"))

    def _open_result_window(self, result, announcement, notes):
        """The result window over `result` — fresh or stored, the same one."""
        i18n = self._i18n
        _spoken, shown = split_result_notes(announcement, notes)
        name = title_name(self._panel, self._msg)
        title = (i18n.t("transcription_result_title").format(name=name) if name
                 else i18n.t("transcription_progress_title"))
        dialog = TranscriptionResultDialog(
            self._main_window,
            self._main_window,
            title,
            result.text,
            notes=[i18n.t(n.i18n_key).format(**n.values) for n in shown],
            spoken=result_speech(i18n, announcement, notes, window=True),
            default_file=default_file_name(i18n, name),
        )
        try:
            dialog.run()
            insert = dialog.insert_requested
        finally:
            dialog.Destroy()
        if insert:
            self._insert_into_message_field(result.text)
        else:
            self._focus_message()

    def _offer_settings(self, sentence):
        """Say what is missing, and offer to open the tab that fixes it."""
        i18n = self._i18n
        dlg = wx.MessageDialog(
            self._main_window,
            f"{sentence}\n\n{i18n.t('transcription_open_settings_question')}",
            i18n.t("transcription_progress_title"),
            wx.YES_NO | wx.ICON_WARNING,
        )
        try:
            answer = dlg.ShowModal()
        finally:
            dlg.Destroy()
        if answer == wx.ID_YES:
            open_transcription_settings(self._main_window)
        self._focus_message()

    def _say_after_focus(self, sentence, failed=False):
        if failed:
            self._play_error_sound()
        self._focus_message()
        # After the focus move, not before: see the module docstring.
        wx.CallAfter(self._main_window.output, sentence)

    def _play_error_sound(self):
        sound = getattr(self._main_window, "error_sound", None)
        if sound is not None:
            sound.play()

    def _focus_message(self):
        """Back to the message the user came from — found by its id.

        Not by the row it was on: a transcription takes minutes, and messages
        arriving meanwhile re-sort `_sorted_messages`, so the old index is some
        other message by now. A message no longer in the list (deleted,
        revoked, scrolled out of the loaded window) leaves the focus on the
        list itself rather than on a neighbour it was never on.
        """
        panel = self._panel
        index = panel._find_index_by_msg_id(self._msg_id)
        if index >= 0:
            panel._focus_message_row(index)
            return
        messages_list = panel.messages_list
        if messages_list.IsShown():
            messages_list.SetFocus()

    def _insert_into_message_field(self, text):
        """Put the text in at the cursor, keeping everything already typed.

        `WriteText()` rather than rebuilding the value: it lets Windows do the
        caret arithmetic, which is UTF-16 and breaks around emoji when done by
        hand (`insert_emoji()` learned that). It also *replaces* a selection,
        so a selection is first collapsed to its end — "without erasing what
        was typed" includes what was typed and happened to be selected.
        """
        field = self._panel.message_field
        start, end = field.GetSelection()
        if start != end:
            field.SetInsertionPoint(end)
        position = field.GetInsertionPoint()
        before = field.GetRange(position - 1, position) if position > 0 else ""
        after = field.GetRange(position, position + 1)
        piece = insertion_text(text, before, after)
        if piece:
            field.WriteText(piece)
        field.SetFocus()


def transcribe_message(panel, msg):
    """Run a transcription — "Transcrever novamente", or a message with none."""
    MessageTranscriptionFlow(panel, msg).start()


def open_or_transcribe(panel, msg):
    """Entry point for Alt+Shift+T and the menu's first transcription item.

    The stored transcription when there is one — the key never starts minutes
    of work to produce what is already there — and a run otherwise.
    """
    flow = MessageTranscriptionFlow(panel, msg)
    if stored_transcription.saved_transcription(msg) is not None:
        flow.show_saved()
    else:
        flow.start()


def delete_transcription(panel, msg):
    """Entry point for "Apagar transcrição"."""
    MessageTranscriptionFlow(panel, msg).delete_saved()
