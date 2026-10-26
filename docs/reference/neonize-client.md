# The client on the Neonize engine

What the client assumes when `wpp_engine` is `neonize` (a "custom API" client
pointed at the local bridge in `neonize_bridge/`: no Node, no Chrome). The
bridge's own routes are in `neonize_bridge/COVERAGE.md`. With no Chrome there
is no CDP: inspecting a running bridge is in `docs/reference/live-inspection.md`.
The neonize the bridge runs is built from source with WinZapp's patches:
`docs/reference/neonize-build.md`.

## The engine is a setting

`connection.wpp_engine` (`wppconnect`, `neonize` or `custom`) is install-wide
(`app_settings._CONNECTION_GLOBAL`). It used to be inferred from
(server, websocket, port 6300) by `core/connection_engine.engine_of()`, but the
port is per account: after one account switched the install to Neonize, an
account on an automatic port read as "custom" and started no bridge, and a real
WPPConnect typed in at `127.0.0.1:6300` read as Neonize.

- `engine_from_connection()` believes the stored value only while it agrees
  with `wpp_custom_api`; otherwise it infers (installs from before the setting,
  hand-edited files). `_apply_global_settings()` stores the inferred value once,
  from the first account to start.
- `MainWindow.wpp_engine` is the engine this process started with. A change
  (Settings, import) needs a restart; `_uses_neonize()` reads it.
- With Neonize every account's port is the bridge's (`_resolve_wpp_port`).
- Settings export/import never carry the engine of a bundled or Neonize
  install, only a custom server (`settings_transfer.ENGINE_KEYS`); an import
  that would change the engine is saved, not applied, and asks for a restart.

## Things that do not apply, and what stops them

| WPPConnect machinery | With Neonize |
| --- | --- |
| WPPConnect update checker, Help > Force WPPConnect reinstall, `_update_wpp_server` | skipped (`profile_is_local`) |
| Chrome profile, snapshots, orphan Chrome kill | skipped (`profile_is_local`) |
| Orphan-session sweep at shutdown | skipped: the bridge answers 401 to name-only calls and its sessions are the other accounts' |
| Abandoned-session cleanup | asks the bridge to `delete-session` each stale name |
| Removing an account | asks the bridge to `delete-session` each session name; the account stays `deleting` until it confirms (`account_removal.purge_account(bridge_delete=...)`) |
| Node lease | registered when the bridge is started or joined (`_register_node_lease`), so "Quit all accounts" and the updater see the account |
| Outgoing/incoming calls | refused up front (the bridge answers 501); rejecting still works |

## Known leftovers (not fixed)

- Voice and video call buttons and menu items are still shown; they answer with
  "This action is not available for calls." A capability flag from
  `send-capabilities` (`calls: false`) would let the client hide them.
- `delete-session` logs the device out only if the session is connected on the
  bridge at that moment. The account removed while in use is closed first, so
  its device can stay linked on the phone (remove it there).
- Texts that send the user to Help > Force WPPConnect reinstall
  (`voice_call_voip_unavailable`, `clear_chat_starred_kept_on_phone`) are
  misleading with Neonize.
- Custom-list filters and buttons are always shown; the bridge answers 404 and
  the client says "unavailable" only after the click.
- Events the bridge never emits (announcement-replies, media-*-progress,
  `call:*:remote`, messages.update, presence.update) are harmless.
- `reconnect-socket-stream` answering 400 counts strikes towards
  `_restart_wpp_session` (`connection.py`); the bridge side is F1's.
- `_start_wpp_update_checker` with no `client/api/` folder is now skipped for
  Neonize; for WPPConnect it still schedules a retry every 12 h.
- Settings dialog: choosing another engine still sets `main_window.wpp_custom_api`
  live (before the restart the dialog asks for); only `wpp_engine` waits.
- Name-only calls (`close-session`, `check-connection-session`, `logout-session`
  with `Authorization: Bearer ...`) depend on the bridge accepting that
  credential (F1). `ui/dialogs/connect.py` line ~291 and
  `session_tokens._logout_abandoned_session` send the *signature half* of the
  token as the Bearer, not the bridge secret; the other sites send the global
  key (the secret under Neonize).
- The packaged build does not know the bridge yet (`build.py`, the updater's
  `xcopy`, `own_node_pids` only handles `node.exe`).
