"""Startup exits before an account is bound leave a trace and, in a
foreground launch, a message (client/startup_exit_notice.py).

Reported 2026-10-18: after a multi-account update WinZapp "did not open" and
neither account's log.log said anything after the update, because the exits
in main.py ran before setup_logging() and said nothing on screen.
"""

from pathlib import Path

import pytest

import startup_exit_notice as notice

MAIN = (Path(__file__).resolve().parents[1] / "client" / "main.py").read_text(encoding="utf-8")


class TestWhichExitsTalk:
    @pytest.mark.parametrize("reason, key", [
        (notice.UPDATE_IN_PROGRESS, "startup_update_in_progress"),
        (notice.ACCOUNT_NOT_ANSWERING, "startup_account_not_answering"),
    ])
    def test_a_foreground_launch_is_told_why(self, reason, key):
        assert notice.message_key(reason, background=False) == key

    def test_a_handed_over_launch_says_nothing(self):
        """The running window is coming forward: a box on top of it is noise."""
        assert notice.message_key(notice.ACTIVATED, background=False) is None

    @pytest.mark.parametrize("reason", [notice.UPDATE_IN_PROGRESS, notice.ACCOUNT_NOT_ANSWERING,
                                        notice.ALREADY_RUNNING_BACKGROUND])
    def test_a_background_launch_never_shows_a_box(self, reason):
        assert notice.message_key(reason, background=True) is None


class TestTheTrace:
    def test_the_line_names_the_reason_the_process_and_the_account(self):
        line = notice.log_line(notice.ACCOUNT_NOT_ANSWERING, "abc123", pid=77, now=0)
        assert line.endswith("[pid=77] startup exit: account_not_answering account=abc123\n")

    def test_without_an_account_the_line_has_none(self):
        assert "account=" not in notice.log_line(notice.UPDATE_IN_PROGRESS, pid=1, now=0)

    def test_record_appends_and_never_raises(self, tmp_path):
        path = tmp_path / "bootstrap.log"
        notice.record(notice.UPDATE_IN_PROGRESS, path=str(path))
        notice.record(notice.ACTIVATED, "abc", path=str(path))
        assert path.read_text(encoding="utf-8").count("startup exit:") == 2
        notice.record(notice.ACTIVATED, path=str(tmp_path / "missing" / "x.log"))


class TestLeaveStartup:
    def _leave(self, monkeypatch, reason, background):
        monkeypatch.setattr(notice, "record", lambda *a, **k: recorded.append(a))
        recorded, shown = [], []
        with pytest.raises(SystemExit) as exit_info:
            notice.leave_startup(reason, background=background, account_id="abc",
                                 translate=lambda key: f"<{key}>", show=shown.append)
        return exit_info.value.code, recorded, shown

    def test_exits_cleanly_after_recording_and_telling_the_user(self, monkeypatch):
        code, recorded, shown = self._leave(monkeypatch, notice.UPDATE_IN_PROGRESS, background=False)
        assert code == 0, "account_launcher counts exit code 0 as a good hand-over"
        assert recorded == [(notice.UPDATE_IN_PROGRESS, "abc")]
        assert shown == ["<startup_update_in_progress>"]

    def test_background_records_but_shows_nothing(self, monkeypatch):
        code, recorded, shown = self._leave(monkeypatch, notice.UPDATE_IN_PROGRESS, background=True)
        assert code == 0 and recorded and shown == []

    def test_a_failing_message_box_still_exits(self, monkeypatch):
        monkeypatch.setattr(notice, "record", lambda *a, **k: None)

        def _boom(_text):
            raise OSError("no desktop")

        with pytest.raises(SystemExit):
            notice.leave_startup(notice.ACCOUNT_NOT_ANSWERING, background=False,
                                 translate=lambda key: key, show=_boom)


class TestMainUsesIt:
    def test_the_update_gate_no_longer_exits_silently(self):
        gate = MAIN[MAIN.index("os.makedirs(gd, exist_ok=True)"):MAIN.index("atexit.register(update_coord.release_runtime_lease")]
        assert "sys.exit" not in gate
        assert "leave_startup(startup_exit_notice.UPDATE_IN_PROGRESS" in gate

    def test_an_account_already_running_no_longer_exits_silently(self):
        block = MAIN[MAIN.index("if not first_instance:"):MAIN.index("setup_logging()\n")]
        assert "sys.exit" not in block
        assert "startup_exit_notice.ACCOUNT_NOT_ANSWERING" in block
        assert "startup_exit_notice.ACTIVATED" in block
