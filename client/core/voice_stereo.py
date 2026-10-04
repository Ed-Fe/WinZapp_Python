"""Stereo voice messages (issue #82).

WinZapp recorded in mono, or captured two channels and downmixed them in the
encoder (`-ac 1`). A microphone with a real stereo mode (the reporter's HyperX
QuadCast 2) lost its left/right image on the way. Stereo is now a choice: a
default in Settings > Dispositivos de áudio, and a second record button for
the other mode, for one message.

WhatsApp on iPhone cannot play a stereo OGG/Opus message, voice message or
file -- the reason WinZapp's early stereo recordings were removed. It does
play the stereo AAC-LC M4A that microphone + computer audio (Ctrl+Shift+H)
sends as an audio message. So a recording that really is stereo goes out
exactly that way (core/audio_transcode.encode_system_audio_to_m4a through
/send-file, see ConversationsPanel._enqueue_system_audio_file); only a mono
recording is still sent as a voice message (OGG/Opus, push-to-talk).

Stereo is only ever what the microphone really delivered: when it will not
open with two channels the capture falls back to mono and says so, instead of
duplicating one channel into two and calling it stereo.
"""

#: Opus rate of a mono voice message. A stereo recording is AAC, not Opus.
MONO_BITRATE = "64k"


def recording_configs_preferring(configs, stereo: bool) -> list:
    """*configs* ((rate, channels) pairs, in order) with two channels first when
    *stereo* is wanted, keeping the order within each group. Mono stays first
    otherwise, exactly as before -- a mono message costs nothing more to
    capture in mono."""
    configs = list(configs or ())
    if not stereo:
        return configs
    return ([c for c in configs if c[1] >= 2]
            + [c for c in configs if c[1] < 2])


def encode_as_stereo(requested: bool, captured_channels) -> bool:
    """Whether the message goes out in stereo: asked for AND really captured."""
    try:
        return bool(requested) and int(captured_channels or 0) >= 2
    except (TypeError, ValueError):
        return False


def fell_back_to_mono(requested: bool, captured_channels) -> bool:
    """Stereo was asked for but the microphone only opened with one channel."""
    return bool(requested) and not encode_as_stereo(True, captured_channels)


def opus_encode_args() -> list:
    """The ffmpeg arguments of a mono voice message: channel count, codec, bitrate."""
    return ["-ac", "1", "-c:a", "libopus", "-b:a", MONO_BITRATE]


def sends_as_audio_file(stereo: bool) -> bool:
    """A stereo recording goes out as an M4A audio message, never as a voice
    message (push-to-talk)."""
    return bool(stereo)


def alternate_mode_is_stereo(default_stereo: bool) -> bool:
    """The second record button records in the mode the setting did NOT pick."""
    return not bool(default_stereo)


def alternate_record_label_key(default_stereo: bool) -> str:
    return ("record_voice_message_stereo" if alternate_mode_is_stereo(default_stereo)
            else "record_voice_message_mono")
