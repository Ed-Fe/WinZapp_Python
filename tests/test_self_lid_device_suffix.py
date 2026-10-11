"""A self LID with a companion-device suffix ("<id>:56@lid") must never be
used as a send destination: WhatsApp Web answers HTTP 500 and a message to
oneself ends as "could not confirm the send"."""

from main import MainWindow


class _Stub:
    _normalize_jid = staticmethod(MainWindow._normalize_jid)

    def __init__(self, phone_to_lid):
        self._phone_to_lid = phone_to_lid


def test_cached_lid_with_device_suffix_is_stripped_on_send():
    stub = _Stub({"5511999990000@s.whatsapp.net": "123456:56@lid"})
    got = MainWindow._resolve_jid_for_chat_state(stub, "5511999990000@s.whatsapp.net")
    assert got == "123456@lid"


def test_lid_destination_with_device_suffix_is_stripped():
    stub = _Stub({})
    assert MainWindow._resolve_jid_for_chat_state(stub, "123456:56@lid") == "123456@lid"


def test_plain_lid_is_untouched():
    stub = _Stub({"5511999990000@s.whatsapp.net": "123456@lid"})
    got = MainWindow._resolve_jid_for_chat_state(stub, "5511999990000@c.us")
    assert got == "123456@lid"
