"""Validation of what arrives on the local port: session name and body file.

Pure functions, tested in test_inputs.py.
"""
import re

from chat_sending import base64_data

_SESSION_NAME = re.compile(r"[A-Za-z0-9_-]{1,128}")


def valid_session_name(name) -> bool:
    """The name becomes a folder in data: no separators, no '..'."""
    return bool(_SESSION_NAME.fullmatch(str(name or "")))


def body_bytes(body) -> bytes:
    """Bytes of the `path` field (base64 data URI or plain base64). Never opens
    a file on disk: the path comes from whoever talks to the local port."""
    return base64_data((body or {}).get("path"))[0]
