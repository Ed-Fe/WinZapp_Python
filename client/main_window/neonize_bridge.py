"""NeonizeBridgeMixin -- part of MainWindow (see main_window/__init__.py).

With the Neonize connection engine the client starts the neonize bridge itself
(core/neonize_bridge_process.py) instead of WPPConnect's Node, and the last
client to leave stops it.
"""

import logging
import threading

import wx

from core import connection_engine as engines


class NeonizeBridgeMixin:
    def _uses_neonize(self) -> bool:
        return engines.engine_of(
            getattr(self, "wpp_custom_api", False), getattr(self, "wpp_server", ""),
            getattr(self, "wpp_ws_server", ""), getattr(self, "wpp_port", 0),
        ) == engines.NEONIZE

    def _start_neonize_bridge_if_needed(self):
        """Called at startup, before the UI: start (or find) the bridge and
        wait for its port. A failure is shown once and the app goes on, the
        way it does when the custom server is down."""
        if not self._uses_neonize():
            return
        from core.neonize_bridge_process import NeonizeBridge

        self.neonize_bridge = NeonizeBridge(port=self.wpp_port)
        ok = False
        try:
            ok = self.neonize_bridge.start()
        except Exception:
            logging.exception("MainWindow: starting the neonize bridge failed")
        if ok:
            logging.info("MainWindow: neonize bridge ready on port %s", self.wpp_port)
            return
        logging.error("MainWindow: neonize bridge is not available")
        if not getattr(self, "background_mode", False):
            wx.MessageBox(
                self.i18n.t("neonize_bridge_unavailable_message"),
                self.i18n.t("neonize_bridge_unavailable_title"),
                wx.OK | wx.ICON_ERROR,
            )

    def _stop_neonize_bridge(self):
        """Called at shutdown: this client leaves; the last one out stops it."""
        bridge = getattr(self, "neonize_bridge", None)
        if bridge is None:
            return
        try:
            bridge.stop()
        except Exception:
            logging.exception("[shutdown] stopping the neonize bridge failed (non-fatal)")
        self.neonize_bridge = None
