"""TranscriptionBackgroundMixin — part of MainWindow (see main_window/__init__.py).

A transcription download (a model, the CUDA libraries, the whisper.cpp
program) that the user chose to run in the background — Settings > General,
"download updates in the background", the option the updater and the WPPConnect
Server update already follow (core/transcription/background.py).

**It lives here and not in the Settings dialog because it outlives it.** The
dialog is modal and the point of the option is that the user goes back to the
app; a job owned by the dialog would have to be cancelled with it, or finish
into a window that no longer exists. So the window starts the job and says how
it ended, and the dialog — when one is open — only asks to be told.

One at a time: the models folder and the libraries are install-wide and
ManagementJob serialises on their lock, but the second job would wait silently
behind the first, which is the one thing a background job must never do.
"""

import logging

import wx

from core.transcription import errors as transcription_errors
from core.transcription import management, management_text


class TranscriptionBackgroundMixin:
    """MainWindow's half of a transcription download that runs without a window."""

    def transcription_background_running(self) -> bool:
        return getattr(self, "_transcription_background_job", None) is not None

    def watch_transcription_background(self, listener) -> None:
        """Be told, on the wx thread, how the running job ended.

        `listener(job, result, error)` returns True when it said the outcome
        itself (the Settings tab does, on screen and aloud); otherwise the
        window says it. A listener is called once and forgotten, so a dialog
        that was closed meanwhile is not kept alive by this list — it only has
        to ignore the call (`if not self`).
        """
        listeners = self.__dict__.setdefault("_transcription_background_listeners", [])
        listeners.append(listener)

    def start_transcription_background_job(self, make_job, status_text) -> bool:
        """Start a management job with no window; False if one is running.

        `make_job(on_progress, on_finished)` is the factory the progress dialog
        takes, so the same job is built either way. Progress is not reported: a
        percentage read out every few seconds for half an hour is the noise
        the option exists to remove, and the start and the end are the news.
        """
        if self.transcription_background_running():
            return False
        try:
            job = make_job(None, self._post_transcription_background_finished)
            self._transcription_background_job = job
            job.start()
        except Exception as exc:
            logging.error("[transcription] the background job could not start: %s",
                          transcription_errors.exception_report(exc))
            self._transcription_background_job = None
            return False
        self.output(
            f"{status_text} {self.i18n.t('transcription_background_started')}"
        )
        return True

    def _post_transcription_background_finished(self, result, error) -> None:
        """On the job's thread: hop to the wx thread, touch nothing else."""
        wx.CallAfter(self._transcription_background_finished, result, error)

    def _transcription_background_finished(self, result, error) -> None:
        job = getattr(self, "_transcription_background_job", None)
        self._transcription_background_job = None
        if job is None:
            return
        listeners = self.__dict__.pop("_transcription_background_listeners", [])
        said = False
        for listener in listeners:
            try:
                said = bool(listener(job, result, error)) or said
            except Exception as exc:
                logging.error("[transcription] a background listener raised: %s",
                              transcription_errors.exception_report(exc))
        if said:
            return
        announcement = job.announcement(result, error)
        if announcement.outcome == management.OUTCOME_FAILED:
            try:
                self.error_sound.play()
            except Exception as exc:
                logging.warning("[transcription] could not play the error sound: %s",
                                transcription_errors.exception_report(exc))
        self.output(management_text.announcement_text(self.i18n, announcement))
