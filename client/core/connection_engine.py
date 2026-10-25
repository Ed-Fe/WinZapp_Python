"""The connection engine: how WinZapp reaches WhatsApp.

Three ways, all spoken to through the same WPPConnect-shaped REST/Socket.IO
API, so the rest of the client does not care which one is behind it:

* ``wppconnect`` -- the bundled WPPConnect server (Node + a browser). Default.
* ``neonize``    -- the neonize bridge (``neonize_bridge/``), a direct
  connection without a browser. It answers on a fixed local address.
* ``custom``     -- any WPPConnect-compatible server the user types in.

``connection.wpp_custom_api`` stays the switch the rest of the client reads
(``neonize`` and ``custom`` both turn it on). No new setting is stored: the
neonize bridge is a WPPConnect-compatible server at a fixed local address, so
"custom API pointing at that address" *is* the neonize engine, and the picker
only shows and sets those same keys. Pure functions, no wx.
"""

WPPCONNECT = "wppconnect"
NEONIZE = "neonize"
CUSTOM = "custom"
ENGINES = (WPPCONNECT, NEONIZE, CUSTOM)

# i18n key of each engine's name, in the order the pickers list them.
LABEL_KEYS = {
    WPPCONNECT: "connection_engine_wppconnect",
    NEONIZE: "connection_engine_neonize",
    CUSTOM: "connection_engine_custom",
}

NEONIZE_SERVER = "http://127.0.0.1"
NEONIZE_WS_SERVER = "ws://127.0.0.1"
NEONIZE_PORT = 6300


def engine_of(custom_api: bool, server: str, ws_server: str, port) -> str:
    """The engine the connection settings stand for."""
    if not custom_api:
        return WPPCONNECT
    try:
        port = int(port)
    except (TypeError, ValueError):
        return CUSTOM
    if fixed_address(NEONIZE) == ((server or "").strip().rstrip("/"),
                                  (ws_server or "").strip().rstrip("/"), port):
        return NEONIZE
    return CUSTOM


def uses_custom_api(engine: str) -> bool:
    """Whether the client talks to a server it did not start itself."""
    return engine != WPPCONNECT


def fixed_address(engine: str):
    """(server, websocket server, port) the engine always uses, or None when
    the user (custom) or the bundled server (wppconnect) decides."""
    if engine == NEONIZE:
        return NEONIZE_SERVER, NEONIZE_WS_SERVER, NEONIZE_PORT
    return None


def addresses_editable(engine: str) -> bool:
    """Only the custom engine lets the user type the server, websocket,
    port and key."""
    return engine == CUSTOM


def index_of(engine: str) -> int:
    return ENGINES.index(engine) if engine in ENGINES else 0


def engine_at(index: int) -> str:
    return ENGINES[index] if 0 <= index < len(ENGINES) else WPPCONNECT


def needs_restart(loaded: str, chosen: str) -> bool:
    """Changing the engine only takes effect after a restart."""
    return chosen != loaded
