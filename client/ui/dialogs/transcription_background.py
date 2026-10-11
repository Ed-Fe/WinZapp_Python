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

    def _transcription_background_wanted(self, action) -> bool:
        """Whether `action` goes to the main window instead of the dialog."""
        return transcription_background.runs_in_background(
            action,
            background_downloads_enabled(getattr(self.main_window, "settings", None)),
        )

    def _start_transcription_job(self, action, model_id=None, build_id=None,
                                 compute_capability=None):
        """Run one action, then say it. In the background when the user asked."""
        if self._transcription_background_wanted(action):
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
        self._enter_transcription_background_run()

    def _enter_transcription_background_run(self) -> None:
        """The tab goes quiet and offers Cancel while the window runs the job."""
        self._set_transcription_job_running(True)
        self._show_transcription_cancel(True)
        self.main_window.watch_transcription_background(
            self._transcription_background_ended
        )

    def _transcription_background_ended(self, job, result, error, last=True) -> bool:
        """The window's call when a job of the background run is over.

        True: said here. `last` is False between the jobs of a queue, when the
        tab stays busy and Cancel stays offered.
        """
        if not self:
            # Settings was closed meanwhile; the window says it.
            return False
        if last:
            self._set_transcription_job_running(False)
            self._show_transcription_cancel(False)
        self._after_transcription_job(job, result, error, restore_focus=False)
        return True

    def _after_transcription_job(self, job, result, error, restore_focus=True,
                                 extra_keys=()):
        """Report a finished job by whichever family its action belongs to."""
        if job.action in transcription_management.WHISPER_CPP_ACTIONS:
            self._finish_whisper_cpp_job(
                job.action, job.build_id, job, result, error,
                restore_focus=restore_focus,
            )
            return
        if error is None and job.action == transcription_management.ACTION_DOWNLOAD_MODEL:
            # It arrived: it is no longer waiting in the queue.
            self._transcription_model_marks.discard(job.model_id)
        self._report_transcription_job(
            job.action, job.model_id, job, result, error,
            extra_keys=extra_keys, restore_focus=restore_focus,
        )

    def _watch_transcription_background(self) -> None:
        """A tab opened while a download runs in the background adopts it."""
        running = getattr(self.main_window, "transcription_background_running", None)
        if not callable(running) or not running():
            return
        self._enter_transcription_background_run()

    # ── Cancel ───────────────────────────────────────────────────────────────

    def _show_transcription_cancel(self, shown) -> None:
        """Offer Cancel only while there is something to cancel.

        Hidden rather than disabled: it is a stop in the tab order that answers
        nothing the rest of the time. Re-enabled whenever it is shown again,
        since pressing it disables it until the run is over.
        """
        button = getattr(self, "_transcription_cancel_btn", None)
        if button is None:
            return
        button.Enable(True)
        button.Show(bool(shown))
        button.GetParent().Layout()

    def _on_transcription_cancel_background(self, event) -> None:
        """Stop the running download and whatever was queued behind it."""
        if not self.main_window.cancel_transcription_background():
            return
        self._transcription_cancel_btn.Disable()
        self.main_window.speak_output.output(
            self.main_window.i18n.t("transcription_progress_cancelling")
        )
