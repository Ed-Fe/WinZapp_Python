"""build_app.py's calls to Apple's servers: a failure must say what Apple
answered, a timeout is retried, and a rejected notarization stops the build
with Apple's log instead of an unexplained stapler error. Nothing is signed
or sent to Apple here; subprocess.run is faked.
"""

import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path[:0] = [os.path.join(ROOT, "macos")]

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS build")

import build_app  # noqa: E402

# What notarytool 1.x printed for real submissions (2026-10-01).
ACCEPTED = '{"status":"Accepted","message":"Processing complete","id":"e886e6f2-8b8a-4e58-b54d-0800fe399e79"}\n'
INVALID = '{"status":"Invalid","id":"84a9b45d-6600-4fb8-85bb-ccb44d5f1226","message":"Processing complete"}\n'
TIMED_OUT = 'Error: HTTPError(statusCode: nil, error: Error Domain=NSURLErrorDomain Code=-1001 "The request timed out.")'


class FakeRun:
    """Answers each subprocess.run call with the next (returncode, stdout, stderr)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        code, out, err = self.answers.pop(0) if self.answers else (0, "", "")
        return subprocess.CompletedProcess(cmd, code, out, err)


@pytest.fixture
def no_wait(monkeypatch):
    monkeypatch.setattr(build_app.time, "sleep", lambda s: None)


def test_notary_status_reads_the_submission():
    assert build_app.notary_status(ACCEPTED) == ("e886e6f2-8b8a-4e58-b54d-0800fe399e79", "Accepted")
    assert build_app.notary_status(INVALID)[1] == "Invalid"


def test_notary_status_without_an_answer():
    assert build_app.notary_status("") == (None, None)
    assert build_app.notary_status("not json\n") == (None, None)


def test_a_failed_codesign_prints_apples_message(monkeypatch, capsys, no_wait):
    fake = FakeRun(*[(1, "", "The timestamp service is not available.")] * 3)
    monkeypatch.setattr(build_app.subprocess, "run", fake)
    with pytest.raises(subprocess.CalledProcessError):
        build_app.run_apple(["codesign", "x.node"], quiet=True)
    out = capsys.readouterr().out
    assert out.count("The timestamp service is not available.") == 3
    assert "attempt 3/3 failed" in out
    assert len(fake.calls) == 3


def test_a_timeout_is_retried_and_then_succeeds(monkeypatch, capsys, no_wait):
    fake = FakeRun((1, "", "The timestamp service is not available."), (0, "", ""))
    monkeypatch.setattr(build_app.subprocess, "run", fake)
    assert build_app.run_apple(["codesign", "x.node"], quiet=True).returncode == 0
    assert len(fake.calls) == 2


def test_a_quiet_success_prints_nothing(monkeypatch, capsys):
    monkeypatch.setattr(build_app.subprocess, "run", FakeRun((0, "", "x.node: replacing existing signature")))
    build_app.run_apple(["codesign", "x.node"], quiet=True)
    assert capsys.readouterr().out == ""


@pytest.fixture
def notary_env(monkeypatch, tmp_path):
    monkeypatch.setenv("WINZAPP_NOTARY_KEY", str(tmp_path / "key.p8"))
    monkeypatch.setenv("WINZAPP_NOTARY_KEY_ID", "KEYID")
    monkeypatch.setenv("WINZAPP_NOTARY_ISSUER", "ISSUER")
    monkeypatch.setattr(build_app, "BUILD", str(tmp_path))
    ran = []

    def run(cmd, **kw):
        ran.append(list(cmd))
        if cmd[0] == "ditto":
            open(cmd[-1], "w").close()      # the archive notarize() removes at the end

    monkeypatch.setattr(build_app, "run", run)
    return ran


def test_notarization_retries_when_apple_is_not_reached(monkeypatch, notary_env, no_wait):
    fake = FakeRun((1, "", TIMED_OUT), (0, ACCEPTED, ""))
    monkeypatch.setattr(build_app.subprocess, "run", fake)
    assert build_app.notarize() is True
    assert [c[2] for c in fake.calls] == ["submit", "submit"]
    assert any(c[:3] == ["xcrun", "stapler", "staple"] for c in notary_env)


def test_notarization_gives_up_after_the_last_attempt(monkeypatch, notary_env, no_wait, capsys):
    monkeypatch.setattr(build_app.subprocess, "run", FakeRun(*[(1, "", TIMED_OUT)] * 3))
    with pytest.raises(SystemExit, match="could not be reached"):
        build_app.notarize()
    assert "The request timed out." in capsys.readouterr().out
    assert not any("stapler" in c for c in notary_env)


def test_a_rejected_app_prints_apples_log_and_stops(monkeypatch, notary_env, capsys):
    log = '"message": "The executable does not have the hardened runtime enabled."'
    fake = FakeRun((0, INVALID, ""), (0, log, ""))
    monkeypatch.setattr(build_app.subprocess, "run", fake)
    with pytest.raises(SystemExit, match="Invalid"):
        build_app.notarize()
    assert fake.calls[1][:4] == ["xcrun", "notarytool", "log", "84a9b45d-6600-4fb8-85bb-ccb44d5f1226"]
    assert "hardened runtime" in capsys.readouterr().out
    assert not any("stapler" in c for c in notary_env)


def test_a_release_bundle_is_versioned_by_its_tag():
    """A release is built from the tag's clean checkout, whose version.py is
    the unstamped placeholder: the tag is the version, numbers only (Apple
    specifies integers in CFBundleShortVersionString)."""
    assert build_app.bundle_version(("v2.1.0.4050alpha", "a" * 40)) == "2.1.0.4050"
    assert build_app.bundle_version(("v2.1.0.4055beta", "a" * 40)) == "2.1.0.4055"
    assert build_app.bundle_version(("v2.1.0.4060", "a" * 40)) == "2.1.0.4060"


def test_a_development_bundle_keeps_version_py(monkeypatch):
    monkeypatch.syspath_prepend(build_app.CLIENT)   # restores sys.path, bundle_version's insert included
    from version import __version__
    assert build_app.bundle_version(None) == __version__
