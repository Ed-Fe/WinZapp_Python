"""Rules behind File > WhatsApp settings > Privacy
(ui/dialogs/whatsapp_settings_dialog.py).

These settings live on the WhatsApp account, not in WinZapp: they are read and
written through the local server (main_window/whatsapp_privacy.py, which calls
the ``/privacy`` routes in api_patches). No wx here, so all of it is tested
without opening a window.

Two kinds of setting:

* Six account settings, each one value from a short list (last seen, online,
  About, profile photo, read receipts, who can add me to groups).
* Who can see my Status: my contacts, my contacts except some, or only some.
  The "some" is a list of people picked from the contacts WinZapp already
  synchronised with WhatsApp (ui/dialogs/status_audience_dialog.py).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PrivacyField:
    """One account-wide privacy setting.

    ``setting`` is the name the server and WPP.privacy use; ``options`` pairs
    the raw value WhatsApp expects with the i18n key of the text shown for it.
    """
    setting: str
    label_key: str
    options: tuple


_EVERYONE = ("all", "wa_privacy_opt_all")
_CONTACTS = ("contacts", "wa_privacy_opt_contacts")
_NOBODY = ("none", "wa_privacy_opt_none")

PRIVACY_FIELDS = (
    PrivacyField("lastSeen", "wa_privacy_last_seen_label",
                 (_EVERYONE, _CONTACTS, _NOBODY)),
    PrivacyField("online", "wa_privacy_online_label",
                 (_EVERYONE, ("match_last_seen", "wa_privacy_opt_match_last_seen"))),
    PrivacyField("about", "wa_privacy_about_label",
                 (_EVERYONE, _CONTACTS, _NOBODY)),
    PrivacyField("profilePicture", "wa_privacy_profile_pic_label",
                 (_EVERYONE, _CONTACTS, _NOBODY)),
    PrivacyField("readReceipts", "wa_privacy_read_receipts_label",
                 (("all", "wa_privacy_opt_on"), ("none", "wa_privacy_opt_off"))),
    PrivacyField("groupAdd", "wa_privacy_group_add_label",
                 (_EVERYONE, _CONTACTS)),
)


def option_index(field: PrivacyField, current) -> int:
    """Position of *current* among the field's options, or -1 when WhatsApp
    answered something this list does not know (a value added after this was
    written, or "my contacts except…", which WhatsApp stores as its own value):
    the dropdown is then left unselected instead of guessing."""
    for index, (value, _label_key) in enumerate(field.options):
        if value == current:
            return index
    return -1


def unreadable(settings) -> bool:
    """True when WhatsApp answered but no account setting holds a value this
    dialog knows.

    That is not an error to the server (it said 200), so without this check the
    dialog would show six empty dropdowns and look as if it worked. It happens
    when wa-js can reach the privacy call but not the data behind it, e.g. after
    WhatsApp Web changes an internal module.
    """
    if not isinstance(settings, dict):
        return True
    return all(
        option_index(field, settings.get(field.setting)) == -1
        for field in PRIVACY_FIELDS
    )


def unrecognized_fields(settings: dict) -> list:
    """The fields whose current value has no option here. Their dropdowns stay
    empty and are left alone on apply; the dialog names them so an empty
    dropdown is never mistaken for "not loaded"."""
    return [
        field for field in PRIVACY_FIELDS
        if option_index(field, settings.get(field.setting)) == -1
    ]


def changed_privacy(loaded: dict, chosen: dict) -> list:
    """(setting, value) pairs to send, only for what actually changed.

    *loaded* is what WhatsApp reported; *chosen* maps a setting to the index
    picked in its dropdown (-1 or missing = never set, so left alone). Sending
    only differences keeps the number of calls — and of possible failures — to
    what the person changed.
    """
    changes = []
    for field in PRIVACY_FIELDS:
        index = chosen.get(field.setting, -1)
        if not 0 <= index < len(field.options):
            continue
        value = field.options[index][0]
        if loaded.get(field.setting) != value:
            changes.append((field.setting, value))
    return changes


def not_confirmed(requested: list, readback) -> list:
    """The settings among *requested* that WhatsApp does not report back as set.

    *requested* is the (setting, value) list that was sent, *readback* a fresh
    read of the account settings afterwards. A setter can answer "success" and
    still do nothing (the server only knows the call returned), so the only
    proof is reading the value again. An unreadable *readback* confirms
    nothing: every setting is returned.
    """
    if unreadable(readback):
        return [setting for setting, _value in requested]
    return [
        setting for setting, value in requested
        if readback.get(setting) != value
    ]


# ── Who can see my Status ────────────────────────────────────────────────────

STATUS_CONTACTS = "contact"
STATUS_EXCEPT = "deny-list"
STATUS_ONLY = "allow-list"

STATUS_OPTIONS = (
    (STATUS_CONTACTS, "wa_privacy_opt_contacts"),
    (STATUS_EXCEPT, "wa_privacy_status_opt_except"),
    (STATUS_ONLY, "wa_privacy_status_opt_only"),
)

# Other spellings WhatsApp has used for the same three settings.
_STATUS_ALIASES = {
    "contact": STATUS_CONTACTS,
    "contacts": STATUS_CONTACTS,
    "deny-list": STATUS_EXCEPT,
    "denylist": STATUS_EXCEPT,
    "blacklist": STATUS_EXCEPT,
    "contact_blacklist": STATUS_EXCEPT,
    "allow-list": STATUS_ONLY,
    "allowlist": STATUS_ONLY,
    "whitelist": STATUS_ONLY,
}


@dataclass(frozen=True)
class StatusPrivacy:
    """Who can see my Status, as WhatsApp reported it.

    ``people`` is None when the setting was read but its list was not: the
    list then cannot be shown, and is only ever replaced on purpose.
    """
    setting: str
    people: "tuple | None"


def parse_status_privacy(raw) -> "StatusPrivacy | None":
    """The server's ``status`` answer, or None when it could not be read or
    holds a setting this dialog does not know (the Status choice is then left
    out, and the six account settings still work)."""
    if not isinstance(raw, dict):
        return None
    setting = _STATUS_ALIASES.get(str(raw.get("setting", "")).strip().lower())
    if setting is None:
        return None
    people = raw.get("list")
    if setting == STATUS_CONTACTS:
        return StatusPrivacy(setting, ())
    if not isinstance(people, list):
        return StatusPrivacy(setting, None)
    return StatusPrivacy(setting, tuple(p for p in people if isinstance(p, str) and p))


def status_option_index(setting: str) -> int:
    for index, (value, _label_key) in enumerate(STATUS_OPTIONS):
        if value == setting:
            return index
    return -1


def jid_for_status_list(jid: str) -> str:
    """The id WhatsApp's status list takes for a contact: a phone JID in the
    ``@c.us`` form (WinZapp stores ``@s.whatsapp.net``), an ``@lid`` unchanged,
    anything else (groups, broadcasts, devices) is not a person: ''."""
    jid = (jid or "").strip()
    if "@" not in jid:
        return ""
    local, domain = jid.split("@", 1)
    local = local.split(":", 1)[0]
    if not local.isdigit():
        return ""
    if domain in ("s.whatsapp.net", "c.us"):
        return f"{local}@c.us"
    if domain == "lid":
        return f"{local}@lid"
    return ""


def status_change(loaded: "StatusPrivacy | None", setting: str,
                  people: "list | None") -> "tuple | None":
    """(setting, list of JIDs) to send, or None when nothing changed.

    *people* is the list chosen in the dialog, or None when the person never
    opened the contact picker — then the list WhatsApp already holds is kept
    (sent again unchanged when only the setting changes). "My contacts" never
    sends a list.
    """
    if setting == STATUS_CONTACTS:
        if loaded is not None and loaded.setting == STATUS_CONTACTS:
            return None
        return (STATUS_CONTACTS, [])
    if people is None:
        if loaded is not None and loaded.setting == setting:
            return None
        # Switching setting without picking anyone: carry the current list
        # over only when it is the same kind of list.
        people = []
    wanted = sorted({j for j in (jid_for_status_list(p) for p in people) if j})
    if loaded is not None and loaded.setting == setting and loaded.people is not None:
        current = sorted({j for j in (jid_for_status_list(p) for p in loaded.people) if j})
        if current == wanted:
            return None
    return (setting, wanted)


def status_needs_people(change: "tuple | None") -> bool:
    """"Only share with" nobody would hide every Status from everyone: WhatsApp
    itself asks for at least one person, so the dialog does too."""
    return bool(change) and change[0] == STATUS_ONLY and not change[1]


def status_not_confirmed(requested: str, reported) -> bool:
    """True when WhatsApp, read back after the change, does not report the
    Status setting that was sent (or reports nothing at all)."""
    parsed = parse_status_privacy({"setting": reported}) if reported else None
    return parsed is None or parsed.setting != requested
