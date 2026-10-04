"""macOS compatibility layer for WinZapp.

Everything here is installed by macos/launcher.py before WinZapp's own
client/main.py is imported, so upstream files need as few Mac-specific
edits as possible and upstream merges stay clean.
"""

import sys


def install():
    if sys.platform != "darwin":
        return
    from . import paths_mac
    paths_mac.install()  # first: other modules bind app_paths names on import
    from . import accessibility_mac, audio_mac, autostart_mac, camera_mac, combo_mac, diagnostics_mac, focus_mac, hotkey_mac, keymap_mac, lifecycle_mac, listctrl, menubar_mac, notify_mac, platform_mac, server_mac, sound_mac, speech, spell_mac, strings_mac, updater_mac
    diagnostics_mac.install()
    accessibility_mac.install()
    listctrl.install()
    combo_mac.install()
    speech.install()
    autostart_mac.install()
    sound_mac.install()
    audio_mac.install()
    keymap_mac.install()
    menubar_mac.install()
    strings_mac.install()
    notify_mac.install()
    lifecycle_mac.install()
    server_mac.install()
    platform_mac.install()
    spell_mac.install()
    camera_mac.install()
    hotkey_mac.install()
    focus_mac.install()
    updater_mac.install()
