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
        engine = getattr(self, "wpp_engine", None)
        if engine in engines.ENGINES:
            return engine == engines.NEONIZE
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

        # The lease is how the other accounts, "Quit all accounts" and the
        # updater know this account's process is running; only the Node path
        # used to write one. _stop_wpp_server() releases it at shutdown.
        self._register_node_lease()
        self.neonize_bridge = NeonizeBridge(port=self.wpp_port)
        ok = False
        try:
            ok = self.neonize_bridge.start()
        except Exception:
            logging.exception("MainWindow: starting the neonize bridge failed")
        if ok:
            logging.info("MainWindow: neonize bridge ready on port %s", self.wpp_port)
            self._use_neonize_bridge_secret()
            self._finish_pending_deletions_on_bridge()
            return
        logging.error("MainWindow: neonize bridge is not available")
        if not getattr(self, "background_mode", False):
            wx.MessageBox(
                self.i18n.t("neonize_bridge_unavailable_message"),
                self.i18n.t("neonize_bridge_unavailable_title"),
                wx.OK | wx.ICON_ERROR,
            )

    def _use_neonize_bridge_secret(self):
        """The bridge only answers to its own secret. It stands in for the API key
        for this run only: it is never written to the settings."""
        from core.neonize_bridge_process import read_secret

        secret = read_secret(self.neonize_bridge.bridge)
        if secret:
            self.wpp_api_key = secret
        else:
            logging.error("MainWindow: the neonize bridge has no secret file; a bridge "
                          "started by hand must use the same --pasta as WinZapp")

    def _neonize_session_deleter(self):
        """``name -> bool`` that deletes a session's data on the bridge
        (account_removal.py), or None when this process is not on Neonize."""
        if not self._uses_neonize():
            return None
        from core.neonize_session_data import bridge_deleter

        return bridge_deleter(f"{self.wpp_server}:{self.wpp_port}", self.wpp_api_key)

    def _finish_pending_deletions_on_bridge(self):
        """Accounts left 'deleting' whose data is on the bridge could not be
        finished before it was running: retry now, off the UI thread (logging
        a device out of WhatsApp is a network round trip)."""
        registry = getattr(self, "registry", None)
        if registry is None:
            return

        def _finish():
            try:
                from account_removal import finish_pending_deletions
                finish_pending_deletions(
                    registry, self.global_dir, self._neonize_session_deleter())
            except Exception:
                logging.exception("MainWindow: finishing pending removals on the bridge failed")

        threading.Thread(target=_finish, name="neonize-pending-deletions",
                         daemon=True).start()

    def _cleanup_abandoned_bridge_sessions(self):
        """The abandoned-session cleanup (session_tokens.py) for the Neonize
        engine, which has no browser profile to delete: each pairing attempt
        or re-pairing left a folder in the bridge's data (the WhatsApp keys and
        every message), and the device may still be linked on the phone. The
        bridge removes it (``delete-session``); the store row goes only once it
        confirmed, so a refusal is retried at the next start. Runs on the
        cleanup worker, never the UI thread."""
        from coord_locks import LockTimeout, sessions_lock
        import session_store

        deleter = self._neonize_session_deleter()
        store = self._get_session_store()
        current = (getattr(self, "token", "") or "").split(":")[0]
        gd = getattr(self, "global_dir", None)
        if deleter is None or store is None or not current or not gd:
            return      # no confirmed current session: delete nothing
        try:
            with sessions_lock(gd):
                try:
                    protected = self._protected_session_names()    # fail-closed
                except Exception:
                    logging.exception("[sessions] a sessions.json is unreadable — "
                                      "aborting the bridge cleanup (fail-closed)")
                    return
                protected.add(current)
                for stale in session_store.sessions_to_close(store.list(), current):
                    name = stale.get("name") or ""
                    if not name or name in protected:
                        continue
                    if deleter(name):
                        store.remove(name)
                        logging.info("[sessions] cleaned abandoned bridge session %s", name[:12])
        except LockTimeout:
            logging.info("[sessions] sessions_lock busy — skipping the bridge cleanup this start")

    def _stop_neonize_bridge(self, force=False):
        """Called at shutdown: this client leaves; the last one out stops it.

        Removing the account this window runs (Accounts > Manage > Delete on the
        only account) is still to come once the session is closed, and it asks
        the bridge to delete the account's data: the bridge stays up until
        ``_purge_account_if_requested`` has run and stops it with ``force``."""
        bridge = getattr(self, "neonize_bridge", None)
        if bridge is None:
            return
        if getattr(self, "_purge_account_on_exit", None) and not force:
            return
        try:
            bridge.stop()
        except Exception:
            logging.exception("[shutdown] stopping the neonize bridge failed (non-fatal)")
        self.neonize_bridge = None
