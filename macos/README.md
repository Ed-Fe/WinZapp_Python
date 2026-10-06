# WinZapp for macOS

A native macOS build of WinZapp, made for VoiceOver users. Everything
Mac-specific lives in this folder; WinZapp's Windows code is unchanged and
the Windows build is unaffected.

Maintained by Rocco Fiorentino (@rfiorentino1). Windows changes never need
to be tested on a Mac; see "What Windows changes can break" below.

## How it works

`winzapp_mac/` is a compatibility layer installed before WinZapp's
`client/main.py` runs (by `launcher.py` in development, by a PyInstaller
runtime hook in the app). It swaps Windows-only pieces for Mac equivalents
and adapts WinZapp's UI to how Mac apps and VoiceOver behave:

| Module | What it does on the Mac |
|---|---|
| `listctrl.py`, `native_rows.py` | `wx.ListCtrl` is invisible to VoiceOver on macOS (wxGenericListCtrl, custom-drawn). Every list becomes a native table; rows answer VO-Space (activate, like Enter), VO-Shift-M (the row's context menu) and offer the context-menu items as VoiceOver actions (VO-Command-Space), read from WinZapp's own menu handlers so new items appear automatically. |
| `accessibility_mac.py` | `wx.Accessible` does nothing on macOS; its names, descriptions and shortcuts are bridged to NSAccessibility, and unlabelled fields take the label before them, as NVDA does. |
| `speech.py` | Speech goes to VoiceOver as NSAccessibility announcements (no AppleScript setting needed); the system voice is the fallback instead of SAPI. |
| `layout_mac.py` | The messages list keeps a few rows of height when voice playback, recording, a quote or the composer show extra controls: the room comes from the Chats list, which keeps a few rows too. A zero-height list disappears from VoiceOver. |
| `keymap_mac.py` | Shortcuts follow one rule — Ctrl becomes Command, Alt becomes Command-Option, Ctrl+Alt becomes Control-Command — with exceptions where the rule would hit a macOS command (e.g. Exit is Command-Q, archive is Control-Command-A). Menus, hints and the shortcuts help speak the Mac keys. Option+letter keeps typing characters. |
| `menubar_mac.py` | Settings, About and Quit in the application menu; Chats and Messages menus mirroring the selected row's context menu; the Windows self-updater is off. |
| `notify_mac.py` | Native notifications with reply and quick reactions as actions; Focus modes apply. |
| `launch_mac.py` | Ends wxWidgets 3.2's launch wait as soon as macOS finishes launching (it otherwise idles inside `wx.App()` until an unrelated event arrives, the fix wxWidgets 3.3.2 made) and logs `[STARTUP_TIMING] wx.App:` lines for each phase of `wx.App()`. |
| `lifecycle_mac.py` | Closing the window keeps WinZapp running (it still receives messages); the Dock icon brings it back; quitting leaves the Dock immediately. |
| `server_mac.py` | Stops the Node server on quit (the Mac equivalent of `taskkill /F /T`). |
| `paths_mac.py` | Data and the paired session live in `~/Library/Application Support/WinZapp`; the app installs its bundled server runtime there at launch. |
| `sound_mac.py`, `audio_mac.py` | Universal BASS dylibs and the Opus plugin; bundled ffmpeg; the microphone recovers when CoreAudio restarts. |
| `spell_mac.py`, `camera_mac.py`, `hotkey_mac.py`, `platform_mac.py` | Spell checking with the Mac's dictionaries, the camera through AVFoundation, the global hotkey through Carbon's RegisterEventHotKey (no Accessibility permission), Show in Finder, macOS region and language. |
| `strings_mac.py` | Mac wording: a string that describes Windows has a Mac variant beside it in `client/languages` (`<key>_macos`), used in its place on the Mac. |
| `focus_mac.py` | WinZapp's quiet-hours gate follows macOS Focus (Developer ID builds with the Communication Notifications entitlement). |
| `updater_mac.py` | Signed release builds update from the macOS release feed named in their Info.plist; without one the updater is off. |
| `version_mac.py` | A release build runs as its Info.plist release tag (About, update checks), not the unstamped `client/version.py`. |

## Building

Needs Python 3.13 and Homebrew's PortAudio (`brew install portaudio`).

    python3 macos/build_app.py --zip

builds `macos/dist/WinZapp.app` for the Mac it runs on (Apple Silicon or
Intel) and `WinZapp-macOS-<arch>.zip`. It installs the Python
dependencies into `.pydeps/`, downloads the pinned Node.js and ffmpeg
(SHA-256 checked), runs `setup_api.py`, and bundles the WPPConnect server
and its headless Chrome inside the app. `.github/workflows/build-macos.yml`
does this for both architectures on pull requests that touch the Mac build.

Development run without building: `python3 macos/launcher.py`.

Tests: `PYTHONPATH=.pydeps python3 -m pytest macos/tests -c /dev/null --rootdir macos/tests`.
They never show a window.

## What Windows changes can break

The layer never edits WinZapp's files; it replaces WinZapp functions and
methods at startup. So on the Windows side:

- **Renaming or removing** a function, class or method the layer patches or
  reads fails `tests/test_macos_layer_contract.py` in the normal `pytest`
  run, on any platform, naming the file and line in `macos/winzapp_mac` to
  update. Nothing else in `client/` is constrained.
- **A new string that mentions Windows** shows "macOS" in its place on the
  Mac. For better wording, add `<key>_macos` next to it in every locale
  (checked by the same tests as any other key).
- Everything else, including behaviour changes inside a patched method, is
  the Mac maintainer's to follow up; the Windows build never runs Mac code.

## Not yet on the Mac

- **Recording microphone + computer audio.** Windows uses WASAPI; the Mac
  needs a Core Audio process-tap recorder (macOS 14.2+), which would also
  allow a separate VoiceOver volume like the NVDA one. The button is shown
  dimmed and says so.

## Opening a downloaded build

A build without `WINZAPP_SIGN_IDENTITY` (such as the pull-request workflow's)
is ad-hoc signed, not notarized. macOS blocks a downloaded copy the first time: open it once, then allow it
in System Settings, Privacy & Security ("Open Anyway").

## Commit provenance

Mac releases are published from the maintainer's fork, so the Apple
signature proves who built an update, not that its code went through this
repository. A Mac release must therefore DECLARE an official tag and commit,
and the updater checks that this tag exists in
`gabrielhhaber/WinZapp_Python` and points at the declared commit. That is a
declaration checked against our repository. It does not prove the zip was
built from that commit; the stronger guarantee needs an attestation from a
workflow of the official repository, or a maintainer signature over the zip
hash like Windows. Code: `winzapp_mac/provenance.py` (pure standard library,
tested on any platform by `tests/test_macos_provenance.py`).

**Threat model.** The attacker controls the releases repository (its assets,
its CI secrets) and can publish any zip and any provenance there. They cannot
push a tag to the official repository without write access there, which is
the same trust the Windows releases rest on. A Mac release that cannot name
one of our tags and its commit must not install.

**The running version is the official tag the app was built from**
(Info.plist `WinZappReleaseTag`, written by `build_app.py` before signing,
next to `WinZappSourceCommit`). It is never `client/version.py`, which is
the unstamped placeholder in every tagged commit (CI stamps it only at
build), so an old genuine release would otherwise count as newer. A build without a valid
`WinZappReleaseTag` (a development build) offers and installs no update, and
the log says why. Versions compare as integers; an alpha or beta is older
than the stable of the same number; an equal tag is not newer. The same tag,
without its `v`, is the version the whole app shows and uses: `version_mac`
sets `version.__version__` from it at startup (About, the update check's
User-Agent, `UpdateChecker`'s "is it newer" check), and `build_app.py`
writes its numbers (no `alpha`/`beta`, which Apple's integer format does
not allow) as `CFBundleShortVersionString`. Without it, the unstamped `version.py` of the
tag's checkout would make `UpdateChecker` offer the running release on every
check.

**What is verified, in this order.** Any failure, or any answer that is not a
clean 200, is a refusal: nothing is installed. Offline, a timeout, an HTTP
403/429 or 5xx all say "GitHub unreachable or rate-limited, try again later"
and the next update check retries. Every request has a per-read timeout of
15 s and a total deadline of 30 s; each read is made in small chunks and
waits at most for what is left of the deadline, so a server trickling bytes
cannot hold a request open past it.

1. The release has `WinZapp-macOS-provenance-<arch>.json` (fetched from the
   releases repository by tag; untrusted content, at most 16 KB, https). It
   must have exactly the fields `schema` (1), `version`, `source_repo`,
   `source_commit`, `artifacts`, with `source_repo` equal to the official
   repository (pinned in the code), `source_commit` 40 lowercase hex,
   `version` a release tag (`v1.2.3.4`, optionally `alpha`/`beta`), at most 8
   artifacts of name to 64-hex sha256. Every pattern is ASCII and a full
   match (no trailing newline, no other scripts' digits); deeply nested JSON
   is a refusal, not a crash.
2. `version` equals the tag of the release being installed, is newer than the
   running tag, and `source_commit` is not the commit already running. A
   genuine provenance of an older release cannot ride on a newer tag, and an
   older release is refused as a downgrade.
3. Over HTTPS to `api.github.com/repos/gabrielhhaber/WinZapp_Python` only,
   unauthenticated, no redirects, 256 KB cap: `git/ref/tags/<tag>` exists,
   and, peeled through annotated tag objects (`git/tags/<sha>`, whose
   answer must be for the object asked), is exactly `source_commit`.
4. The downloaded zip (at most 2 GB and 30 minutes in total; the partial
   file and the temporary folder are deleted on any failure) has the sha256 the
   provenance lists, and the release's `SHA256SUMS.txt` entry.
5. As before: the app inside has the running app's Apple Team ID, passes
   `codesign --verify --deep --strict`, and `spctl` (notarized).

**Why the tag and not "the commit exists in our repo".** GitHub serves a
fork's commits through the parent's `/commits/<sha>` API, so that answers 200
for code that was never in our repository. The tag is fork-proof. A compare
against `main` would also prove "reachable from main", but costs another call
against the 60 an hour unauthenticated limit and would refuse a hotfix tag on
another branch, so it is not used. Provenance is one file per architecture
(`-arm64`, `-x86_64`) so the two build jobs never merge or overwrite a shared
file.

**What it does not prove.** That the zip was built from that commit. A
releaser can build a modified tree and name an honest commit. `build_app.py`
refuses a dirty tracked tree and a HEAD that is not the tag, but that runs on
the releaser's machine, and untracked files are not checked. Only a
reproducible build, or an attestation from an official-repository workflow
verified by the updater, or a maintainer signature over the zip hash, would
prove it. A compromised maintainer account is out of scope here as it is for
Windows. Verification needs GitHub's API, so it cannot run offline.

**Known limit: tag protection is only as strong as the repository's tag
rules.** The ruleset "Allow tag creation only for admins" (id 20587521)
excludes `refs/tags/v*alpha`, so any collaborator with write access can
create or move an alpha tag, and an alpha provenance is only as trustworthy as
that. Recommendation to the maintainer: cover alpha tags too, or restrict
them to the alpha workflow. This change touches no repository setting.

**How a release is produced (Rocco).** There is nothing to stamp: the tag is
the version.

1. Fetch the official tags (`git fetch https://github.com/gabrielhhaber/WinZapp_Python.git
   --tags`) and check out the maintainer's tag on a clean tree. If the
   checkout has no tags, set `WINZAPP_RELEASE_TAG=<tag>`; HEAD must still be
   that tag's commit. Do not edit `client/version.py` (a modified tracked
   file makes `build_app.py` refuse the build).
2. On each architecture:

       WINZAPP_MAC_RELEASES_REPO=rocco-labs/WinZapp_Python \
       WINZAPP_SIGN_IDENTITY=... python3 macos/build_app.py --zip

   `build_app.py` resolves the tag against the official API before building
   and stops if HEAD differs, tracked files are modified, or a release build
   has no tag. It writes `WinZappReleaseTag` and `WinZappSourceCommit` into
   Info.plist before signing, and
   `macos/dist/WinZapp-macOS-provenance-<arch>.json` next to the zip.
3. Publish a release in the fork with the same tag name as the official one,
   with `WinZapp-macOS-<arch>.zip`, `WinZapp-macOS-provenance-<arch>.json`
   and a `SHA256SUMS.txt` covering both zips, for both architectures.

An untagged build makes no provenance and does not self-update.

**What the maintainer does.** Nothing new: tag as today. The tag is the
approval.

**Optional, recommended: artifact attestations.** In the workflow that builds
the release, `actions/attest-build-provenance` (pinned by SHA, with
`id-token: write` and `attestations: write` on that job only) signs the zip's
digest with the workflow's identity, and `gh attestation verify <zip> --repo
<repo>` checks it. Run from an official-repository workflow it would show
which workflow built the zip, and a future updater could require it. It needs
no secret, but whether a release workflow exists is decided in issue #343;
nothing here adds one.
