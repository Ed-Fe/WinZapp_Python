"""The Transcription tab's side of a download that runs without a window.

`_start_transcription_job()` is the one place the tab starts a model, CUDA or
whisper.cpp action. It runs it behind the progress dialog, as before, unless
the user turned on Settings > General > "download updates in the background"
and the action is a download (core/transcription/background.py): then the main
window runs it (main_window/transcription_background.py), the tab goes back to
being usable, and `_transcription_background_ended()` finishes the job when
the window says it is over.

Two things are deliberate:

* **The tab never owns the job.** Settings is modal, the user leaves it, and the
  job goes on. If the dialog is closed the window says the outcome alone; if a
  new dialog is opened meanwhile it adopts the running job
  (`_watch_transcription_background()`), with its buttons off, so a second
  action cannot queue behind the first on the models lock.

* **Finishing in the background never moves the focus.** The user is in
  another tab or another window by then.
"""

from core.transcription import background as transcription_background
from core.transcription import management as transcription_management
from update_background import background_downloads_enabled


class TranscriptionBackgroundTabMixin:
    """Starts and finishes the tab's actions, in a dialog or in the background."""

    def _start_transcription_job(self, action, model_id=None, build_id=None,
                                 compute_capability=None):
        """Run one action, then say it. In the background when the user asked."""
        background = transcription_background.runs_in_background(
            action,
            background_downloads_enabled(getattr(self.main_window, "settings", None)),
        )
        if background:
            self._start_transcription_in_background(
                action, model_id, build_id, compute_capability
            )
            return
        job, result, error = self._run_transcription_job(
            action, model_id=model_id, build_id=build_id,
            compute_capability=compute_capability,
        )
        self._after_transcription_job(job, result, error)

    def _start_transcription_in_background(self, action, model_id, build_id,
                                           compute_capability):
        make_job = self._transcription_job_factory(
            action, model_id=model_id, build_id=build_id,
            compute_capability=compute_capability,
        )
        started = self.main_window.start_transcription_background_job(
            make_job, self._transcription_status_text(action, model_id, build_id)
        )
        if not started:
            # Another download is already running: the buttons are off while
            # one is, so this is a press that got through, and it changes
            # nothing.
            return
        self._set_transcription_job_running(True)
        self.main_window.watch_transcription_background(
            self._transcription_background_ended
        )

    def _transcription_background_ended(self, job, result, error) -> bool:
        """The window's call when the background job is over. True: said here."""
        if not self:
            # Settings was closed meanwhile; the window says it.
            return False
        self._set_transcription_job_running(False)
        self._after_transcription_job(job, result, error, restore_focus=False)
        return True

    def _after_transcription_job(self, job, result, error, restore_focus=True):
        """Report a finished job by whichever family its action belongs to."""
        if job.action in transcription_management.WHISPER_CPP_ACTIONS:
            self._finish_whisper_cpp_job(
                job.action, job.build_id, job, result, error,
                restore_focus=restore_focus,
            )
            return
        self._report_transcription_job(
            job.action, job.model_id, job, result, error,
            restore_focus=restore_focus,
        )

    def _watch_transcription_background(self) -> None:
        """A tab opened while a download runs in the background adopts it."""
        running = getattr(self.main_window, "transcription_background_running", None)
        if not callable(running) or not running():
            return
        self._set_transcription_job_running(True)
        self.main_window.watch_transcription_background(
            self._transcription_background_ended
        )
