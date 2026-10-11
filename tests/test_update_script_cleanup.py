"""The installer script removes the extracted update when the copy worked.

Every update left a full copy of the release in the user's %TEMP% because
nothing ever deleted it: the script removed itself and nothing else. The line
that does is placed where it cannot hurt — after a successful copy, after the
relaunch (the user is waiting for the app, not for a few thousand deletes), and
never on the failure paths, where the folder is evidence and the retry's source.
"""

import updater

EXE = r"C:\WinZapp\WinZapp.exe"
LOG = r"C:\L\update_install.log"
EXTRACTED = r"C:\Users\me\AppData\Local\Temp\winzapp_ext_ab12"
REMOVE = f'rmdir /S /Q "{EXTRACTED}"'


def _script(**kw):
    return updater._build_installer_script(
        EXTRACTED + r"\WinZapp", r"C:\WinZapp", EXE, LOG,
        r"C:\WinZapp\update_failed.marker", pid=4242, api_port=6300, **kw,
    )


class TestTheCleanupLine:
    def test_it_is_in_the_script_when_a_folder_is_given(self):
        assert REMOVE in _script(cleanup_dir=EXTRACTED)

    def test_without_a_folder_the_script_is_what_it_was(self):
        assert "rmdir" not in _script()
        assert _script() == _script(cleanup_dir="")

    def test_a_directory_that_is_already_gone_is_not_an_error(self):
        assert f'if exist "{EXTRACTED}" {REMOVE}' in _script(cleanup_dir=EXTRACTED)

    def test_it_comes_after_the_copy_succeeded_and_the_app_was_relaunched(self):
        s = _script(cleanup_dir=EXTRACTED)
        copied = s.index("echo xcopy OK")
        # The failure branch relaunches too, earlier in the script: it is the
        # relaunch after the success marker that has to come first.
        assert copied < s.index(f'start "" "{EXE}"', copied) < s.index(REMOVE)

    def test_it_comes_before_the_script_deletes_itself(self):
        s = _script(cleanup_dir=EXTRACTED)
        assert s.index(REMOVE) < s.index('del "%~f0"')

    def test_a_failed_copy_keeps_the_folder(self):
        s = _script(cleanup_dir=EXTRACTED)
        failure = s[s.index("echo xcopy FAILED"):s.index("echo xcopy OK")]
        assert "rmdir" not in failure

    def test_timing_out_on_another_account_keeps_it_too(self):
        s = _script(extra_pids=[5151], cleanup_dir=EXTRACTED)
        timeout = s[s.index(":OTHER_ACCOUNT_TIMEOUT"):]
        assert "rmdir" not in timeout

    def test_the_log_says_it_was_attempted(self):
        assert "temporary update folder cleaned up" in _script(cleanup_dir=EXTRACTED)

    def test_the_elevated_script_cleans_up_after_handing_the_relaunch_over(self):
        handoff = r"C:\T\winzapp_upd_x.bat.relaunch"
        s = _script(cleanup_dir=EXTRACTED, relaunch_handoff=handoff)
        assert s.index(f'echo done> "{handoff}.done"') < s.index(REMOVE)


class TestWhatTheInstallerPassesIt:
    def test_it_is_handed_the_root_not_the_folder_inside_it(self):
        source = open(updater.__file__, encoding="utf-8").read()
        assert "extraction_root(extracted_dir)" in source
        assert "cleanup_dir=cleanup_dir" in source
