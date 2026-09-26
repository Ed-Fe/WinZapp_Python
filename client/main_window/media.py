"""MediaMixin — part of MainWindow (see main_window/__init__.py).

Moved verbatim out of main.py. Methods run with ``self`` bound to the
MainWindow instance, so every attribute set in MainWindow.__init__ is
available here.
"""

import base64
import json
import logging
import os
import tempfile
import threading
import time
import wx
from core.utils import (
    MEASURED_SECONDS_KEY,
    auto_download_allows,
    encrypt,
    video_seconds,
)
from main_window.message_rules import (
    MediaExpiredError,
    _report_media_fetch_failure,
    media_fetch_timeout,
)
from main_window.runtime_setup import _looks_like_json_response
from core.api_client import api_post
from app_paths import data_path
from ui.conversations import probe_media_duration
from core.transcription import stored as stored_transcription


class MediaMixin:
    """Media download: size/age limits, failed-id bookkeeping, sync_if_media,
    base64 fetch, video duration probing and local audio saving.
    """

    # WhatsApp CDN URLs (mmg.whatsapp.net) expire after ~90 days.  Attempting
    # to download older media causes the WPPConnect to enter a 5-second retry
    # loop for every expired URL, which starves the API thread pool and eventually
    # breaks sends.  Never request media older than this threshold.
    _MEDIA_MAX_AGE_SECONDS = 14 * 24 * 3600  # 14 days — WhatsApp CDN typical TTL
    _MEDIA_SYNC_WORKERS    = 1               # parallel workers during bulk sync — kept
                                              # low because WPPConnect proxies every
                                              # request through a single Puppeteer/Chrome
                                              # automation session; too many concurrent
                                              # downloads were starving unrelated requests
                                              # (send-seen, contact lookups) into sporadic
                                              # "session is not active" / "chat not found"
                                              # failures even though the session was fine.
    _MEDIA_SYNC_TIMEOUT    = 60              # seconds per request during bulk sync

    # A message this large gets skipped by the automatic background sync
    # (sync_if_media) instead of being downloaded eagerly. WPPConnect base64-
    # encodes the whole file inside its Node/Puppeteer process before ever
    # handing it back over HTTP — for a ~1 GB document sent into a group this
    # was observed pushing node.exe's memory usage past 5 GB and hanging the
    # machine. The user can still explicitly open/download an oversized file
    # from the conversation view (_on_action_open/_on_action_download in
    # conversations.py) — only the unattended background pass is capped.
    _MEDIA_AUTO_DOWNLOAD_MAX_BYTES = 100 * 1024 * 1024   # 100 MB — fallback only,
                                                          # see _media_max_download_bytes()

    def _media_max_download_days(self) -> int:
        """User-configurable cap (Settings > Armazenamento) on how old a
        message can be and still have its media auto-downloaded. 0 means
        unlimited (still subject to the hard _MEDIA_MAX_AGE_SECONDS CDN-TTL
        floor above, which is not user-configurable — downloading past that
        point fails regardless of what the user asked for)."""
        try:
            return int(self.settings.get("storage", {}).get("media_max_days", 30))
        except (TypeError, ValueError):
            return 30

    def _media_max_download_bytes(self) -> int:
        """User-configurable cap (Settings > Armazenamento) on individual
        media file size for auto-download. 0 means unlimited."""
        try:
            mb = int(self.settings.get("storage", {}).get("media_max_mb", 100))
        except (TypeError, ValueError):
            mb = 100
        return mb * 1024 * 1024 if mb > 0 else 0

    def _load_media_failed_ids(self) -> dict:
        """Load {message_id: failed_at_timestamp} for media whose CDN URL has
        previously expired (403/410) — checked by sync_if_media() to skip a
        pointless repeat download attempt.

        This was a bare set with no eviction, growing forever and persisted
        across every restart (data/media_failed.json) — for an account with
        a lot of old/expired media, a genuine unbounded-growth source. Every
        entry is provably dead weight once its message is older than
        _MEDIA_MAX_AGE_SECONDS anyway: sync_if_media()'s own age check skips
        it before ever consulting this set, so there is nothing lost by
        pruning entries past that point — they can never be looked up again.
        """
        try:
            with open(data_path("media_failed.json"), "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            return {}
        now = time.time()
        if isinstance(raw, dict):
            return {
                mid: ts for mid, ts in raw.items()
                if isinstance(ts, (int, float)) and (now - ts) <= self._MEDIA_MAX_AGE_SECONDS
            }
        if isinstance(raw, list):
            # Legacy format (plain list from before this became a dict) —
            # no timestamp to judge age by, so treat every entry as freshly
            # failed rather than either keeping stale ones forever or
            # discarding real, still-useful skip-hints outright.
            return {mid: now for mid in raw if isinstance(mid, str)}
        return {}

    def _save_media_failed_ids(self):
        """Persist the failed-media map so expired IDs are skipped on future launches."""
        with self._media_failed_lock:
            try:
                with open(data_path("media_failed.json"), "w", encoding="utf-8") as f:
                    json.dump(self._media_failed_ids, f)
            except Exception:
                pass

    def _forget_media_failures(self):
        """Drop the ids of media whose CDN URL had already expired (403/410),
        from RAM and from data/media_failed.json.

        Both wipes need exactly this and had a copy each. F5 because the
        messages those ids name were just deleted; the account switch because
        they name the PREVIOUS account's messages, and the file outlives the
        switch entirely — a fresh install of account B started life refusing
        to download media it had never once tried.
        """
        self._media_failed_ids = {}
        try:
            media_failed_path = data_path("media_failed.json")
            if os.path.isfile(media_failed_path):
                os.remove(media_failed_path)
        except Exception as exc:
            logging.warning(
                "[media_failures] failed to remove media_failed.json: %s", exc)

    def _is_conversation_open_for(self, msg) -> bool:
        """True if msg belongs to the conversation currently shown on screen."""
        cp = getattr(self, "conversations_panel", None)
        if cp is None or getattr(cp, "conversation", None) is None:
            return False
        open_jid = cp.conversation.get("remoteJid", "")
        if not open_jid:
            return False
        key = msg.get("key", {})
        msg_jid = self._normalize_jid(key.get("remoteJid", ""))
        return msg_jid == self._normalize_jid(open_jid)

    def sync_if_media(self, msg, timeout=60):
        """Download media for a single message during the background sync phase.

        Returns True only when a file was actually downloaded. Every skip
        below — offline, not a media message, past the CDN TTL, past the
        user's day/size caps, a known-expired id, already on disk — returns
        False, so sync_media_for_all_chats() can count real work rather than
        candidates.
        """
        if not getattr(self, "_wa_connected", False) or getattr(self, "offline_mode", False):
            return False
        message_type = msg.get("messageType", "")
        if not message_type and msg.get("type"):
            t = str(msg.get("type"))
            if t in ("audio", "ptt"):
                message_type = "audioMessage"
            elif t == "image":
                message_type = "imageMessage"
            elif t == "video":
                message_type = "videoMessage"
            elif t in ("document", "doc"):
                message_type = "documentMessage"
            elif t == "sticker":
                message_type = "stickerMessage"

        _MEDIA_TYPES = {"documentMessage", "imageMessage", "stickerMessage", "videoMessage"}
        if message_type not in _MEDIA_TYPES and message_type != "audioMessage":
            return False

        # Configuracoes > Armazenamento > "Tipos de midia a serem baixados
        # automaticamente". Checked here rather than at the two call sites
        # because this is the single funnel every automatic download passes
        # through — the live-message path (on_new_message) and the sync sweep
        # (sync_media_for_all_chats) both land here. Opening the media by hand
        # still downloads it: this is about what happens without being asked.
        if not auto_download_allows(self.settings, msg):
            return False

        # Skip messages older than the CDN TTL — URLs have certainly expired.
        ts = int(msg.get("messageTimestamp", 0) or 0)
        if ts and (time.time() - ts) > self._MEDIA_MAX_AGE_SECONDS:
            return False

        # User-configurable age cap (Settings > Armazenamento > "Baixar
        # mídias de até (dias)"). 0 means unlimited — falls back to whatever
        # the CDN-TTL check above already allows.
        max_days = self._media_max_download_days()
        if ts and max_days > 0 and (time.time() - ts) > max_days * 86400:
            return False

        msg_id = msg.get("key", {}).get("id", "")
        if not msg_id or "-" in msg_id or msg.get("_local_pending"):
            return False

        # Skip IDs that previously returned 403/410 (expired CDN URL).
        if msg_id and msg_id in self._media_failed_ids:
            return False

        # Skip oversized files during the automatic background sync — see
        # _media_max_download_bytes() (Settings > Armazenamento > "Baixar
        # mídias de até no máximo (mb)"; 0 = unlimited).
        max_bytes = self._media_max_download_bytes()
        msg_inner = msg.get("message")
        if isinstance(msg_inner, str):
            try:
                msg_inner = json.loads(msg_inner)
            except Exception:
                msg_inner = None
        inner = msg_inner.get(message_type) if isinstance(msg_inner, dict) else None
        if max_bytes and isinstance(inner, dict):
            try:
                file_length = int(inner.get("fileLength") or 0)
            except (TypeError, ValueError):
                file_length = 0
            if file_length > max_bytes:
                logging.info(
                    "[sync_if_media] Skipping auto-download of %s (%s, %.1f MB > %.0f MB limit)",
                    msg_id, message_type, file_length / (1024 * 1024),
                    max_bytes / (1024 * 1024),
                )
                return False

        try:
            if message_type == "audioMessage":
                return bool(self.handle_audio_message(msg, timeout=timeout))
            else:
                # Bulk background sync: download WITHOUT per-chunk progress
                # callbacks. Streaming 64 KB chunks across 6 workers used to fire
                # a wx.CallAfter per chunk per file — tens of thousands of UI
                # events, each doing an O(n) scan of the open conversation —
                # which froze the app while media downloaded. Only refresh the
                # row once, and only when its chat is the conversation currently
                # on screen.
                downloaded = self.handle_media_message(
                    msg, progress_callback=None, timeout=timeout)
                if msg_id and self._is_conversation_open_for(msg):
                    conv = self.conversations_panel
                    wx.CallAfter(conv.update_message_download_progress, msg_id, 1.0)
                return bool(downloaded)
        except MediaExpiredError:
            if msg_id:
                self._media_failed_ids[msg_id] = time.time()
        except Exception:
            pass
        return False

    def handle_media_message(self, msg, progress_callback=None, timeout=60):
        """Download and encrypt a document/image/sticker/video to data/media/.

        Returns True only when this call actually wrote a new file — every
        other path (no id, already on disk, not connected, empty response)
        returns False. sync_media_for_all_chats() counts those return values
        to report how much was really downloaded; see its own docstring.
        """
        msg_id = msg.get("key", {}).get("id", "")
        if not msg_id:
            return False
        if "_" in msg_id:
            parts = msg_id.split("_")
            msg_id = parts[2] if len(parts) > 2 else parts[-1]
        media_path = data_path("media", f"{msg_id}.wzmedia")
        if os.path.isfile(media_path):
            return False
        if not getattr(self, "_wa_connected", False):
            # Covers both "confirmed offline" and "still connecting at
            # startup" (_wa_connected only flips True once the connection is
            # actually verified — see _set_wa_connected) — attempting the
            # HTTP call in either case just burns the request timeout against
            # an API that cannot possibly answer yet, and previously surfaced
            # as a generic "could not download this media file" instead of
            # something that tells the user to wait for the connection.
            logging.info("[handle_media_message] Skipping download for %s — not connected.", msg_id)
            return False
        # Bytes, and a timeout that knows how big the file is. Between them
        # these are what make a 200 MB document downloadable at all: the base64
        # route allocated roughly 1.3 GB in this process for one, and the flat
        # 60s abandoned the request long before the server had finished
        # fetching it. See fetch_media_bytes() and media_fetch_timeout().
        content = self.fetch_media_bytes(
            msg, progress_callback=progress_callback,
            timeout=media_fetch_timeout(msg, timeout),
        )
        if not content:
            return False
        encrypted = encrypt(content, self.key)
        with open(media_path, "wb") as f:
            f.write(encrypted)
        self._maybe_probe_video_duration(msg, content)
        return True

    def _maybe_probe_video_duration(self, msg: dict, content: bytes):
        """Measure a just-downloaded video that never stated its duration, if
        Settings > Armazenamento says to.

        A video whose sender omitted the duration reads as a bare "vídeo"
        until it is played, at which point _learn_video_duration() fills the
        gap for free (the file is decoded for playback anyway). This option
        trades that wait for one media decode per downloaded video: worth it
        for someone who wants the length in the list without opening
        anything, wasted work for someone who doesn't — hence off by default.

        The measurement runs on its own thread. handle_media_message() is
        called from the UI thread too (the play path downloads on demand
        before starting), and a BASS decode of a 25 MB video is not something
        to do inline there. `content` is the bytes already in hand, so the
        just-written file is never read back and decrypted a second time.
        """
        if not self.settings.get("storage", {}).get(
            "probe_video_duration_on_download", False
        ):
            return
        video = (msg.get("message") or {}).get("videoMessage")
        if not isinstance(video, dict) or video_seconds(video) is not None:
            return

        def _bg():
            tmp_path = ""
            try:
                with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
                    tmp.write(content)
                    tmp_path = tmp.name
                secs = probe_media_duration(tmp_path)
            except Exception:
                logging.exception("[_maybe_probe_video_duration] probe failed")
                return
            finally:
                if tmp_path:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
            # secs == 0 is a real answer (a clip under a second), only None
            # means the file could not be read — see video_seconds().
            if secs is not None and secs >= 0:
                wx.CallAfter(self._apply_probed_video_duration, msg, secs)

        threading.Thread(target=_bg, daemon=True).start()

    def _apply_probed_video_duration(self, msg: dict, seconds: int):
        """Store a probed video length on the record and show it (UI thread)."""
        video = (msg.get("message") or {}).get("videoMessage")
        if not isinstance(video, dict) or video_seconds(video) is not None:
            # Playback got there first (_learn_video_duration) — its answer
            # came from the same file, so there is nothing to correct.
            return
        video[MEASURED_SECONDS_KEY] = seconds
        msg_id = msg.get("key", {}).get("id", "")
        jid = self._normalize_jid(msg.get("key", {}).get("remoteJid", ""))
        logging.info("[_apply_probed_video_duration] %s: file says %ds", msg_id, seconds)
        if jid and getattr(self, "db", None) is not None:
            def _bg_persist():
                try:
                    self.db.insert_message(jid, msg)
                except Exception as exc:
                    logging.warning("[_apply_probed_video_duration] persist failed for %s: %s",
                                    msg_id, exc)
            self._msg_bg_executor.submit(_bg_persist)
            self._schedule_save(dirty_jid=jid)
        cp = getattr(self, "conversations_panel", None)
        if cp is not None and cp.conversation and cp.conversation.get("remoteJid") == jid:
            # Repaint only — a rebuild here would move the user's focus for a
            # row that just gained a duration clause.
            cp._repaint_message_rows([msg_id])

    # ── Saved transcriptions (issue #112) ───────────────────────────────────
    # The rule that keeps a transcription across resyncs, and why deleting
    # writes a tombstone, is in core/transcription/stored.py. What lives here
    # is only which dicts hold the message and how the write is scheduled.

    def _transcription_copies(self, jid: str, msg_id: str) -> list:
        """Every in-memory dict that may be message *msg_id* of chat *jid*.

        More than one, routinely: the chat's records and its lastMessage, and
        the conversation panel's own lists — which keep the dicts of the chat
        they were built from after a resync has swapped self.chats[jid] for a
        new one. Each of them can be written back to the database by some
        later path (a star toggled, a status update, save_data()), and the
        menu reads the panel's copy, so all of them have to agree.
        """
        copies = []
        chat = self.get_chat(jid)
        if isinstance(chat, dict):
            copies.extend(((chat.get("messages") or {}).get("messages") or {}).get("records") or [])
            copies.append(chat.get("lastMessage"))
        cp = getattr(self, "conversations_panel", None)
        conversation = getattr(cp, "conversation", None)
        if (isinstance(conversation, dict)
                and self._normalize_jid(conversation.get("remoteJid", "")) == self._normalize_jid(jid)):
            copies.extend(((conversation.get("messages") or {}).get("messages") or {}).get("records") or [])
            copies.extend(getattr(cp, "_all_sorted_messages", None) or [])
            copies.extend(getattr(cp, "_sorted_messages", None) or [])
        return copies

    def store_message_transcription(self, jid: str, msg_id: str, value: dict) -> str:
        """Keep *value* as the transcription of message *msg_id* (UI thread).

        Returns one of stored_transcription's SAVE_* answers, which the flow
        turns into a note in the result window when it is not SAVE_STORED —
        or, for SAVE_WITHDRAWN, into one spoken sentence and no window
        (ui.transcription_flow).

        The message is looked up again here, by id, rather than trusted from
        the dict the flow held when the run started: a transcription takes
        minutes, and a sync may have replaced that dict since, or the send of
        an own message may have given it its real id. Memory first, on every
        copy, so the menu offers "Ver transcrição" at once; the database gets
        the dedicated key-only write (never insert_message() of this record,
        whose other fields may be minutes stale) on the transcription write
        queue, in one call covering every JID the conversation's rows may be
        filed under (_transcription_storage_jids()). When none of them has
        the row yet — a message that arrived live and has not been persisted
        — the record is written whole through insert_message(), whose rule
        keeps whichever decision is later. Only an actual failure of the
        database is said to the user, in one sentence: the window must not go
        on implying the text was kept when nothing was written.

        A message its sender deleted for everyone while it was being
        transcribed is refused outright (SAVE_WITHDRAWN): _apply_remote_revoke()
        turns the record into a protocolMessage in place, so find_record()
        still finds it, and the text is exactly what that sender withdrew.
        The database refuses it too (_rewrite_transcription()), for a revoke
        that reaches the disk before it reaches this dict.
        """
        copies = self._transcription_copies(jid, msg_id)
        current = stored_transcription.find_record(copies, msg_id)
        if current is None:
            return stored_transcription.SAVE_MISSING
        if stored_transcription.is_withdrawn(current):
            return stored_transcription.SAVE_WITHDRAWN
        if stored_transcription.is_unsent(current):
            return stored_transcription.SAVE_UNSENT
        real_id = (current.get("key") or {}).get("id", "")
        if real_id != msg_id:
            # Sent while it was being transcribed: the copies now carry the
            # real id, and that is the one the row is stored under.
            copies = self._transcription_copies(jid, real_id)
        # Dated after anything these copies hold: the flow dated it with the
        # wall clock minutes ago, and a clock set back since would make this
        # newer decision lose to the one it replaces.
        at = stored_transcription.decision_time(value)
        later = stored_transcription.next_decision_time(
            at if at is not None else time.time(),
            *(c.get(stored_transcription.TRANSCRIPTION_KEY) for c in copies
              if isinstance(c, dict) and (c.get("key") or {}).get("id") == real_id),
        )
        if later != at:
            value = dict(value, at=later)
        stored_transcription.set_on_copies(copies, real_id, value)
        db = getattr(self, "db", None)
        if db is not None:
            # Taken here, on the UI thread, already carrying the value: the
            # fallback below writes this snapshot, never a dict still in use.
            record = dict(current)
            storage_jids = self._transcription_storage_jids(jid)

            def _bg_persist():
                # Nothing about the message in these lines — not its id either.
                try:
                    if not db.set_message_transcription(storage_jids, real_id, value):
                        db.insert_message(jid, record)
                        # The missing row is the database's own line; this
                        # one says only what was done about it.
                        logging.info("[transcription] fell back to writing the message whole")
                except Exception as exc:
                    logging.warning("[transcription] storing a transcription failed: %s",
                                    type(exc).__name__)
                    wx.CallAfter(self._say_transcription_not_stored)
            self._transcription_write_queue.submit(_bg_persist)
        self._schedule_save(dirty_jid=jid)
        return stored_transcription.SAVE_STORED

    def _say_transcription_not_stored(self):
        """The one sentence for a transcription the database refused (UI thread).

        Said over whatever is on screen — usually the result window, which
        opened before the background write answered — without interrupting
        it: the text is still there to read, and the copies in memory still
        carry it, but nothing promises it will be there next time.
        """
        if hasattr(self, "error_sound"):
            self.error_sound.play()
        self.output(self.i18n.t("transcription_store_failed"))

    def _transcription_storage_jids(self, jid: str) -> list:
        """The chat JIDs the rows of conversation *jid* may be stored under.

        The conversation's own, and the @lid its older history can still be
        filed under — the same lookup ConversationsPanel._history_storage_jid()
        makes to read that history back. A message loaded from there lives in
        the database under the @lid, so a write keyed on the phone JID alone
        finds no row, and the transcription would reach the disk only if some
        later save happened to carry it.
        """
        jids = [jid]
        lid = (getattr(self, "_phone_to_lid", None) or {}).get(jid, "")
        if lid and lid != jid:
            jids.append(lid)
        return jids

    def delete_message_transcription(self, jid: str, msg_id: str, on_done) -> None:
        """Delete message *msg_id*'s transcription; call *on_done(ok)* on the
        UI thread once the database has answered.

        The database first and memory after, the reverse of storing: saying
        "deleted" while the disk still holds the text would be false, and it
        is the disk that outlives the session. *on_done* runs only once every
        copy on disk has the tombstone: the message's row under each JID the
        conversation may be stored under — one call, one lock hold — and the
        chat's preview, which never holds the text
        (DatabaseManager._build_chat_values()) and is scrubbed by the same
        write if an older one did. A row that does not exist is not a failure
        — nothing is left holding the text, which is what the user asked for.
        Queued behind any save still waiting for the database, never beside
        it: see _transcription_write_queue in __init__. When a newer
        transcription took the deletion's place while it waited, *on_done*
        is not called at all — there is nothing true to say about a deletion
        the user has already superseded.
        """
        copies = self._transcription_copies(jid, msg_id)
        # After whatever is stored, whatever the clock says — see
        # store_message_transcription(); a delete dated before the text it
        # deletes is one the next sync would undo.
        deleted_at = stored_transcription.next_decision_time(
            time.time(),
            *(c.get(stored_transcription.TRANSCRIPTION_KEY) for c in copies
              if isinstance(c, dict) and (c.get("key") or {}).get("id") == msg_id),
        )
        value = stored_transcription.tombstone(deleted_at)
        db = getattr(self, "db", None)
        storage_jids = self._transcription_storage_jids(jid)

        def _finish(ok):
            if ok:
                # Only where the tombstone is still the latest decision: this
                # delete may have waited in the queue behind a "Transcrever
                # novamente" whose text is already on the copies (and on disk,
                # written after the tombstone). Overwriting it would put the
                # older decision back in memory under the new result window.
                current_copies = self._transcription_copies(jid, msg_id)
                accepted, found = stored_transcription.set_where_newer(
                    current_copies, msg_id, value
                )
                if accepted:
                    self._schedule_save(dirty_jid=jid)
                if found > accepted:
                    # Saying "deleted" now would contradict the transcription
                    # the user is looking at; the later decision is the answer.
                    # Any copy refusing it is enough, not only all of them: a
                    # copy reloaded from the database can carry the newer text
                    # while another still holds the old one, and "apagada"
                    # would then be said over text that is still there.
                    #
                    # Nor can the copies that took the tombstone keep it: the
                    # disk holds the newer decision, so memory would disagree
                    # with it — some copies deleted, one with text — until the
                    # next reload. Not because a tombstone can lose on disk
                    # (_rewrite_transcription() lets it win over whatever the
                    # row holds), but because of when it was written: it is
                    # dated after every copy memory held when the delete was
                    # decided, so any decision newer than it was made and
                    # queued after it, and the write queue has one thread —
                    # that decision reached the disk after the tombstone and
                    # replaced it there.
                    # The newest decision any copy holds goes on all of them.
                    newest = value
                    for copy in current_copies:
                        if isinstance(copy, dict) and (copy.get("key") or {}).get("id") == msg_id:
                            newest = stored_transcription.newer_decision(
                                copy.get(stored_transcription.TRANSCRIPTION_KEY), newest
                            )
                    stored_transcription.set_on_copies(current_copies, msg_id, newest)
                    logging.info("[transcription] a later transcription replaced the deleted "
                                 "one while the delete was queued: %d of %d copy(ies) kept it",
                                 found - accepted, found)
                    return
            on_done(ok)

        def _bg_delete():
            ok = db is not None
            if ok:
                try:
                    db.delete_message_transcription(storage_jids, msg_id, deleted_at)
                except Exception as exc:
                    ok = False
                    logging.warning("[transcription] deleting a transcription failed: %s",
                                    type(exc).__name__)
            wx.CallAfter(_finish, ok)

        self._transcription_write_queue.submit(_bg_delete)

    def handle_audio_message(self, msg, timeout=60):
        """Download and encrypt a voice message to data/voice_messages/.

        Returns True only when this call actually wrote a new file — see
        handle_media_message(), which follows the same contract.
        """
        voice_messages_dir = data_path("voice_messages")
        msg_id = msg.get('key', {}).get('id', '')
        if "_" in msg_id:
            parts = msg_id.split("_")
            msg_id = parts[2] if len(parts) > 2 else parts[-1]
        audio_file_path = os.path.join(voice_messages_dir, f"{msg_id}.msv")
        if os.path.isfile(audio_file_path):
            return False
        if not getattr(self, "_wa_connected", False):
            # See handle_media_message() — same reasoning applies to audio.
            logging.info("[handle_audio_message] Skipping download for %s — not connected.", msg_id)
            return False
        base64_audio = self.get_base64_from_media(msg, timeout=timeout)
        if not base64_audio:
            return False
        audio_content = base64.b64decode(base64_audio)
        return self.save_audio_locally(msg, audio_content)

    def fetch_media_bytes(self, media, progress_callback=None, timeout=60):
        """The media file itself, as bytes — never as base64.

        Preferred over get_base64_from_media() by anything that just wants to
        write the file somewhere. For a 200 MB document the base64 route holds,
        in Python alone, the chunk list (267 MB), the joined buffer (267 MB),
        its decoded str (267 MB), the str json.loads builds (267 MB) and only
        then the 200 MB of actual file — before encrypt() adds its own ~267 MB
        Fernet token. That is the "it says it is downloading, downloads
        nothing, and fills the RAM" report.

        Returns b"" on every failure, exactly as its base64 sibling returns "".
        """
        return self.get_base64_from_media(
            media, progress_callback=progress_callback, timeout=timeout,
            _binary=True,
        ) or b""

    def get_base64_from_media(self, media, progress_callback=None, timeout=60,
                              _binary=False):
        """
        Fetch encrypted media from WPPConnect and return its base64 string.

        Raises MediaExpiredError when the WhatsApp CDN URL has expired (HTTP 403/410).
        When *progress_callback* is provided the request is streamed and the
        callback is called with a float in [0, 1] as each chunk arrives.

        `_binary` is private and belongs to fetch_media_bytes() — see there for
        why bytes matter. It changes the return type to bytes, which is exactly
        why no caller should pass it directly. Everything up to the response is
        shared rather than duplicated: the body this endpoint needs is ninety
        lines of JID and mediaKey archaeology, and a second copy of it would
        drift the moment either is touched.
        """
        _key = media.get("key", {})
        remote_jid = _key.get("remoteJid", "") or media.get("from", "")
        # If remote_jid is phone@c.us and we have an LID mapping for it, prefer LID JID
        if remote_jid and not remote_jid.endswith("@lid"):
            norm_phone = self._normalize_jid(remote_jid)
            alt_lid = getattr(self, "_phone_to_lid", {}).get(norm_phone, "")
            if alt_lid:
                _key = dict(_key)
                _key["remoteJid"] = alt_lid

        msg_id = self._serialize_msg_id(_key.get("remoteJid", "") or media.get("from", ""), _key, full_msg=media)
        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/get-media-by-message/{msg_id}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        if _binary:
            # Content negotiation, not a switch: client/api/ is reinstalled
            # independently of this app, so an older server that has never
            # heard of this header must keep working. It answers with the
            # base64 JSON it always did, and the reader below detects which
            # shape came back rather than assuming.
            headers["Accept"] = "application/octet-stream"

        # Prepare body with media details to bypass Puppeteer cache lookups in WPPConnect Server
        body_data = dict(media)
        # The record goes over the wire whole, and a saved transcription is
        # part of it — the text of a private voice note, which the server has
        # no use for and never had a copy of.
        body_data.pop(stored_transcription.TRANSCRIPTION_KEY, None)
        msg_type = media.get("messageType")
        msg_inner_obj = media.get("message")
        if isinstance(msg_inner_obj, str):
            try:
                msg_inner_obj = json.loads(msg_inner_obj)
            except Exception:
                msg_inner_obj = None

        if not msg_type and media.get("type"):
            t = str(media.get("type"))
            if t in ("audio", "ptt"):
                msg_type = "audioMessage"
            elif t == "image":
                msg_type = "imageMessage"
            elif t == "video":
                msg_type = "videoMessage"
            elif t in ("document", "doc"):
                msg_type = "documentMessage"

        # Check nested structures as well as top-level keys
        candidate_objs = []
        if isinstance(msg_inner_obj, dict):
            candidate_objs.append(msg_inner_obj)
            if msg_type and isinstance(msg_inner_obj.get(msg_type), dict):
                candidate_objs.append(msg_inner_obj.get(msg_type))
            for k in ("audioMessage", "imageMessage", "videoMessage", "documentMessage", "stickerMessage"):
                if isinstance(msg_inner_obj.get(k), dict):
                    candidate_objs.append(msg_inner_obj.get(k))
        candidate_objs.append(media)

        for obj in candidate_objs:
            if not body_data.get("mediaKey") and obj.get("mediaKey"):
                mk = obj.get("mediaKey")
                if isinstance(mk, bytes):
                    body_data["mediaKey"] = base64.b64encode(mk).decode("utf-8")
                elif isinstance(mk, dict) and "data" in mk:
                    body_data["mediaKey"] = base64.b64encode(bytes(mk["data"])).decode("utf-8")
                else:
                    body_data["mediaKey"] = str(mk)
            if not body_data.get("clientUrl") and (obj.get("url") or obj.get("clientUrl")):
                body_data["clientUrl"] = obj.get("url") or obj.get("clientUrl")
            if not body_data.get("directPath") and obj.get("directPath"):
                body_data["directPath"] = obj.get("directPath")
            if not body_data.get("mimetype") and obj.get("mimetype"):
                body_data["mimetype"] = obj.get("mimetype")

        if msg_type:
            body_data["type"] = msg_type.replace("Message", "")

        # Correlation id for the server's progress events. Deliberately the
        # message's own key id rather than the serialized form sent in the URL:
        # the panel keys its gauge by that, and the serialized form goes
        # through @lid/@c.us rewriting on both sides, so matching on it would
        # mean re-deriving the same guess in two places.
        body_data["progressId"] = _key.get("id", "") or msg_id

        has_media_key = bool(body_data.get("mediaKey"))
        has_client_url = bool(body_data.get("clientUrl"))
        has_direct_path = bool(body_data.get("directPath"))
        media_type = body_data.get("type", "")
        logging.info(
            "[get_base64_from_media] Requesting media for msg_id=%s, url=%s, has_mediaKey=%s, has_clientUrl=%s, type=%s",
            msg_id, url, has_media_key, has_client_url, media_type
        )

        max_attempts = 3
        for attempt in range(max_attempts):
            if progress_callback is None:
                try:
                    response = api_post(url, headers=headers, json=body_data, timeout=timeout)
                except MediaExpiredError:
                    logging.warning("[get_base64_from_media] MediaExpiredError for msg_id=%s", msg_id)
                    raise
                except Exception as exc:
                    logging.warning(
                        "[get_base64_from_media] request exception for %s (attempt %d/%d): %s",
                        msg_id, attempt + 1, max_attempts, exc,
                    )
                    if attempt < max_attempts - 1:
                        time.sleep(3)
                        continue
                    return ""
                
                # Deliberately NOT response.text on a successful body. That
                # decodes the whole response into a str just to log its first
                # 200 characters — for a 200 MB document, a quarter-gigabyte
                # allocation whose only purpose is a log line, and on the
                # binary path it would also be a str built out of arbitrary
                # bytes. The snippet is only ever read on a failure, so pay
                # for it only there.
                resp_text = ""
                if response.status_code not in (200, 201):
                    resp_text = response.text or ""
                logging.info(
                    "[get_base64_from_media] WPPConnect server status=%d for msg_id=%s, body_snippet=%s",
                    response.status_code, msg_id, resp_text[:200]
                )

                if response.status_code in (403, 410):
                    logging.warning("[get_base64_from_media] HTTP %d (CDN expired) for %s", response.status_code, msg_id)
                    raise MediaExpiredError(response.status_code)
                if response.status_code in (200, 201):
                    if _binary and not _looks_like_json_response(response):
                        # The server honoured the octet-stream Accept: the body
                        # IS the file. response.content is the only copy.
                        payload = response.content
                        logging.info(
                            "[get_base64_from_media] Success for %s — %d raw byte(s)",
                            msg_id, len(payload),
                        )
                        return payload
                    b64 = response.json().get("base64", "")
                    logging.info("[get_base64_from_media] Success for %s — base64 len=%d", msg_id, len(b64))
                    return base64.b64decode(b64) if _binary else b64

                # Check for transient session not active errors
                if response.status_code in (400, 500) and any(x in resp_text.lower() for x in ("session is not active", "not active", "disconnected")):
                    logging.warning(
                        "[get_base64_from_media] session not active for %s, retrying in 3s (attempt %d/%d)",
                        msg_id, attempt + 1, max_attempts
                    )
                    self._set_wa_connected(False, "media fetch: session not active", announce=False)
                    if attempt < max_attempts - 1:
                        time.sleep(3)
                        continue
                if not _report_media_fetch_failure(msg_id, response.status_code,
                                                   resp_text):
                    logging.warning(
                         "[get_base64_from_media] HTTP %s fetching media for %s: %s",
                         response.status_code, msg_id, resp_text[:200],
                    )
                return ""
            else:
                # Streaming mode so we can report per-chunk progress
                try:
                    response = api_post(url, headers=headers, json=body_data, stream=True, timeout=timeout)
                    if response.status_code in (403, 410):
                        raise MediaExpiredError(response.status_code)
                    
                    # Check for transient session not active errors before streaming
                    if response.status_code in (400, 500):
                        # Read small error response
                        resp_text = response.text
                        if any(x in resp_text.lower() for x in ("session is not active", "not active", "disconnected")):
                            logging.warning(
                                "[get_base64_from_media] session not active for %s (stream), retrying in 3s (attempt %d/%d)",
                                msg_id, attempt + 1, max_attempts
                            )
                            self._set_wa_connected(False, "media fetch: session not active", announce=False)
                            if attempt < max_attempts - 1:
                                time.sleep(3)
                                continue
                        logging.warning(
                            "[get_base64_from_media] HTTP %s fetching media for %s: %s",
                            response.status_code, msg_id, resp_text[:200],
                        )
                        return ""

                    if response.status_code not in (200, 201):
                        logging.warning(
                            "[get_base64_from_media] HTTP %s fetching media for %s",
                            response.status_code, msg_id,
                        )
                        return ""
                    
                    total = int(response.headers.get("content-length", 0))
                    downloaded = 0
                    # A bytearray, not a list of chunks joined afterwards. The
                    # join doubles the peak — and this is the path the Download
                    # button uses, so it is the one a 200 MB document dies on.
                    body = bytearray()
                    for chunk in response.iter_content(chunk_size=65536):
                        if chunk:
                            body += chunk
                            downloaded += len(chunk)
                            if total > 0:
                                progress_callback(downloaded / total)

                    if _binary and not _looks_like_json_response(response):
                        # Raw file bytes: nothing to decode, nothing to parse.
                        return bytes(body)

                    try:
                        parsed = json.loads(bytes(body))
                        b64 = parsed.get("base64", "")
                        return base64.b64decode(b64) if _binary else b64
                    except Exception:
                        # The body was the raw file (or raw base64) rather than
                        # the JSON envelope — an older or unexpected server.
                        if _binary:
                            return bytes(body)
                        return base64.b64encode(bytes(body)).decode("utf-8")
                except MediaExpiredError:
                    raise
                except Exception as exc:
                    logging.warning(
                        "[get_base64_from_media] request failed for %s (stream) (attempt %d/%d): %s",
                        msg_id, attempt + 1, max_attempts, exc,
                    )
                    if attempt < max_attempts - 1:
                        time.sleep(3)
                        continue
                    return ""
        return ""

    def save_audio_locally(self, msg, audio_content):
        """Encrypt and write a voice message to disk. Returns whether it worked."""
        voice_messages_dir = data_path("voice_messages")
        msg_id = msg.get('key', {}).get('id', '')
        if "_" in msg_id:
            parts = msg_id.split("_")
            msg_id = parts[2] if len(parts) > 2 else parts[-1]
        audio_file_path = os.path.join(voice_messages_dir, f"{msg_id}.msv")
        try:
            with open(audio_file_path, "wb") as audio_file:
                encrypted_audio = encrypt(audio_content, self.key)
                audio_file.write(encrypted_audio)
            return True
        except Exception as e:
            #Ignore audios that couldn't be saved for now
            return False
