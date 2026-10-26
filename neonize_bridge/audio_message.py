"""Audio messages without ffprobe.

neonize's `build_audio_message` uploads the file and then runs `ffprobe` only to
fill `audioMessage.seconds`; WinZapp does not ship ffprobe, so every voice note
failed after the upload. `build` makes the same message with the duration from
media_probe.py.

Tested in test_audio_message.py.
"""
import media_probe


def build(client, file, ptt, mimetype_of=None, seconds_of=None):
    """The audio Message neonize's `build_audio_message` would build, minus ffprobe.
    `file` is a path or the bytes; the upload goes through `client.upload`, so the
    progress client of transfers.py still sees it."""
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import AudioMessage, Message
    data = media_probe.read(file)
    upload = client.upload(data)
    mimetype = (mimetype_of or media_probe.mimetype)(data) or ""
    # neonize only detects "audio/ogg"; WhatsApp wants "audio/ogg; codecs=opus"
    # (without it the voice note may not even show up on some devices)
    if mimetype.startswith("audio/ogg"):
        mimetype = "audio/ogg; codecs=opus"
    return Message(audioMessage=AudioMessage(
        URL=upload.url, directPath=upload.DirectPath, mediaKey=upload.MediaKey,
        fileEncSHA256=upload.FileEncSHA256, fileSHA256=upload.FileSHA256,
        fileLength=upload.FileLength, seconds=(seconds_of or media_probe.duration_seconds)(data),
        mimetype=mimetype, PTT=ptt))
