"""Audio messages without ffprobe: the duration read from Ogg and MP4 bytes built
here, the ffmpeg stderr parsing, the fallbacks, and send-voice-base64 for real
(create_app) against a fake client with no build_audio_message. Runs no ffmpeg."""
import struct
from types import SimpleNamespace as N

import pytest
from aiohttp.test_utils import TestClient, TestServer

import audio_message as am
import authentication as auth
import server


def _ogg_page(payload, granule):
    return (b"OggS" + bytes([0, 0]) + struct.pack("<q", granule) + b"\0" * 12
            + bytes([1, len(payload)]) + payload)


def _opus(granule, pre_skip=312, last_granule=None):
    head = b"OpusHead" + bytes([1, 1]) + struct.pack("<H", pre_skip) + b"\0" * 7
    pages = _ogg_page(head, 0) + _ogg_page(b"OpusTags", 0) + _ogg_page(b"audio", granule)
    if last_granule is not None:
        pages += _ogg_page(b"more", last_granule)
    return pages


def _vorbis(granule, rate=44100):
    head = b"\x01vorbis" + struct.pack("<I", 0) + bytes([2]) + struct.pack("<I", rate) + b"\0" * 10
    return _ogg_page(head, 0) + _ogg_page(b"audio", granule)


def _box(kind, payload):
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def _mp4(scale, length, version=0):
    if version == 1:
        mvhd = bytes([1, 0, 0, 0]) + b"\0" * 16 + struct.pack(">IQ", scale, length)
    else:
        mvhd = bytes([0, 0, 0, 0]) + b"\0" * 8 + struct.pack(">II", scale, length)
    return (_box(b"ftyp", b"M4A \0\0\0\0") + _box(b"free", b"")
            + _box(b"moov", _box(b"trak", b"") + _box(b"mvhd", mvhd + b"\0" * 80)))


# -------------------------------------------------------------- duration
def test_opus_voice_note_counts_at_48_khz_minus_the_pre_skip():
    assert am.ogg_seconds(_opus(48000 * 7 + 312)) == pytest.approx(7.0)


def test_ogg_skips_a_last_page_where_no_packet_ends():
    assert am.ogg_seconds(_opus(48000 * 3 + 312, last_granule=-1)) == pytest.approx(3.0)


def test_vorbis_uses_the_rate_of_its_header():
    assert am.ogg_seconds(_vorbis(44100 * 5, rate=44100)) == pytest.approx(5.0)


@pytest.mark.parametrize("data", [b"", b"ID3...", b"OggS" + b"\0" * 40])
def test_not_an_ogg_it_can_read_is_none(data):
    assert am.ogg_seconds(data) is None


@pytest.mark.parametrize("version", [0, 1])
def test_m4a_duration_comes_from_mvhd(version):
    assert am.mp4_seconds(_mp4(44100, 44100 * 12, version)) == pytest.approx(12.0)


@pytest.mark.parametrize("data", [b"", _opus(48000), _box(b"moov", b""), _mp4(0, 100)])
def test_not_an_mp4_it_can_read_is_none(data):
    assert am.mp4_seconds(data) is None


def test_ffmpeg_output_duration_line():
    stderr = ("Input #0, mp3, from 'x.audio':\n"
              "  Duration: 00:01:02.53, start: 0.025057, bitrate: 128 kb/s\n"
              "At least one output file must be specified\n")
    assert am.ffmpeg_output_seconds(stderr) == pytest.approx(62.53)


@pytest.mark.parametrize("stderr", ["", None, "Duration: N/A, bitrate: N/A"])
def test_ffmpeg_output_without_a_duration_is_none(stderr):
    assert am.ffmpeg_output_seconds(stderr) is None


def test_duration_asks_ffmpeg_only_for_what_it_cannot_read():
    asked = []

    def ffmpeg(data):
        asked.append(data)
        return 9.6
    assert am.duration_seconds(_opus(48000 * 4 + 312), ffmpeg) == 4
    assert am.duration_seconds(_mp4(1000, 2400), ffmpeg) == 2
    assert asked == []
    assert am.duration_seconds(b"ID3 mp3", ffmpeg) == 10
    assert asked == [b"ID3 mp3"]


def test_duration_nobody_can_measure_is_zero_not_an_error():
    assert am.duration_seconds(b"ID3 mp3", lambda data: None) == 0


# -------------------------------------------------------------- build
UPLOAD = N(url="https://mmg/x", DirectPath="/v/x", MediaKey=b"k", FileEncSHA256=b"e",
           FileSHA256=b"s", FileLength=5)


class Client:
    """Only upload and send_message: neonize's build_audio_message (and its
    ffprobe) is not there to be called."""

    def __init__(self):
        self.uploads, self.sent = [], []

    def upload(self, binary, media_type=None):
        self.uploads.append(binary)
        return UPLOAD

    def send_message(self, destination, message):
        self.sent.append((destination, message))
        return N(ID="NEW1", Timestamp=1_700_000_000, Message=message)


def test_build_a_voice_note_from_bytes():
    client = Client()
    m = am.build(client, b"abcde", True, mimetype_of=lambda d: "audio/ogg",
                 seconds_of=lambda d: 7)
    a = m.audioMessage
    assert client.uploads == [b"abcde"]
    assert (a.URL, a.directPath, a.fileLength) == ("https://mmg/x", "/v/x", 5)
    assert a.PTT is True and a.seconds == 7
    assert a.mimetype == "audio/ogg; codecs=opus"


def test_build_an_audio_from_a_path_keeps_its_mimetype(tmp_path):
    path = tmp_path / "a.m4a"
    path.write_bytes(b"m4a!!")
    client = Client()
    m = am.build(client, str(path), False, mimetype_of=lambda d: "audio/mp4",
                 seconds_of=lambda d: 3)
    assert client.uploads == [b"m4a!!"]
    assert m.audioMessage.PTT is False and m.audioMessage.mimetype == "audio/mp4"


# -------------------------------------------------------------- the route
@pytest.fixture
def sessions(monkeypatch):
    created = []

    class Registered(server.Session):
        def __init__(self, *args, **kw):
            super().__init__(*args, **kw)
            created.append(self)
    monkeypatch.setattr(server, "Session", Registered)
    return created


@pytest.mark.asyncio
async def test_send_voice_base64_goes_out_without_ffprobe(tmp_path, sessions, monkeypatch):
    import base64
    monkeypatch.setattr(am, "_mimetype", lambda data: "audio/ogg")
    monkeypatch.setattr(am, "_ffmpeg_seconds", lambda data: pytest.fail("ffmpeg asked"))
    http = TestClient(TestServer(server.create_app(str(tmp_path))))
    await http.start_server()
    try:
        secret = (tmp_path / auth.SECRET_FILE).read_text(encoding="utf-8")
        token = f"account1:{auth.session_token(secret, 'account1')}"
        await http.get(f"/api/{token}/status-session")
        client = Client()
        s = sessions[0]
        s.client, s.status, s.me = client, "CONNECTED", "5500000000@s.whatsapp.net"
        voice = _opus(48000 * 5 + 312)
        response = await http.post(f"/api/{token}/send-voice-base64", json={
            "phone": "5511999990000@c.us",
            "base64Ptt": "data:audio/ogg;base64," + base64.b64encode(voice).decode()})
        assert response.status == 200
        assert (await response.json())["status"] == "success"
        assert client.uploads == [voice]
        sent = client.sent[0][1].audioMessage
        assert sent.PTT is True and sent.seconds == 5
        assert sent.mimetype == "audio/ogg; codecs=opus"
    finally:
        await http.close()
