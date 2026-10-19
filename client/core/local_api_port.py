"""A fixed port for the bundled WPPConnect Server: Settings > Advanced.

By default each account's Node gets a port of its own in
[node_ports.BASE_PORT, +MAX_ACCOUNTS), chosen and kept by
MainWindow._resolve_wpp_port(); a saved port outside that window is dropped.
That leaves no way to move the API off 6300 for good: another program that
already listens there, or a second, separate copy of WinZapp (its own data
folder, so it cannot see this one's accounts), each pick it again.

A fixed port, set per account, wins over that allocation for the bundled API
only (a custom API keeps the Connection page's port). 0 means automatic. It
takes effect the next time the account starts, since the Node of this run is
already listening. Taken by something else when Node is about to start, it
falls back to the automatic choice for that run rather than failing to bind
(MainWindow._ensure_wpp_port_still_free()).
"""

SECTION = "advanced"
KEY = "local_api_port"

#: Below 1024 needs administrator rights on some systems; the Connection
#: page's port has always been held to the same range.
MIN_PORT = 1024
MAX_PORT = 65535


def parse_port(value):
    """The port in *value* (an int or the text of a field), or None when it is
    not a whole number in [MIN_PORT, MAX_PORT]."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value.isdigit():
            return None
        value = int(value)
    if isinstance(value, int) and MIN_PORT <= value <= MAX_PORT:
        return value
    return None


def fixed_port(settings):
    """The fixed port this account asked for, or None for automatic (no
    setting, 0, or a hand-edited value that is not a usable port)."""
    section = settings.get(SECTION) if isinstance(settings, dict) else None
    return parse_port(section.get(KEY)) if isinstance(section, dict) else None
