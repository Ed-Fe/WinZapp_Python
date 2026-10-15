"""AutoAIMediaMixin — part of MainWindow (see main.py).

Transcribes received voice messages and describes received pictures, stickers
and videos on arrival with the online AI providers of Settings > Transcriptions
& Descriptions, and keeps the text in the message's line, when "Transcribe
received voice messages automatically" or "Describe received pictures,
stickers and videos automatically" is on (both off by default). Which messages
qualify and which providers may be used: core/ai_media/auto.py. This is the
glue: the arrival hook, the request, and storing the result.

The local counterpart is main_window/auto_transcription.py, and it shares its
rules: never a window, never a sound, never speech for a message; locked
chats are skipped; one run at a time; nothing about a message in the log.

Only providers the person consented to are used: turning the option on asks
for that consent (ui/dialogs/ai_settings_page.py), and a provider added later
is not sent anything until it has it.
"""

import logging
import time

import wx

from app_paths import global_dir
from app_settings import AppSettings
from core.ai_credentials import CredentialStore
from core.ai_media import auto as ai_auto
from core.ai_media import config as ai_config
from core.ai_media.payload import prepare_media
from core.ai_media.prompts import first_question, instructions
from core.ai_media.service import Operation, run_chain
from core.ai_media.transcript import spoken_answer
from core.transcription import auto_queue
from core.transcription import stored as stored_transcription
from core.utils import decrypt_bytes
from ui.conversation_panel.media_paths import cached_media_path


class AutoAIMediaMixin:
    """Automatic online transcription and description, for MainWindow."""

    def _ai_media_preferences(self):
        app = getattr(self, "_app_settings", None) or AppSettings(global_dir())
        return ai_config.preferences(app)

    def _maybe_auto_ai_media(self, remote_jid: str, msg: dict) -> None:
        """Queue *msg* if the online automatic options ask for it (wx thread,
        for messages that just arrived; cheap when both are off)."""
        try:
            config = self._ai_media_preferences()
            if not (config["auto_transcribe"] or config["auto_describe"]):
                return
            kind = ai_auto.wanted_kind(msg, config, auto_queue.enabled(self.settings))
            if kind is None or self.is_chat_locked(remote_jid):
                return
            if not ai_auto.usable_providers(config, kind, CredentialStore(global_dir()).saved()):
                return
            msg_id = (msg.get("key") or {}).get("id", "")
            self._auto_ai_queue_get().submit((remote_jid, msg_id), (remote_jid, msg_id, msg, kind))
        except Exception as exc:
            # Whatever this does must never cost the arrival of a message.
            logging.warning("[ai-media] could not queue an automatic request: %s",
                            type(exc).__name__)

    def _auto_ai_queue_get(self):
        queue = getattr(self, "_auto_ai_queue", None)
        if queue is None:
            queue = auto_queue.AutoTranscriptionQueue(self._run_auto_ai_media)
            self._auto_ai_queue = queue
        return queue

    def stop_auto_ai_media(self) -> None:
        """At shutdown: nothing more is sent."""
        queue = getattr(self, "_auto_ai_queue", None)
        if queue is not None:
            queue.stop()

    # ── On the queue's worker thread ─────────────────────────────────────────

    def _run_auto_ai_media(self, item) -> None:
        remote_jid, msg_id, msg, kind = item
        try:
            # Checked again: the queue may have held this for a while, and the
            # options or the chat's lock may have changed since.
            config = self._ai_media_preferences()
            if self.is_chat_locked(remote_jid):
                return
            if ai_auto.wanted_kind(dict(msg, messageTimestamp=0), config,
                                   auto_queue.enabled(self.settings)) != kind:
                return
            store = CredentialStore(global_dir())
            providers = ai_auto.usable_providers(config, kind, store.saved())
            if not providers:
                return
            msg_type = msg.get("messageType", "")
            path = cached_media_path(msg_type, msg_id)
            if not self._auto_fetch_media(msg, path):
                return
            limit = ai_config.MAX_SOURCE_BYTES[kind]
            cap = limit * 4 // 3 + 1024
            with open(path, "rb") as stream:
                encrypted = stream.read(cap + 1)
            if len(encrypted) > cap:
                return
            info = (msg.get("message") or {}).get(msg_type) or {}
            mime = str(info.get("mimetype") or "") if isinstance(info, dict) else ""
            prepared = prepare_media(kind, decrypt_bytes(encrypted, self.key), mime,
                                     config["profile"])
            language = getattr(self.i18n, "language", "")
            answer, provider = run_chain(
                Operation(kind), providers, config["models"], store.get, prepared, (),
                first_question(kind), instructions(language, config["profile"], kind),
                config["profile"])
        except Exception as exc:
            # The type and nothing else: the text of these exceptions can
            # quote a path, a key or the message.
            logging.info("[ai-media] automatic request ended without text: %s",
                         type(exc).__name__)
            return
        text = spoken_answer(answer or "")
        if text:
            wx.CallAfter(self._store_auto_ai_media, remote_jid, msg_id, text, provider, kind)

    # ── Back on the wx thread ────────────────────────────────────────────────

    def _store_auto_ai_media(self, remote_jid, msg_id, text, provider, kind) -> None:
        """Keep *text* as the inline text of the message and redraw its row."""
        current = stored_transcription.find_record(
            self._transcription_copies(remote_jid, msg_id), msg_id)
        if current is None or current.get(stored_transcription.TRANSCRIPTION_KEY) is not None:
            # The user got there first (a manual run, or a deletion).
            return
        result = type("AutoResult", (), {
            "text": text, "language": None, "language_probability": None,
            "model_id": provider, "backend": stored_transcription.ONLINE_BACKEND,
            "vad_used": True})()
        now = stored_transcription.next_decision_time(time.time())
        value = stored_transcription.with_result(None, stored_transcription.record_from_result(
            result, now, inline=True,
            kind=None if kind == "audio" else stored_transcription.DESCRIPTION_KIND))
        try:
            answer = self.store_message_transcription(remote_jid, msg_id, value)
        except Exception as exc:
            logging.warning("[ai-media] storing an automatic text failed: %s", type(exc).__name__)
            return
        if answer != stored_transcription.SAVE_STORED or self.is_chat_hidden_by_vault(remote_jid):
            return
        panel = getattr(self, "conversations_panel", None)
        conversation = getattr(panel, "conversation", None)
        if (isinstance(conversation, dict)
                and self._normalize_jid(conversation.get("remoteJid", ""))
                == self._normalize_jid(remote_jid)):
            repaint = getattr(panel, "_repaint_or_repopulate", None)
            if callable(repaint):
                try:
                    repaint([msg_id])
                except Exception as exc:
                    logging.warning("[ai-media] repainting an automatic text failed: %s",
                                    type(exc).__name__)
