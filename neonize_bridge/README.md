# Neonize bridge (experimental)

A second way for WinZapp to reach WhatsApp, without a browser. The bridge is a
small local server that speaks the same REST and Socket.IO API as WPPConnect
Server, but connects to WhatsApp through [neonize](https://github.com/krypton-byte/neonize)
(whatsmeow). The client does not know the difference: the Neonize engine is a
custom API pointing at `http://127.0.0.1:6300`.

WPPConnect stays the default. Nothing changes unless you pick **Neonize** in
Settings, Connection (or in the question asked on the first launch).

## Compiled builds

A compiled WinZapp (the installer and the portable zip) already carries the
bridge, frozen as `neonize_bridge/neonize_bridge.exe` inside the install, next
to WPPConnect (`node/` and `api/`): both engines are installed together and the
user picks one in Settings, Connection. The bridge keeps its sessions and
messages in `data/global/neonize_bridge/data/` of the install, which updates do
not replace, so each engine keeps its own connection when you switch back and
forth. `build.py` freezes the bridge with the PyInstaller it uses itself (it runs
`prepare.py` first when `neonize_bridge/.venv` is missing); `--skip-bridge` leaves
it out of a quick local build.

## Try it from a repository checkout

```
python neonize_bridge/prepare.py
```

This creates `neonize_bridge/.venv`, installs `requirements.txt` and the test
build of neonize 0.5.2 that the bridge needs (see below). Then open WinZapp,
go to Settings, Connection, choose **Neonize (direct connection,
experimental)**, press OK and restart WinZapp. WinZapp starts the bridge by
itself; the last WinZapp window to close stops it. Sessions and messages live
in `neonize_bridge/data/`, the bridge output in `neonize_bridge/run/bridge.log`.
Pairing works by phone code; the QR code is not reliable yet.

Voice notes, video and stickers need `ffmpeg` and `ffprobe` on the bridge's
`PATH`. `client/lib` is added automatically; to add other folders, list them
in `neonize_bridge/path_extra.txt` (one per line, ignored by git).

## Who can talk to the bridge

The bridge listens on 127.0.0.1 only, but a web page open in your browser can
also reach 127.0.0.1, so it does not trust the network. It creates a secret the
first time it runs, in `neonize_bridge/data/secret` (readable by your
user only on systems that support it). WinZapp reads that file and uses the
secret instead of the API key: `generate-token` answers only to it, every
session token is a keyed hash of the session name, the Socket.IO connection
needs a valid token, and requests whose `Host` is not this machine are refused.
A bridge you start by hand must use the same `--folder` as WinZapp, or the
client cannot find the secret. Deleting the `secret` file
makes the bridge create a new one at the next start; WinZapp then renews its
session token on its own.

## Why a test build of neonize

The bridge listens to what the phone does (archive, pin, mute, star, lock,
read, delete) instead of polling for it. Those events, and a few functions to
repair the app state, are open pull requests to neonize (#227 to #232). Until
they are merged, `prepare.py` installs a build that has them from
https://github.com/reffehcsoibaf/neonize/releases/tag/0.5.2-winzapp-teste.2.
`prepare.py` checks the SHA-256 of the download and also what is inside it: it
only extracts files under `neonize/` with the types the package has (`.py`,
`.pyi`, `.pyc`, `.dll`, `.typed`), refuses links, paths that leave the folder,
a `.pth` or `sitecustomize.py`, and archives larger than expected. The hash and
these limits do not tell you what the DLL does: someone has to compare it with
the source linked below before you trust it with a WhatsApp session.
The Go sources of the changes are in the pull requests and in
https://github.com/reffehcsoibaf/TeleZapp/tree/main/docs/neonize.

## What works

Tested on a real account: pairing by phone code, chats, contacts and history;
text, photo, voice note and document sending and receiving; replies, mentions,
forwarding, editing, reactions, polls and pinned messages; seeing status
updates and opening their photos; read receipts both ways; archive, pin, star,
mute and delete, kept in step with the phone.

## What is untested or missing

Untested: video, sticker and GIF sending and receiving, posting or deleting
your own status, and every route added in the second round (chats and messages,
profile and privacy, contacts, labels, communities, group settings, channels,
call rejection, server management): they pass their tests against stubs but
have not run against a real account. Missing: placing, answering or carrying
calls (whatsmeow cannot), business and catalog features (neonize has no
catalog messages), QR pairing, and some WPPConnect-only routes: the bridge
implements 158 of the 220 that WPPConnect Server defines for WinZapp.
[COVERAGE.md](COVERAGE.md) lists every route, grouped by area. Muting and
deleting for everyone done by someone else can take up to a minute to show.
Data is not encrypted at rest. Treat it as a preview.

## Development

```
neonize_bridge/.venv/Scripts/python -m pytest neonize_bridge
neonize_bridge/.venv/Scripts/python neonize_bridge/server.py --port 6300 --folder neonize_bridge/data
```

`--log-state` turns on a technical log of app-state events.
