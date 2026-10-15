"""Automatic online transcription and description: which messages qualify."""
import time

from core.ai_media import auto, config
from core.transcription.stored import TRANSCRIPTION_KEY


def _prefs(**over):
    base = {"enabled": True, "kinds": {k: True for k in config.KINDS},
            "auto_transcribe": True, "auto_describe": True,
            "order": list(config.PROVIDERS), "disabled": [], "consented": []}
    base.update(over)
    return base


def _msg(msg_type="audioMessage", id_="A1", from_me=False, seconds=5, **extra):
    inner = {"mimetype": "audio/ogg", "seconds": seconds, "fileLength": 1000}
    msg = {"messageType": msg_type, "key": {"id": id_, "fromMe": from_me},
           "message": {msg_type: inner}, "messageTimestamp": time.time()}
    msg.update(extra)
    return msg


def test_defaults_are_off():
    from core.ai_media.config import preferences

    class App(dict):
        pass
    prefs = preferences(App())
    assert prefs["auto_transcribe"] is False and prefs["auto_describe"] is False


def test_each_option_covers_its_own_kinds():
    assert auto.wanted_kind(_msg(), _prefs()) == "audio"
    assert auto.wanted_kind(_msg(), _prefs(auto_transcribe=False)) is None
    for kind_type, kind in (("imageMessage", "image"), ("stickerMessage", "sticker"),
                            ("videoMessage", "video")):
        msg = _msg(kind_type)
        assert auto.wanted_kind(msg, _prefs()) == kind
        assert auto.wanted_kind(msg, _prefs(auto_describe=False)) is None
        # Describing is independent of the transcription option.
        assert auto.wanted_kind(msg, _prefs(auto_transcribe=False)) == kind


def test_the_local_automatic_transcription_wins_for_voice_only():
    assert auto.wanted_kind(_msg(), _prefs(), local_auto_transcription=True) is None
    assert auto.wanted_kind(_msg("imageMessage"), _prefs(),
                            local_auto_transcription=True) == "image"


def test_what_is_left_alone():
    assert auto.wanted_kind(_msg(from_me=True), _prefs()) is None
    assert auto.wanted_kind(_msg(**{TRANSCRIPTION_KEY: {"at": 1.0, "text": "x"}}), _prefs()) is None
    assert auto.wanted_kind(_msg(seconds=60 * 60), _prefs()) is None
    old = _msg(messageTimestamp=time.time() - 3600)
    assert auto.wanted_kind(old, _prefs()) is None
    assert auto.wanted_kind(_msg(), _prefs(enabled=False)) is None
    kinds = {k: True for k in config.KINDS}
    kinds["audio"] = False
    assert auto.wanted_kind(_msg(), _prefs(kinds=kinds)) is None
    big = _msg("imageMessage")
    big["message"]["imageMessage"]["fileLength"] = 10 ** 9
    assert auto.wanted_kind(big, _prefs()) is None


def test_only_consented_providers_receive_anything():
    saved = set(config.PROVIDERS)
    some = [p for p in config.PROVIDERS if config.supports(p, "audio")]
    assert auto.usable_providers(_prefs(), "audio", saved) == []
    got = auto.usable_providers(_prefs(consented=some[:1]), "audio", saved)
    assert got == some[:1]
    assert auto.usable_providers(_prefs(consented=some), "audio", set()) == []


class _Box:
    def __init__(self, value):
        self.value = value

    def GetValue(self):
        return self.value

    def SetValue(self, value):
        self.value = value


def _options(local_on, answer, monkeypatch):
    import wx
    from types import SimpleNamespace
    from ui.dialogs.transcription_options import TranscriptionOptionsMixin

    asked = []
    monkeypatch.setattr(wx, "MessageBox",
                        lambda text, *a, **k: asked.append(text) or (wx.YES if answer else wx.NO))
    dialog = TranscriptionOptionsMixin()
    dialog.main_window = SimpleNamespace(i18n=SimpleNamespace(t=lambda key: key))
    dialog._transcription_auto_check = _Box(local_on)
    dialog._ai_page = SimpleNamespace(auto_transcribe=_Box(False))
    return dialog, asked


def test_turning_the_online_option_on_asks_before_turning_the_local_one_off(monkeypatch):
    dialog, asked = _options(local_on=True, answer=True, monkeypatch=monkeypatch)
    assert dialog._confirm_online_replaces_local() is True
    assert dialog._transcription_auto_check.GetValue() is False
    assert asked == ["transcription_online_auto_replaces_local"]

    dialog, asked = _options(local_on=True, answer=False, monkeypatch=monkeypatch)
    assert dialog._confirm_online_replaces_local() is False
    assert dialog._transcription_auto_check.GetValue() is True

    dialog, asked = _options(local_on=False, answer=False, monkeypatch=monkeypatch)
    assert dialog._confirm_online_replaces_local() is True and asked == []


def test_turning_the_local_option_on_asks_before_turning_the_online_one_off(monkeypatch):
    from types import SimpleNamespace

    dialog, asked = _options(local_on=True, answer=True, monkeypatch=monkeypatch)
    dialog._ai_page.auto_transcribe.SetValue(True)
    event = SimpleNamespace(Skip=lambda: None)
    dialog._on_local_auto_toggle(event)
    assert dialog._ai_page.auto_transcribe.GetValue() is False
    assert dialog._transcription_auto_check.GetValue() is True
    assert asked == ["transcription_auto_replaces_online"]

    dialog, asked = _options(local_on=True, answer=False, monkeypatch=monkeypatch)
    dialog._ai_page.auto_transcribe.SetValue(True)
    dialog._on_local_auto_toggle(event)
    assert dialog._transcription_auto_check.GetValue() is False
    assert dialog._ai_page.auto_transcribe.GetValue() is True
