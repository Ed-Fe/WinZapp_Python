"""Audio messages without ffprobe.

neonize's `build_audio_message` uploads the file and then runs `ffprobe` only to
fill `audioMessage.seconds`. WinZapp ships `ffmpeg.exe` (client/lib) but not
ffprobe, so every voice note failed with FileNotFoundError after the upload.

Here the duration comes from the file itself: Ogg (the voice note WinZapp
records is Ogg/Opus) by the last page's granule position, MP4/M4A (stereo voice
and converted attachments) by the `mvhd` box. Any other format asks the bundled
`ffmpeg -i`, whose stderr prints "Duration: ..."; when that is missing too the
message goes out with 0 seconds instead of failing.

Tested in test_audio_message.py.
"""
import logging
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile

log = logging.getLogger("bridge")

OPUS_RATE = 48000                    # an Opus granule position always counts at 48 kHz
_FFMPEG_DURATION = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")


def ogg_seconds(data):
    """Duration of an Ogg Opus/Vorbis file in seconds, or None if it is not one."""
    if not data.startswith(b"OggS") or len(data) < 28:
        return None
    first_payload = 27 + data[26]
    head = data[first_payload:first_payload + 30]
    if head.startswith(b"OpusHead") and len(head) >= 12:
        rate, skip = OPUS_RATE, struct.unpack_from("<H", head, 10)[0]
    elif head.startswith(b"\x01vorbis") and len(head) >= 16:
        rate, skip = struct.unpack_from("<I", head, 12)[0], 0
    else:
        return None
    end = len(data)
    while rate > 0:
        start = data.rfind(b"OggS", 0, end)
        if start < 0 or start + 14 > len(data):
            return None
        granule = struct.unpack_from("<q", data, start + 6)[0]
        if granule >= 0:          # -1: a page where no packet ends; look further back
            return max(granule - skip, 0) / rate
        end = start
    return None


def _boxes(data, start, end):
    """(type, payload start, payload end) of the MP4 boxes between start and end."""
    while start + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, start)
        header = 8
        if size == 1 and start + 16 <= end:
            size, header = struct.unpack_from(">Q", data, start + 8)[0], 16
        elif size == 0:
            size = end - start
        if size < header:
            return
        yield kind, start + header, min(start + size, end)
        start += size


def mp4_seconds(data):
    """Duration of an MP4/M4A file in seconds (moov/mvhd), or None."""
    for kind, start, end in _boxes(data, 0, len(data)):
        if kind != b"moov":
            continue
        for inner, body, body_end in _boxes(data, start, end):
            if inner != b"mvhd" or body_end - body < 20:
                continue
            if data[body] == 1:
                if body_end - body < 32:
                    return None
                scale, length = struct.unpack_from(">IQ", data, body + 20)
            else:
                scale, length = struct.unpack_from(">II", data, body + 12)
            return length / scale if scale else None
    return None


def ffmpeg_output_seconds(stderr):
    """Seconds from the "Duration: HH:MM:SS.xx" line of `ffmpeg -i`, or None."""
    found = _FFMPEG_DURATION.search(stderr or "")
    if not found:
        return None
    hours, minutes, seconds = found.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _ffmpeg_seconds(data):
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return None
    fd, path = tempfile.mkstemp(suffix=".audio")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        result = subprocess.run([ffmpeg, "-hide_banner", "-i", path], capture_output=True,
                                timeout=30, creationflags=flags)
        return ffmpeg_output_seconds(result.stderr.decode("utf-8", "replace"))
    except (OSError, subprocess.SubprocessError):
        log.exception("audio duration via ffmpeg")
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def duration_seconds(data, ffmpeg_seconds=None):
    """Whole seconds of the audio in `data`; 0 when nothing could measure it."""
    seconds = ogg_seconds(data)
    if seconds is None:
        seconds = mp4_seconds(data)
    if seconds is None:
        seconds = (ffmpeg_seconds or _ffmpeg_seconds)(data)
    if seconds is None:
        log.info("audio duration unknown: sent with 0 seconds")
        return 0
    return int(round(seconds))


def _mimetype(data):
    import magic
    return magic.from_buffer(data, mime=True)


def build(client, file, ptt, mimetype_of=None, seconds_of=None):
    """The audio Message neonize's `build_audio_message` would build, minus ffprobe.
    `file` is a path or the bytes; the upload goes through `client.upload`, so the
    progress client of transfers.py still sees it."""
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import AudioMessage, Message
    if isinstance(file, (bytes, bytearray)):
        data = bytes(file)
    else:
        with open(file, "rb") as f:
            data = f.read()
    upload = client.upload(data)
    mimetype = (mimetype_of or _mimetype)(data) or ""
    # neonize only detects "audio/ogg"; WhatsApp wants "audio/ogg; codecs=opus"
    # (without it the voice note may not even show up on some devices)
    if mimetype.startswith("audio/ogg"):
        mimetype = "audio/ogg; codecs=opus"
    return Message(audioMessage=AudioMessage(
        URL=upload.url, directPath=upload.DirectPath, mediaKey=upload.MediaKey,
        fileEncSHA256=upload.FileEncSHA256, fileSHA256=upload.FileSHA256,
        fileLength=upload.FileLength, seconds=(seconds_of or duration_seconds)(data),
        mimetype=mimetype,
        PTT=ptt))
