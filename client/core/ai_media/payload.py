"""What goes to a provider: one bounded, validated media payload of any kind."""
from dataclasses import dataclass

from .config import MAX_SOURCE_BYTES
from .errors import DescriptionError
from .image_input import prepare_image


@dataclass(frozen=True)
class Media:
    kind: str
    data: bytes
    mime: str
    filename: str


# Container formats the providers' audio endpoints accept, by the MIME type
# WhatsApp reports (parameters such as "; codecs=opus" are dropped first).
_AUDIO = {
    "audio/ogg": ("audio/ogg", "audio.ogg"),
    "audio/opus": ("audio/ogg", "audio.ogg"),
    "audio/mpeg": ("audio/mpeg", "audio.mp3"),
    "audio/mp3": ("audio/mpeg", "audio.mp3"),
    "audio/mp4": ("audio/mp4", "audio.m4a"),
    "audio/x-m4a": ("audio/mp4", "audio.m4a"),
    "audio/aac": ("audio/aac", "audio.aac"),
    "audio/wav": ("audio/wav", "audio.wav"),
    "audio/x-wav": ("audio/wav", "audio.wav"),
    "audio/webm": ("audio/webm", "audio.webm"),
    "audio/flac": ("audio/flac", "audio.flac"),
}
_VIDEO = {
    "video/mp4": ("video/mp4", "video.mp4"),
    "video/3gpp": ("video/3gpp", "video.3gp"),
    "video/quicktime": ("video/quicktime", "video.mov"),
    "video/webm": ("video/webm", "video.webm"),
}


def _bare(mime):
    return str(mime or "").split(";")[0].strip().lower()


def prepare_media(kind, data, mime="", profile="balanced"):
    """Validate ``data`` for ``kind`` and return the payload to send.

    Images and stickers are decoded and re-encoded (orientation applied,
    metadata stripped). Audio, video and PDF are sent as they are, after the
    size, container and (for PDF) header checks: re-encoding them locally would
    need codecs WinZapp does not ship.
    """
    if not data or len(data) > MAX_SOURCE_BYTES[kind]:
        raise DescriptionError("media_size")
    if kind in ("image", "sticker"):
        image = prepare_image(data, profile, first_frame=kind == "sticker")
        return Media(kind, image.data, image.mime, "image.jpg")
    if kind == "pdf":
        if not data.startswith(b"%PDF-"):
            raise DescriptionError("media_format")
        return Media(kind, data, "application/pdf", "document.pdf")
    table = _AUDIO if kind == "audio" else _VIDEO
    found = table.get(_bare(mime))
    if found is None:
        raise DescriptionError("media_format")
    return Media(kind, data, *found)
