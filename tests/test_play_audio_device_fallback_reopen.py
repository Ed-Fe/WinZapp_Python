"""ConversationsPanel._play_audio()'s retry after an output-device fallback
called `_open_stream()`, a local closure that 83724638 (2026-09-03) turned
into the method `_open_audio_stream_from_temp_file()`. Two of its three
callers were updated; the one inside the device-fallback retry was not.

So when play() failed on a device BASS no longer had (a USB/Bluetooth
headset unplugged, the default device changed, Settings switching the
output), SoundSystem.handle_playback_failure() re-initialised BASS on the
default device and returned True — and the retry then raised NameError,
which its own `except` swallowed as "Retry after device fallback also
failed" before calling _stop_audio(). The message simply did not play, with
nothing said; only a second attempt worked. Shipped in 1.1.0.0 and 1.1.1.0.

pyflakes reports the undefined name; no test reached this branch.

ConversationsPanel is a wx.Panel, so _play_audio() is bound onto a plain
stub, the same way tests/test_toggle_playback_device_switch_recovery.py
covers the neighbouring _toggle_playback() recovery.
"""

import os

from tests.god_modules import patch_conversations_global
from ui.conversations import ConversationsPanel


class _Stream:
    def __init__(self, raise_on_play=False):
        self.played = 0
        self.positions = []
        self._raise_on_play = raise_on_play

    def play(self):
        self.played += 1
        if self._raise_on_play:
            raise RuntimeError("BASS_ERROR_HANDLE: the device this stream was on is gone")

    def set_position(self, pos):
        self.positions.append(pos)


class _Timer:
    def __init__(self):
        self.started = []

    def Start(self, interval):
        self.started.append(interval)


class _SoundSystem:
    def __init__(self, fell_back):
        self.fell_back = fell_back
        self.calls = 0

    def handle_playback_failure(self):
        self.calls += 1
        return self.fell_back


class _MainWindow:
    key = b"k"

    def __init__(self, fell_back):
        self.sound_system = _SoundSystem(fell_back)

    def _find_api_ffmpeg(self):
        return None


class _Stub:
    _play_audio = ConversationsPanel._play_audio

    def __init__(self, streams, fell_back=True):
        self.main_window = _MainWindow(fell_back)
        self.conversation = {"remoteJid": "5511999999999@s.whatsapp.net"}
        self._audio_stream = None
        self._audio_tempo_ctrl = None
        self._audio_temp_file = None
        self._current_audio_id = None
        self._audio_positions = {}
        self._audio_timer = _Timer()
        self._audio_speed_steps = [1.0]
        self._audio_speed_index = 0
        self._is_audio_playing = False
        self.stop_calls = 0
        self._streams = list(streams)
        self.opened = []

    def _open_audio_stream_from_temp_file(self):
        stream = self._streams.pop(0)
        self.opened.append(stream)
        return stream, None

    def _stop_audio(self):
        self.stop_calls += 1

    def _focused_msg_id(self):
        return None


def _voice_file(tmp_path, monkeypatch):
    # The stored file is encrypted; decryption is not what is under test.
    patch_conversations_global(monkeypatch, "decrypt_bytes", lambda content, key: content)
    path = tmp_path / "voice.ogg"
    path.write_bytes(b"OggS" + b"\0" * 60)
    return str(path)


def _cleanup(stub):
    if stub._audio_temp_file and os.path.exists(stub._audio_temp_file):
        os.unlink(stub._audio_temp_file)


def test_after_a_device_fallback_the_message_plays_on_a_fresh_stream(tmp_path, monkeypatch):
    dead, fresh = _Stream(raise_on_play=True), _Stream()
    stub = _Stub([dead, fresh], fell_back=True)
    try:
        stub._play_audio("m1", 12, _voice_file(tmp_path, monkeypatch))
    finally:
        _cleanup(stub)

    assert stub.main_window.sound_system.calls == 1
    assert stub.opened == [dead, fresh], "a new stream must be opened after the fallback"
    assert fresh.played == 1
    assert stub._audio_stream is fresh
    assert stub.stop_calls == 0
    assert stub._is_audio_playing is True
    assert stub._audio_timer.started == [30]


def test_the_saved_position_is_restored_on_the_fresh_stream(tmp_path, monkeypatch):
    dead, fresh = _Stream(raise_on_play=True), _Stream()
    stub = _Stub([dead, fresh], fell_back=True)
    stub._audio_positions["m1"] = 4.5
    try:
        stub._play_audio("m1", 12, _voice_file(tmp_path, monkeypatch))
    finally:
        _cleanup(stub)

    assert fresh.positions == [4.5]
    assert fresh.played == 1


def test_without_a_fallback_playback_still_stops(tmp_path, monkeypatch):
    """handle_playback_failure() returning False means nothing was
    re-initialised, so there is no fresh device to retry on."""
    dead = _Stream(raise_on_play=True)
    stub = _Stub([dead], fell_back=False)
    try:
        stub._play_audio("m1", 12, _voice_file(tmp_path, monkeypatch))
    finally:
        _cleanup(stub)

    assert stub.opened == [dead]
    assert stub.stop_calls == 1
    assert stub._is_audio_playing is False
