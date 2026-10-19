"""Pure call-state helpers shared by the wx UI and headless tests."""


def incoming_call_can_answer(details: dict | None) -> bool:
    """Return whether this one-to-one incoming call can be answered."""
    return not bool((details or {}).get("is_group"))


def active_call_label_key(active: dict | None) -> str:
    """Return the label for an active individual call."""
    return "video_call_active_label" if (active or {}).get("is_video") else "voice_call_active_label"


# Windows RegisterHotKey modifier bits and virtual-key codes.
_MOD_ALT, _MOD_CONTROL, _MOD_SHIFT = 0x0001, 0x0002, 0x0004
_VK_BACK, _VK_RETURN = 0x08, 0x0D

# System-wide keys that answer, and reject / hang up, from any program. They
# are changeable in Settings > Keyboard shortcuts; these are the shipped keys.
# Ctrl+Alt+Shift matches the app's other all-windows commands and avoids
# AltGr (Ctrl+Alt) letters, which type characters on several keyboard layouts.
CALL_ANSWER_HOTKEY = (_VK_RETURN, _MOD_CONTROL | _MOD_ALT | _MOD_SHIFT)
CALL_END_HOTKEY = (_VK_BACK, _MOD_CONTROL | _MOD_ALT | _MOD_SHIFT)

#: general.<key> -> shipped (vk, mod). A stored {"vk": 0, "mod": 0} means the
#: user removed the shortcut.
CALL_HOTKEY_DEFAULTS = {
    "call_answer_hotkey": CALL_ANSWER_HOTKEY,
    "call_end_hotkey": CALL_END_HOTKEY,
}


def call_hotkey_is_valid(value) -> bool:
    """A key with Ctrl or Alt (as for the "show WinZapp" key), or the
    {"vk": 0, "mod": 0} that records a removed shortcut."""
    if not isinstance(value, dict):
        return False
    vk, mod = value.get("vk"), value.get("mod")
    if isinstance(vk, bool) or isinstance(mod, bool):
        return False
    if not isinstance(vk, int) or not isinstance(mod, int):
        return False
    if vk == 0 and mod == 0:
        return True
    if not 0 < vk < 256:
        return False
    if mod & ~(_MOD_ALT | _MOD_CONTROL | _MOD_SHIFT):
        return False
    return bool(mod & (_MOD_CONTROL | _MOD_ALT))


def call_hotkey_setting(general, key):
    """The (vk, mod) saved under ``general[key]``; the shipped key when nothing
    usable is saved; None once the user removed the shortcut."""
    value = (general or {}).get(key)
    if not call_hotkey_is_valid(value):
        return CALL_HOTKEY_DEFAULTS[key]
    if not value["vk"]:
        return None
    return value["vk"], value["mod"]


def first_answerable_call(active_calls, details_map) -> str:
    """Return the identity of the oldest ringing call that can be answered.

    A group offer rings too, but cannot be answered, so it must not be the
    call a global "answer" key acts on when a one-to-one call rings beside it.
    """
    for identity in active_calls or ():
        if incoming_call_can_answer((details_map or {}).get(identity)):
            return identity
    return ""
