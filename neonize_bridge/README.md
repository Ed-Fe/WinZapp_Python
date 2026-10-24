# Neonize bridge (experimental)

A second way for WinZapp to reach WhatsApp, without a browser. The bridge is a
small local server that speaks the same REST and Socket.IO API as WPPConnect
Server, but connects to WhatsApp through [neonize](https://github.com/krypton-byte/neonize)
(whatsmeow). The client does not know the difference: the Neonize engine is a
custom API pointing at `http://127.0.0.1:6300`.

WPPConnect stays the default. Nothing changes unless you pick **Neonize** in
Settings, Connection (or in the question asked on the first launch).

## Try it

```
python neonize_bridge/preparar.py
```

This creates `neonize_bridge/.venv`, installs `requirements.txt` and the test
build of neonize 0.5.2 that the bridge needs (see below). Then open WinZapp,
go to Settings, Connection, choose **Neonize (direct connection,
experimental)**, press OK and restart WinZapp. WinZapp starts the bridge by
itself; the last WinZapp window to close stops it. Sessions and messages live
in `neonize_bridge/dados-real/`, the bridge output in `neonize_bridge/execucao/ponte.log`.
Pairing works by phone code; the QR code is not reliable yet.

Voice notes, video and stickers need `ffmpeg` and `ffprobe` on the bridge's
`PATH`. `client/lib` is added automatically; to add other folders, list them
in `neonize_bridge/path_extra.txt` (one per line, ignored by git).

## Why a test build of neonize

The bridge listens to what the phone does (archive, pin, mute, star, lock,
read, delete) instead of polling for it. Those events, and a few functions to
repair the app state, are open pull requests to neonize (#227 to #232). Until
they are merged, `preparar.py` installs a build that has them from
https://github.com/reffehcsoibaf/neonize/releases/tag/0.5.2-winzapp-teste.1.
The Go sources of the changes are in the pull requests and in
https://github.com/reffehcsoibaf/TeleZapp/tree/main/docs/neonize.

## What works

Tested on a real account: pairing by phone code, chats, contacts and history;
text, photo, voice note and document sending and receiving; replies, mentions,
forwarding, editing, reactions, polls and pinned messages; seeing status
updates and opening their photos; read receipts both ways; archive, pin, star,
mute and delete, kept in step with the phone.

## What is untested or missing

Untested: video, sticker and GIF sending and receiving, and posting or
deleting your own status. Missing: calls, channels, communities, creating and
editing groups, business features, and most WPPConnect routes: the bridge
implements 66 of the 220 that WPPConnect Server defines for WinZapp.
[COBERTURA.md](COBERTURA.md) lists every route, grouped by area. Muting and
deleting for everyone done by someone else can take up to a minute to show.
Data is not encrypted at rest. Treat it as a preview.

## Development

```
neonize_bridge/.venv/Scripts/python -m pytest neonize_bridge
neonize_bridge/.venv/Scripts/python neonize_bridge/server.py --porta 6300 --pasta neonize_bridge/dados-real
```

`--log-estado` turns on a technical log of app-state events.
