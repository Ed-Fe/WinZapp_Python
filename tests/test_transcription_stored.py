"""A transcription kept with its message: surviving every write, and deletable.

A transcription takes minutes, and part 7 keeps it inside the message record
(`core.transcription.stored.TRANSCRIPTION_KEY`) so it is never done twice. The
server knows nothing of it, and everything that refreshes a conversation
writes the server's copy over the record — which is how it gets lost, or worse,
comes back after being deleted:

* **A resync erases it.** `insert_message()`, `insert_messages_batch()` and
  `import_from_dict()` (behind `save_data()`) all `INSERT OR REPLACE` the whole
  row; and `sync_chat_messages()` replaces the records in memory. Both sides
  need the rule, or it survives on disk and leaves the screen at the first
  sync — the video-duration precedent (`test_video_duration_persists.py`) paid
  for learning that.

* **Storing it undoes something else.** The flow held its copy of the message
  for minutes; writing that copy back whole would revert a delivery status, a
  reaction or an edit a sync applied meanwhile. The dedicated write touches the
  key alone, under the same lock hold as its read.

* **A deleted transcription comes back.** The same message lives in several
  dicts at once, and every one of them can be written back later. Deleting
  therefore leaves a dated tombstone, and the later decision wins everywhere.

* **A merge drops the copy that had it.** The same voice note filed under an
  `@lid` and under the phone number until the two conversations are merged;
  the merge deletes one copy, in the database and in memory, and the one it
  deletes can be the only one holding the text.

* **An own message still being sent.** Its id is a local UUID that the send
  replaces; a transcription stored under it would be orphaned, so it is refused
  and the window says so.

* **The text leaks.** It may exist only inside the encrypted payload — never in
  a clear column, never in the log, never in a request to WPPConnect.
"""

import asyncio
import inspect
import logging

import pytest

import main as main_module
from core.database import DatabaseManager
from core.transcription import narration, stored
from core.transcription.backend import TranscriptionResult
from core.transcription.stored import TRANSCRIPTION_KEY
from core.utils import MEASURED_SECONDS_KEY
from main import MainWindow
from tests.test_deep_sync import _MessagesStub, _Resp
from tests.test_historical_self_chat_guard import _DedupStub
from tests.test_lid_merge_keeps_messages import _Stub as _LidMergeStub
from tests.test_transcription_message_run import _scan, _scan_source
from ui.conversations import ConversationsPanel

_JID = "5511988887777@s.whatsapp.net"
_LID = "70000000000001@lid"
_ID = "3EB0C0FFEE00112233"
# Distinctive enough that finding it anywhere it should not be is unambiguous.
_SECRET = "zzqq o cofre fica na terceira gaveta yyww"


def _audio(mid=_ID, status=None, msg_type="audioMessage", jid=_JID, **extra):
    msg = {
        "key": {"id": mid, "remoteJid": jid, "fromMe": False},
        "messageType": msg_type,
        "messageTimestamp": 1_700_000_000,
        "message": {msg_type: {"seconds": 7, "mimetype": "audio/ogg; codecs=opus"}},
    }
    if status is not None:
        msg["status"] = status
    msg.update(extra)
    return msg


def _value(text=_SECRET, at=100.0, vad_used=True):
    return {
        "text": text, "language": "pt", "language_probability": 0.97,
        "model_id": "small", "backend": "faster_whisper", "vad_used": vad_used,
        "at": at,
    }


def _text_of(msg):
    saved = stored.saved_transcription(msg)
    return saved["text"] if saved else None


# =============================================================================
#  The rule itself
# =============================================================================


class TestWhichCopyWins:
    def test_a_copy_without_the_key_inherits_the_stored_one(self):
        merged = stored.with_known_transcription(_audio(), {TRANSCRIPTION_KEY: _value()})
        assert _text_of(merged) == _SECRET

    def test_a_newer_copy_with_the_key_wins(self):
        incoming = _audio(**{TRANSCRIPTION_KEY: _value("nova", at=200.0)})
        merged = stored.with_known_transcription(incoming, {TRANSCRIPTION_KEY: _value(at=100.0)})
        assert _text_of(merged) == "nova"

    def test_an_older_copy_with_the_key_loses(self):
        """A stale copy in memory must not undo a transcription made again."""
        incoming = _audio(**{TRANSCRIPTION_KEY: _value("velha", at=100.0)})
        merged = stored.with_known_transcription(incoming, {TRANSCRIPTION_KEY: _value("nova", at=200.0)})
        assert _text_of(merged) == "nova"

    def test_a_tombstone_beats_an_older_text(self):
        incoming = _audio(**{TRANSCRIPTION_KEY: _value(at=100.0)})
        merged = stored.with_known_transcription(incoming, {TRANSCRIPTION_KEY: stored.tombstone(150.0)})
        assert _text_of(merged) is None
        assert merged[TRANSCRIPTION_KEY]["deleted"] is True

    def test_a_newer_text_beats_a_tombstone(self):
        incoming = _audio(**{TRANSCRIPTION_KEY: _value(at=300.0)})
        merged = stored.with_known_transcription(incoming, {TRANSCRIPTION_KEY: stored.tombstone(150.0)})
        assert _text_of(merged) == _SECRET

    def test_a_copy_that_is_no_longer_audio_drops_the_key(self):
        """A message deleted for everyone comes back as a protocolMessage: the
        text of what its sender withdrew is not kept."""
        revoked = _audio(msg_type="protocolMessage", **{TRANSCRIPTION_KEY: _value()})
        merged = stored.with_known_transcription(revoked, {TRANSCRIPTION_KEY: _value()})
        assert TRANSCRIPTION_KEY not in merged

    def test_a_type_passed_through_unmapped_leaves_the_stored_one_as_it_is(self):
        """websocket_client hands on a type it does not map (a `ciphertext`
        placeholder of a note not decrypted yet): it may be the voice note
        itself, and writing it must not erase the transcription for good."""
        placeholder = _audio(msg_type="ciphertext")
        merged = stored.with_known_transcription(placeholder, {TRANSCRIPTION_KEY: _value()})
        assert merged[TRANSCRIPTION_KEY] == _value()

    def test_nor_does_it_drop_or_replace_with_its_own(self):
        """Neither adopted nor dropped: what is stored stays, and a copy of
        such a type holding one with nothing stored keeps it."""
        own = _audio(msg_type="ciphertext", **{TRANSCRIPTION_KEY: _value("da copia", at=900.0)})
        assert _text_of(stored.with_known_transcription(own, {TRANSCRIPTION_KEY: _value(at=1.0)})) == _SECRET
        assert _text_of(stored.with_known_transcription(own, None)) == "da copia"

    def test_a_known_other_kind_is_left_as_it_came(self):
        image = _audio(msg_type="imageMessage")
        assert stored.with_known_transcription(image, {TRANSCRIPTION_KEY: _value()}) is image

    @pytest.mark.parametrize("junk", [None, "texto", 42, {"text": "sem data"}, {"at": True}])
    def test_junk_under_the_key_never_outranks_a_real_value(self, junk):
        merged = stored.with_known_transcription(_audio(**{TRANSCRIPTION_KEY: junk}),
                                                 {TRANSCRIPTION_KEY: _value()})
        assert _text_of(merged) == _SECRET

    def test_the_callers_dict_is_never_changed(self):
        mine = _audio()
        stored.with_known_transcription(mine, {TRANSCRIPTION_KEY: _value()})
        assert TRANSCRIPTION_KEY not in mine

    def test_a_document_carries_it_by_type_even_without_a_mimetype(self):
        doc = _audio(msg_type="documentMessage")
        doc["message"]["documentMessage"] = {}
        assert _text_of(stored.with_known_transcription(doc, {TRANSCRIPTION_KEY: _value()})) == _SECRET


class TestTheTimeOfADecision:
    def test_the_clock_when_it_is_ahead_of_everything(self):
        assert stored.next_decision_time(500.0, _value(at=100.0), None) == 500.0

    def test_after_the_latest_when_the_clock_went_back(self):
        at = stored.next_decision_time(50.0, _value(at=100.0), stored.tombstone(300.0), "junk")
        assert at > 300.0
        assert stored.newer_decision(stored.tombstone(at), stored.tombstone(300.0))["at"] == at


class TestWhatIsStored:
    def test_the_fields_needed_to_reopen_it_truthfully_and_no_segments(self):
        result = TranscriptionResult(
            text=_SECRET, language="pl", language_probability=0.4, duration_seconds=9.0,
            segments=("x",), backend="faster_whisper", model_id="medium", device="cpu",
            compute_type="int8", vad_used=False,
        )
        value = stored.record_from_result(result, 123.5)
        assert value == {
            "text": _SECRET, "language": "pl", "language_probability": 0.4,
            "model_id": "medium", "backend": "faster_whisper", "vad_used": False,
            "at": 123.5,
        }

    def test_a_reopened_transcription_made_without_the_filter_still_warns(self):
        result = TranscriptionResult(text=_SECRET, language="pt", language_probability=0.9,
                                     duration_seconds=None, vad_used=False)
        reopened = stored.as_result(stored.record_from_result(result, 1.0))
        keys = [n.i18n_key for n in narration.result_notes(reopened, "pt")]
        assert narration.VAD_UNAVAILABLE_I18N_KEY in keys

    def test_a_value_that_lost_vad_used_is_treated_as_unfiltered(self):
        value = _value()
        del value["vad_used"]
        assert stored.as_result(value).vad_used is False

    @pytest.mark.parametrize("value", [stored.tombstone(5.0), _value(text="   "), "texto", None])
    def test_nothing_readable_is_not_a_saved_transcription(self, value):
        assert stored.saved_transcription(_audio(**{TRANSCRIPTION_KEY: value})) is None


# =============================================================================
#  In memory — carry_over_transcriptions()
# =============================================================================


class TestCarryOverInMemory:
    def test_the_server_copy_replacing_the_record_keeps_it(self):
        server = [_audio()]
        assert stored.carry_over_transcriptions(server, [_audio(**{TRANSCRIPTION_KEY: _value()})]) == 1
        assert _text_of(server[0]) == _SECRET

    def test_a_tombstone_travels_too(self):
        server = [_audio()]
        stored.carry_over_transcriptions(server, [_audio(**{TRANSCRIPTION_KEY: stored.tombstone(9.0)})])
        assert server[0][TRANSCRIPTION_KEY]["deleted"] is True

    def test_the_count_is_of_transcriptions_not_of_tombstones(self):
        """It is what sync_chat_messages() logs as "saved transcription(s)"."""
        server = [_audio("A"), _audio("B")]
        carried = stored.carry_over_transcriptions(server, [
            _audio("A", **{TRANSCRIPTION_KEY: _value()}),
            _audio("B", **{TRANSCRIPTION_KEY: stored.tombstone(9.0)}),
        ])
        assert carried == 1
        assert server[1][TRANSCRIPTION_KEY]["deleted"] is True

    def test_the_newer_of_snapshot_and_live_records_is_kept(self):
        """sync_chat_messages() carries from the snapshot first and from the
        live records after; a delete landing between the two must win."""
        server = [_audio()]
        stored.carry_over_transcriptions(server, [_audio(**{TRANSCRIPTION_KEY: _value(at=1.0)})])
        stored.carry_over_transcriptions(server, [_audio(**{TRANSCRIPTION_KEY: stored.tombstone(2.0)})])
        assert _text_of(server[0]) is None

    @pytest.mark.parametrize("junk", [None, [], [None, "x", 3]])
    def test_junk_is_tolerated(self, junk):
        assert stored.carry_over_transcriptions(junk, junk) == 0

    def test_every_path_that_replaces_records_calls_it(self):
        """An extra net under the behaviour tests below: the sync (both
        merges) and the reload from the database when a conversation opens."""
        sync = inspect.getsource(MainWindow.sync_chat_messages)
        assert sync.count("carry_over_transcriptions(") == 2
        opening = inspect.getsource(ConversationsPanel.navigate_to_conversation)
        assert "self._load_conversation_page_from_db(conversation)" in opening


class TestTheSyncKeepsIt:
    """MainWindow.sync_chat_messages() itself, on test_deep_sync's stub."""

    def _stub(self, monkeypatch, local_records, api_records):
        monkeypatch.setattr(main_module.requests, "get",
                            lambda url, **kwargs: _Resp(200, {"response": api_records}))
        stub = _MessagesStub([])
        stub._normalize_fetched_messages = lambda raw, _jid: [dict(m) for m in raw]
        stub.chats = {_JID: {"remoteJid": _JID, "t": 100,
                             "messages": {"messages": {"records": local_records}}}}
        return stub

    @staticmethod
    def _synced(stub, mid=_ID):
        [record] = [r for r in stub.chats[_JID]["messages"]["messages"]["records"]
                    if r["key"]["id"] == mid]
        return record

    def test_the_server_copy_takes_the_local_text(self, monkeypatch):
        stub = self._stub(monkeypatch, [_audio(**{TRANSCRIPTION_KEY: _value()})], [_audio()])
        MainWindow.sync_chat_messages(stub, dict(stub.chats[_JID]))
        assert _text_of(self._synced(stub)) == _SECRET

    def test_the_snapshot_counts_even_when_the_live_chat_lost_it(self, monkeypatch):
        """Another write swapped the live chat for server copies during the
        fetch: only the snapshot still holds the text, so only the first
        carry-over can keep it."""
        stub = self._stub(monkeypatch,
                          [_audio(**{TRANSCRIPTION_KEY: _value()}), _audio("LOCAL-ONLY")],
                          [_audio()])

        def _swap(remote_jid, message):
            if message["key"]["id"] == "LOCAL-ONLY":
                stub.chats[_JID] = {"remoteJid": _JID, "messages": {"messages": {"records": [_audio()]}}}
            return False

        stub._is_cleared_message = _swap
        MainWindow.sync_chat_messages(stub, dict(stub.chats[_JID]))
        assert _text_of(self._synced(stub)) == _SECRET

    def test_a_delete_during_the_fetch_wins(self, monkeypatch):
        """The tombstone lands on the live record after the snapshot was
        carried over: _is_cleared_message() runs between the two."""
        live = _audio(**{TRANSCRIPTION_KEY: _value(at=100.0)})
        stub = self._stub(monkeypatch, [live, _audio("LOCAL-ONLY")], [_audio()])

        def _delete_meanwhile(remote_jid, message):
            if message["key"]["id"] == "LOCAL-ONLY":
                stored.set_on_copies([live], _ID, stored.tombstone(200.0))
            return False

        stub._is_cleared_message = _delete_meanwhile
        MainWindow.sync_chat_messages(stub, dict(stub.chats[_JID]))
        synced = self._synced(stub)
        assert _text_of(synced) is None
        assert synced[TRANSCRIPTION_KEY]["deleted"] is True


class _OpeningDb:
    def __init__(self, rows):
        self.rows = rows

    def get_messages(self, jid, limit=200):
        return [dict(r) for r in reversed(self.rows)]

    def get_message_count(self, jid):
        return len(self.rows)


class _OpeningWindow:
    def __init__(self, rows):
        self.settings = {"user_interface": {"messages_page_size": 200}}
        self.db = _OpeningDb(rows)


class _OpeningPanel:
    _load_conversation_page_from_db = ConversationsPanel._load_conversation_page_from_db

    def __init__(self, rows):
        self.main_window = _OpeningWindow(rows)


class TestOpeningAConversationKeepsIt:
    """What navigate_to_conversation() reloads from the database, which can
    be a moment behind a decision still being written."""

    def test_the_records_in_hand_give_their_text_to_the_database_page(self):
        conversation = {"remoteJid": _JID, "messages": {"messages": {"records": [
            _audio(**{TRANSCRIPTION_KEY: _value()})]}}}
        _OpeningPanel([_audio()])._load_conversation_page_from_db(conversation)
        [record] = conversation["messages"]["messages"]["records"]
        assert _text_of(record) == _SECRET

    def test_and_a_delete_not_yet_written_is_not_undone(self):
        conversation = {"remoteJid": _JID, "messages": {"messages": {"records": [
            _audio(**{TRANSCRIPTION_KEY: stored.tombstone(200.0)})]}}}
        _OpeningPanel([_audio(**{TRANSCRIPTION_KEY: _value(at=100.0)})]) \
            ._load_conversation_page_from_db(conversation)
        [record] = conversation["messages"]["messages"]["records"]
        assert _text_of(record) is None


# =============================================================================
#  In the database
# =============================================================================


async def _row(db, mid=_ID):
    cursor = await db._conn.execute(
        "SELECT * FROM messages WHERE remote_jid=? AND message_id=?", (_JID, mid)
    )
    return await cursor.fetchone()


async def _stored_record(db, mid=_ID, jid=_JID):
    [msg] = [m for m in await db.get_messages(jid) if m["key"]["id"] == mid]
    return msg


async def _stored_text(db, mid=_ID, jid=_JID):
    return _text_of(await _stored_record(db, mid, jid))


class TestTheDatabaseKeepsIt:
    async def test_a_batch_resync_without_the_key_keeps_it(self, in_memory_db):
        await in_memory_db.insert_message(_JID, _audio())
        assert await in_memory_db.set_message_transcription(_JID, _ID, _value())

        await in_memory_db.insert_messages_batch(_JID, [_audio(), _audio("OUTRO")])

        assert await _stored_text(in_memory_db) == _SECRET

    async def test_a_single_insert_keeps_it(self, in_memory_db):
        await in_memory_db.insert_message(_JID, _audio())
        await in_memory_db.set_message_transcription(_JID, _ID, _value())

        await in_memory_db.insert_message(_JID, _audio(status=4))

        assert await _stored_text(in_memory_db) == _SECRET

    async def test_the_full_state_save_keeps_it(self, in_memory_db):
        """save_data() goes through import_from_dict(clear_first=False) with
        whatever memory holds — which may be a copy older than the row."""
        await in_memory_db.insert_message(_JID, _audio())
        await in_memory_db.set_message_transcription(_JID, _ID, _value())

        await in_memory_db.import_from_dict({"chats": {_JID: {
            "remoteJid": _JID, "messages": {"messages": {"records": [_audio()]}},
        }}}, clear_first=False)

        assert await _stored_text(in_memory_db) == _SECRET

    async def test_a_newer_copy_with_the_key_wins(self, in_memory_db):
        await in_memory_db.insert_message(_JID, _audio(**{TRANSCRIPTION_KEY: _value("velha", at=1.0)}))
        await in_memory_db.insert_message(_JID, _audio(**{TRANSCRIPTION_KEY: _value("nova", at=2.0)}))
        assert await _stored_text(in_memory_db) == "nova"

    async def test_storing_does_not_undo_a_status_that_arrived_meanwhile(self, in_memory_db):
        """The flow's copy said "sent"; a sync stored "read" during the run."""
        await in_memory_db.insert_message(_JID, _audio(status=2))
        await in_memory_db.insert_message(_JID, _audio(status=4))

        await in_memory_db.set_message_transcription(_JID, _ID, _value())

        row = await _row(in_memory_db)
        assert row["status"] == 4
        [msg] = await in_memory_db.get_messages(_JID)
        assert msg["status"] == 4 and _text_of(msg) == _SECRET

    async def test_a_missing_row_is_an_answer(self, in_memory_db):
        assert await in_memory_db.set_message_transcription(_JID, _ID, _value()) is False
        assert await in_memory_db.delete_message_transcription(_JID, _ID, 5.0) is False
        assert await in_memory_db.get_messages(_JID) == []

    async def test_a_promoted_own_message_is_not_re_keyed(self, in_memory_db):
        """update_message_id() renames the column only; the JSON still says
        the local id. Writing through INSERT OR REPLACE would re-create the row
        under that old id."""
        own = _audio("LOCAL-UUID")
        own["key"]["fromMe"] = True
        await in_memory_db.insert_message(_JID, own)
        await in_memory_db.update_message_id(_JID, "LOCAL-UUID", "REAL")

        assert await in_memory_db.set_message_transcription(_JID, "REAL", _value())

        cursor = await in_memory_db._conn.execute("SELECT message_id FROM messages")
        assert [r["message_id"] for r in await cursor.fetchall()] == ["REAL"]

    async def test_one_read_per_message_and_none_for_text(self, in_memory_db, monkeypatch):
        reads = []
        original = DatabaseManager._stored_message

        async def _counting(self, conn, remote_jid, message_id):
            reads.append(message_id)
            return await original(self, conn, remote_jid, message_id)

        monkeypatch.setattr(DatabaseManager, "_stored_message", _counting)
        text = {"key": {"id": "T1", "remoteJid": _JID}, "messageType": "conversation",
                "messageTimestamp": 1, "message": {"conversation": "oi"}}
        await in_memory_db.insert_messages_batch(_JID, [_audio("A1"), _audio("A2"), text])
        assert sorted(reads) == ["A1", "A2"]

    async def test_the_read_and_the_write_are_one_lock_hold(self, in_memory_db, monkeypatch):
        """A sync writing the row while the transcription is being stored:
        with the lock released in between, the store would write back the
        row it read before the sync and the new status would be gone."""
        await in_memory_db.insert_message(_JID, _audio(status=2))
        original = DatabaseManager._stored_message
        state = {"sync": None}

        async def _slow_read(self, conn, remote_jid, message_id):
            found = await original(self, conn, remote_jid, message_id)
            if state["sync"] is None:
                state["sync"] = asyncio.ensure_future(self.insert_message(_JID, _audio(status=4)))
                for _ in range(20):
                    await asyncio.sleep(0)
            return found

        monkeypatch.setattr(DatabaseManager, "_stored_message", _slow_read)
        await in_memory_db.set_message_transcription(_JID, _ID, _value())
        await state["sync"]

        [msg] = await in_memory_db.get_messages(_JID)
        assert msg["status"] == 4
        assert _text_of(msg) == _SECRET


    async def test_an_unmapped_copy_does_not_erase_it(self, in_memory_db):
        await in_memory_db.insert_message(_JID, _audio())
        await in_memory_db.set_message_transcription(_JID, _ID, _value())

        await in_memory_db.insert_message(_JID, _audio(msg_type="ciphertext"))
        await in_memory_db.insert_message(_JID, _audio())

        assert await _stored_text(in_memory_db) == _SECRET

    async def test_a_withdrawn_copy_still_drops_it(self, in_memory_db):
        await in_memory_db.insert_message(_JID, _audio())
        await in_memory_db.set_message_transcription(_JID, _ID, _value())
        await in_memory_db.insert_message(_JID, _audio(msg_type="protocolMessage"))
        assert TRANSCRIPTION_KEY not in await _stored_record(in_memory_db)

    async def test_what_is_read_before_a_write(self, in_memory_db, monkeypatch):
        """Audio, documents and unmapped types need the row; a known other
        kind cannot be the same message and costs no read."""
        reads = []
        original = DatabaseManager._stored_message

        async def _counting(self, conn, remote_jid, message_id):
            reads.append(message_id)
            return await original(self, conn, remote_jid, message_id)

        monkeypatch.setattr(DatabaseManager, "_stored_message", _counting)
        await in_memory_db.insert_messages_batch(_JID, [
            _audio("IMG", msg_type="imageMessage"), _audio("CIPHER", msg_type="ciphertext"),
            _audio("DOC", msg_type="documentMessage"), _audio("GONE", msg_type="protocolMessage"),
        ])
        assert sorted(reads) == ["CIPHER", "DOC"]

    async def test_the_full_state_save_reads_per_chat_not_per_message(self, in_memory_db, monkeypatch):
        """save_data() used to pay one SELECT per voice note in memory, under
        the write lock; the same rule now costs one per conversation."""
        records = [_audio(f"A{i}") for i in range(40)]
        await in_memory_db.insert_messages_batch(_JID, records)
        await in_memory_db.set_message_transcription(_JID, "A7", _value())
        conn = in_memory_db._conn
        original = conn.execute
        selects = []

        def _counting(sql, *args, **kwargs):
            if sql.lstrip().upper().startswith("SELECT") and "FROM messages" in sql:
                selects.append(sql)
            return original(sql, *args, **kwargs)

        monkeypatch.setattr(conn, "execute", _counting)
        await in_memory_db.import_from_dict({"chats": {_JID: {
            "remoteJid": _JID, "messages": {"messages": {"records": records}},
        }}}, clear_first=False)
        monkeypatch.undo()

        assert len(selects) == 1
        assert await _stored_text(in_memory_db, "A7") == _SECRET

    async def test_the_full_state_save_keeps_a_measured_duration_too(self, in_memory_db):
        video = {"key": {"id": "V1", "remoteJid": _JID, "fromMe": False},
                 "messageType": "videoMessage", "messageTimestamp": 1,
                 "message": {"videoMessage": {"seconds": 0}}}
        measured = {**video, "message": {"videoMessage": {"seconds": 0, MEASURED_SECONDS_KEY: 42}}}
        await in_memory_db.insert_message(_JID, measured)
        await in_memory_db.import_from_dict({"chats": {_JID: {
            "remoteJid": _JID, "messages": {"messages": {"records": [video]}},
        }}}, clear_first=False)
        stored_video = (await _stored_record(in_memory_db, "V1"))["message"]["videoMessage"]
        assert stored_video[MEASURED_SECONDS_KEY] == 42

    async def test_an_explicit_decision_is_later_than_the_row_whatever_the_clock(self, in_memory_db):
        """The clock went back between transcribing and deleting: a tombstone
        dated before the text would lose to the next stale copy written."""
        await in_memory_db.insert_message(_JID, _audio())
        await in_memory_db.set_message_transcription(_JID, _ID, _value(at=100.0))

        assert await in_memory_db.delete_message_transcription(_JID, _ID, 50.0)
        await in_memory_db.insert_message(_JID, _audio(**{TRANSCRIPTION_KEY: _value(at=100.0)}))

        record = await _stored_record(in_memory_db)
        assert _text_of(record) is None
        assert record[TRANSCRIPTION_KEY]["at"] > 100.0

    async def test_transcribing_again_with_the_clock_behind_still_replaces_it(self, in_memory_db):
        await in_memory_db.insert_message(_JID, _audio())
        await in_memory_db.set_message_transcription(_JID, _ID, _value("velha", at=100.0))
        await in_memory_db.set_message_transcription(_JID, _ID, _value("nova", at=10.0))
        assert await _stored_text(in_memory_db) == "nova"


class TestTheChatPreviewNeverHoldsIt:
    """chats.last_message_json is written by the debounced upsert_chat(),
    which can land after "Transcrição apagada" has been said."""

    @staticmethod
    async def _preview(db):
        cursor = await db._conn.execute("SELECT last_message_json FROM chats WHERE jid=?", (_JID,))
        row = await cursor.fetchone()
        return db._decrypt_json(row["last_message_json"])

    async def test_no_chat_write_puts_it_there(self, in_memory_db):
        chat = {"remoteJid": _JID, "lastMessage": _audio(**{TRANSCRIPTION_KEY: _value()})}
        await in_memory_db.upsert_chat(_JID, chat)
        assert TRANSCRIPTION_KEY not in await self._preview(in_memory_db)
        await in_memory_db.upsert_chats_batch({_JID: chat})
        assert TRANSCRIPTION_KEY not in await self._preview(in_memory_db)
        await in_memory_db.import_from_dict({"chats": {_JID: chat}}, clear_first=False)
        assert TRANSCRIPTION_KEY not in await self._preview(in_memory_db)
        assert TRANSCRIPTION_KEY in chat["lastMessage"], "the caller's dict is untouched"

    async def test_deleting_scrubs_a_preview_written_before(self, in_memory_db):
        await in_memory_db.insert_message(_JID, _audio())
        await in_memory_db.set_message_transcription(_JID, _ID, _value())
        await in_memory_db.upsert_chat(_JID, {"remoteJid": _JID})
        await in_memory_db._conn.execute(
            "UPDATE chats SET last_message_json=? WHERE jid=?",
            (in_memory_db._encrypt_json(_audio(**{TRANSCRIPTION_KEY: _value()})), _JID),
        )
        await in_memory_db._conn.commit()

        assert await in_memory_db.delete_message_transcription(_JID, _ID, 500.0)

        assert TRANSCRIPTION_KEY not in await self._preview(in_memory_db)
        assert (await self._preview(in_memory_db))["key"]["id"] == _ID


class TestMergingTwoChatsKeepsIt:
    """The blocker: a voice note filed under the @lid (live) and under the
    phone (sync), transcribed in the @lid conversation, then the @lid
    resolved and the two merged — the row holding the text was the one
    dropped for "having a twin"."""

    async def _both(self, db, lid_value=None, phone_value=None):
        await db.insert_message(_LID, _audio(jid=_LID))
        await db.insert_message(_JID, _audio())
        if lid_value is not None:
            assert await db.set_message_transcription(_LID, _ID, lid_value)
        if phone_value is not None:
            assert await db.set_message_transcription(_JID, _ID, phone_value)

    async def test_the_text_on_the_dropped_copy_survives(self, in_memory_db):
        await self._both(in_memory_db, lid_value=_value())
        await in_memory_db.merge_or_rename_chat(_LID, _JID)
        assert await in_memory_db.get_messages(_LID) == []
        assert await _stored_text(in_memory_db) == _SECRET

    async def test_a_tombstone_on_the_dropped_copy_still_wins(self, in_memory_db):
        await self._both(in_memory_db, lid_value=stored.tombstone(200.0),
                         phone_value=_value(at=100.0))
        await in_memory_db.merge_or_rename_chat(_LID, _JID)
        record = await _stored_record(in_memory_db)
        assert _text_of(record) is None and record[TRANSCRIPTION_KEY]["deleted"] is True

    async def test_the_later_of_two_texts_is_kept(self, in_memory_db):
        await self._both(in_memory_db, lid_value=_value("velha", at=100.0),
                         phone_value=_value("nova", at=300.0))
        await in_memory_db.merge_or_rename_chat(_LID, _JID)
        assert await _stored_text(in_memory_db) == "nova"

    async def test_a_measured_video_duration_survives_too(self, in_memory_db):
        def _video(jid, measured=None):
            video = {"seconds": 0}
            if measured is not None:
                video[MEASURED_SECONDS_KEY] = measured
            return {"key": {"id": "V1", "remoteJid": jid, "fromMe": False},
                    "messageType": "videoMessage", "messageTimestamp": 1,
                    "message": {"videoMessage": video}}

        await in_memory_db.insert_message(_LID, _video(_LID, measured=42))
        await in_memory_db.insert_message(_JID, _video(_JID))
        await in_memory_db.merge_or_rename_chat(_LID, _JID)
        stored_video = (await _stored_record(in_memory_db, "V1"))["message"]["videoMessage"]
        assert stored_video[MEASURED_SECONDS_KEY] == 42

    async def test_the_survivor_is_read_only_when_the_twin_carries_something(
            self, in_memory_db, monkeypatch):
        """The step runs on every @lid resolution."""
        for mid in ("A", "B", "C"):
            await in_memory_db.insert_message(_LID, _audio(mid, jid=_LID))
            await in_memory_db.insert_message(_JID, _audio(mid))
        await in_memory_db.set_message_transcription(_LID, "B", _value())
        reads = []
        original = DatabaseManager._stored_message

        async def _counting(self, conn, remote_jid, message_id):
            reads.append(message_id)
            return await original(self, conn, remote_jid, message_id)

        monkeypatch.setattr(DatabaseManager, "_stored_message", _counting)
        await in_memory_db.merge_or_rename_chat(_LID, _JID)
        assert reads == ["B"]
        assert await _stored_text(in_memory_db, "B") == _SECRET


@pytest.fixture
def synchronous_merge(monkeypatch):
    class _SyncThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            self._run = lambda: target(*args, **(kwargs or {}))

        def start(self):
            self._run()

    monkeypatch.setattr(main_module.threading, "Thread", _SyncThread)


def _chat_of(jid, *records):
    return {"remoteJid": jid, "messages": {"messages": {"records": list(records)}}}


class TestMergingInMemoryKeepsIt:
    def test_the_lid_copy_gives_its_text_to_the_phone_record(self, synchronous_merge):
        phone_record = _audio()
        stub = _LidMergeStub({
            _LID: _chat_of(_LID, _audio(jid=_LID, **{TRANSCRIPTION_KEY: _value()})),
            _JID: _chat_of(_JID, phone_record),
        })
        stub._merge_lid_into_phone(_LID, _JID)
        assert _LID not in stub.chats
        [record] = stub.chats[_JID]["messages"]["messages"]["records"]
        assert record is phone_record, "the dict the panel may hold, changed in place"
        assert _text_of(record) == _SECRET

    def test_and_a_tombstone_on_it_wins(self, synchronous_merge):
        phone_record = _audio(**{TRANSCRIPTION_KEY: _value(at=100.0)})
        stub = _LidMergeStub({
            _LID: _chat_of(_LID, _audio(jid=_LID, **{TRANSCRIPTION_KEY: stored.tombstone(200.0)})),
            _JID: _chat_of(_JID, phone_record),
        })
        stub._merge_lid_into_phone(_LID, _JID)
        assert _text_of(phone_record) is None

    def test_a_measured_duration_travels_the_same_way(self, synchronous_merge):
        def _video(jid, **video):
            return {"key": {"id": "V1", "remoteJid": jid}, "messageType": "videoMessage",
                    "message": {"videoMessage": {"seconds": 0, **video}}}

        phone_record = _video(_JID)
        stub = _LidMergeStub({
            _LID: _chat_of(_LID, _video(_LID, **{MEASURED_SECONDS_KEY: 42})),
            _JID: _chat_of(_JID, phone_record),
        })
        stub._merge_lid_into_phone(_LID, _JID)
        assert phone_record["message"]["videoMessage"][MEASURED_SECONDS_KEY] == 42

    def test_deduplicate_chats_keeps_it_too(self):
        cus = _JID.replace("@s.whatsapp.net", "@c.us")
        phone_record = _audio()
        out = _DedupStub().deduplicate_chats({
            cus: _chat_of(cus, _audio(jid=cus, **{TRANSCRIPTION_KEY: _value()})),
            _JID: _chat_of(_JID, phone_record),
        })
        [record] = out[_JID]["messages"]["messages"]["records"]
        assert record is phone_record
        assert _text_of(record) == _SECRET


class TestDeletingDoesNotResurrect:
    async def _deleted(self, db):
        await db.insert_message(_JID, _audio())
        await db.set_message_transcription(_JID, _ID, _value(at=100.0))
        assert await db.delete_message_transcription(_JID, _ID, 200.0)

    async def test_it_is_gone(self, in_memory_db):
        await self._deleted(in_memory_db)
        assert await _stored_text(in_memory_db) is None

    async def test_a_stale_copy_written_whole_does_not_bring_it_back(self, in_memory_db):
        """A star toggled on the panel's old dict, a status echo — any path
        that rewrites the record from memory."""
        await self._deleted(in_memory_db)
        await in_memory_db.insert_message(_JID, _audio(**{TRANSCRIPTION_KEY: _value(at=100.0)}))
        assert await _stored_text(in_memory_db) is None

    async def test_nor_does_a_batch_or_a_full_save(self, in_memory_db):
        await self._deleted(in_memory_db)
        stale = _audio(**{TRANSCRIPTION_KEY: _value(at=100.0)})
        await in_memory_db.insert_messages_batch(_JID, [stale])
        await in_memory_db.import_from_dict({"chats": {_JID: {
            "remoteJid": _JID, "messages": {"messages": {"records": [stale]}},
        }}}, clear_first=False)
        assert await _stored_text(in_memory_db) is None

    async def test_transcribing_again_after_deleting_is_kept(self, in_memory_db):
        await self._deleted(in_memory_db)
        await in_memory_db.set_message_transcription(_JID, _ID, _value("de novo", at=300.0))
        assert await _stored_text(in_memory_db) == "de novo"


class TestTheTextIsOnlyEverEncrypted:
    async def test_no_column_holds_it_in_the_clear(self, in_memory_db):
        await in_memory_db.insert_message(_JID, _audio())
        await in_memory_db.set_message_transcription(_JID, _ID, _value())

        row = await _row(in_memory_db)
        for column in row.keys():
            assert "cofre" not in str(row[column]), column

    async def test_nor_does_the_database_file(self, tmp_path, fernet_key):
        path = tmp_path / "messages.db"
        async with DatabaseManager(str(path), fernet_key) as db:
            await db.insert_message(_JID, _audio())
            await db.set_message_transcription(_JID, _ID, _value())
            assert await _stored_text(db) == _SECRET
        blob = b"".join(p.read_bytes() for p in tmp_path.iterdir() if p.name.startswith("messages.db"))
        assert b"cofre" not in blob
        assert "cofre".encode("utf-16-le") not in blob


# =============================================================================
#  MainWindow: finding the message again, and every copy of it
# =============================================================================


class _Inline:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


class _RecordingDb:
    """The bridge's calls, recorded. insert_message is here only to catch a
    regression that writes the whole record: the store runs on a background
    executor that logs and swallows, so a missing method would pass silently."""

    def __init__(self):
        self.calls = []
        self.values = []
        self.fail = False
        self.fail_insert = False
        #: The (jid, id) rows that exist; None means every row does.
        self.rows = None

    def set_message_transcription(self, jid, msg_id, value):
        self.calls.append(("set", jid, msg_id))
        self.values.append(value)
        if self.fail:
            raise TimeoutError("busy")
        return self.has_row(jid, msg_id)

    def delete_message_transcription(self, jid, msg_id, deleted_at):
        self.calls.append(("delete", jid, msg_id))
        self.values.append(deleted_at)
        if self.fail:
            raise TimeoutError("busy")
        return self.has_row(jid, msg_id)

    def insert_message(self, jid, msg):
        self.calls.append(("insert", jid, (msg.get("key") or {}).get("id")))
        self.values.append(msg)
        if self.fail_insert:
            raise TimeoutError("busy")

    def has_row(self, jid, msg_id):
        return self.rows is None or (jid, msg_id) in self.rows


class _Panel:
    def __init__(self, conversation, all_sorted=(), sorted_=()):
        self.conversation = conversation
        self._all_sorted_messages = list(all_sorted)
        self._sorted_messages = list(sorted_)


class _Sound:
    def __init__(self):
        self.played = 0

    def play(self):
        self.played += 1


class _KeysI18n:
    @staticmethod
    def t(key):
        return key


class _Window:
    def __init__(self, records, panel=None):
        self.chats = {_JID: {"remoteJid": _JID, "messages": {"messages": {"records": records}}}}
        self.conversations_panel = panel
        self.db = _RecordingDb()
        self._msg_bg_executor = _Inline()
        self._phone_to_lid = {}
        self.i18n = _KeysI18n()
        self.error_sound = _Sound()
        self.spoken = []
        self.saves = []

    _normalize_jid = staticmethod(MainWindow._normalize_jid)
    get_chat = MainWindow.get_chat
    _transcription_copies = MainWindow._transcription_copies
    _transcription_storage_jids = MainWindow._transcription_storage_jids
    _say_transcription_not_stored = MainWindow._say_transcription_not_stored
    store_message_transcription = MainWindow.store_message_transcription
    delete_message_transcription = MainWindow.delete_message_transcription

    def _schedule_save(self, dirty_jid=None, contacts_dirty=False):
        self.saves.append(dirty_jid)

    def output(self, text, interrupt=False):
        self.spoken.append(text)


@pytest.fixture
def inline_call_after(monkeypatch):
    monkeypatch.setattr(main_module.wx, "CallAfter", lambda fn, *a, **k: fn(*a, **k))


class TestStoringFromMainWindow:
    def test_the_current_record_gets_it_not_the_one_the_flow_held(self):
        """A sync replaced the dict during the run; the new one is what the
        menu reads now."""
        held_by_the_flow = _audio()
        current = _audio(status=4)
        window = _Window([current])

        answer = window.store_message_transcription(_JID, _ID, _value())

        assert answer == stored.SAVE_STORED
        assert _text_of(current) == _SECRET
        assert TRANSCRIPTION_KEY not in held_by_the_flow
        assert window.db.calls == [("set", _JID, _ID)]
        assert window.saves == [_JID]

    def test_every_copy_is_updated(self):
        in_chat = _audio()
        panel_copy = _audio()
        page_copy = _audio()
        window = _Window([in_chat])
        window.chats[_JID]["lastMessage"] = in_chat
        window.conversations_panel = _Panel(
            {"remoteJid": _JID, "messages": {"messages": {"records": [panel_copy]}}},
            all_sorted=[panel_copy, page_copy], sorted_=[page_copy],
        )

        window.store_message_transcription(_JID, _ID, _value())

        for copy in (in_chat, panel_copy, page_copy):
            assert _text_of(copy) == _SECRET

    def test_another_conversations_panel_is_left_alone(self):
        other = _audio()
        window = _Window([_audio()])
        window.conversations_panel = _Panel({"remoteJid": "outro@g.us"}, all_sorted=[other])
        window.store_message_transcription(_JID, _ID, _value())
        assert TRANSCRIPTION_KEY not in other

    def test_a_message_still_being_sent_is_refused(self):
        pending = _audio("LOCAL-UUID", _local_pending=True, _local_id="LOCAL-UUID")
        window = _Window([pending])

        assert window.store_message_transcription(_JID, "LOCAL-UUID", _value()) == stored.SAVE_UNSENT
        assert TRANSCRIPTION_KEY not in pending
        assert window.db.calls == []

    def test_the_pending_flag_alone_is_enough(self):
        """A record that says it is still being sent is believed even when it
        lacks the _local_id the other check compares against."""
        pending = _audio(_local_pending=True)
        window = _Window([pending])
        assert window.store_message_transcription(_JID, _ID, _value()) == stored.SAVE_UNSENT
        assert TRANSCRIPTION_KEY not in pending

    def test_a_record_still_keyed_by_its_local_id_is_refused_too(self):
        """No longer flagged pending, but the send never gave it a WhatsApp
        id: `key.id` is still the `_local_id`, which is what refuses it."""
        unconfirmed = _audio("LOCAL-UUID", _local_pending=False, _local_id="LOCAL-UUID")
        window = _Window([unconfirmed])
        assert window.store_message_transcription(_JID, "LOCAL-UUID", _value()) == stored.SAVE_UNSENT
        assert TRANSCRIPTION_KEY not in unconfirmed
        assert window.db.calls == []

    def test_a_message_sent_during_the_run_is_stored_under_its_real_id(self):
        """The flow captured the local UUID; by the end the send gave the
        record its real id. Found through _local_id, written under the real one."""
        promoted = _audio("REAL", _local_pending=False, _local_id="LOCAL-UUID")
        window = _Window([promoted])

        assert window.store_message_transcription(_JID, "LOCAL-UUID", _value()) == stored.SAVE_STORED
        assert _text_of(promoted) == _SECRET
        assert window.db.calls == [("set", _JID, "REAL")]

    def test_a_message_gone_from_the_chat_is_missing(self):
        window = _Window([_audio("OUTRO")])
        assert window.store_message_transcription(_JID, _ID, _value()) == stored.SAVE_MISSING
        assert window.db.calls == []

    def test_a_failed_write_keeps_the_copy_in_memory(self, caplog, inline_call_after):
        """The next write of the record takes the key to disk — see the rule."""
        record = _audio()
        window = _Window([record])
        window.db.fail = True
        with caplog.at_level(logging.DEBUG):
            assert window.store_message_transcription(_JID, _ID, _value()) == stored.SAVE_STORED
        assert _text_of(record) == _SECRET

    def test_a_failed_write_is_said_in_one_sentence(self, inline_call_after):
        window = _Window([_audio()])
        window.db.fail = True
        window.store_message_transcription(_JID, _ID, _value())
        assert window.spoken == ["transcription_store_failed"]
        assert window.error_sound.played == 1

    def test_a_row_filed_under_the_lid_gets_it(self, inline_call_after):
        """History loaded through _history_storage_jid() lives in the
        database under the @lid; a write keyed on the phone alone found no
        row and was only a log line."""
        window = _Window([_audio()])
        window._phone_to_lid = {_JID: _LID}
        window.db.rows = {(_LID, _ID)}

        window.store_message_transcription(_JID, _ID, _value())

        assert window.db.calls == [("set", _JID, _ID), ("set", _LID, _ID)]
        assert window.spoken == []

    def test_no_row_anywhere_writes_the_record_whole_through_the_rule(self, inline_call_after):
        record = _audio(status=4)
        window = _Window([record])
        window._phone_to_lid = {_JID: _LID}
        window.db.rows = set()

        window.store_message_transcription(_JID, _ID, _value())

        assert window.db.calls[-1] == ("insert", _JID, _ID)
        written = window.db.values[-1]
        assert _text_of(written) == _SECRET and written["status"] == 4
        assert written is not record, "a snapshot, never the dict still in use"
        assert window.spoken == []

    def test_and_if_that_fails_too_it_is_said(self, inline_call_after):
        window = _Window([_audio()])
        window.db.rows = set()
        window.db.fail_insert = True
        window.store_message_transcription(_JID, _ID, _value())
        assert window.spoken == ["transcription_store_failed"]

    def test_it_is_dated_after_what_the_copies_hold(self):
        """The flow dated it when the run started; the clock went back since."""
        record = _audio(**{TRANSCRIPTION_KEY: _value("velha", at=2_000_000_000.0)})
        window = _Window([record])
        window.store_message_transcription(_JID, _ID, _value("nova", at=1_000_000_000.0))
        assert _text_of(record) == "nova"
        assert record[TRANSCRIPTION_KEY]["at"] > 2_000_000_000.0
        assert window.db.values[-1]["at"] == record[TRANSCRIPTION_KEY]["at"]


class TestDeletingFromMainWindow:
    def test_memory_follows_the_database(self, inline_call_after):
        record = _audio(**{TRANSCRIPTION_KEY: _value()})
        window = _Window([record])
        answers = []

        window.delete_message_transcription(_JID, _ID, answers.append)

        assert answers == [True]
        assert window.db.calls == [("delete", _JID, _ID)]
        assert _text_of(record) is None
        assert record[TRANSCRIPTION_KEY]["deleted"] is True

    def test_a_failed_delete_leaves_it_and_says_so(self, inline_call_after):
        record = _audio(**{TRANSCRIPTION_KEY: _value()})
        window = _Window([record])
        window.db.fail = True
        answers = []

        window.delete_message_transcription(_JID, _ID, answers.append)

        assert answers == [False]
        assert _text_of(record) == _SECRET

    def test_the_tombstone_is_dated_after_the_text_whatever_the_clock(
            self, inline_call_after, monkeypatch):
        monkeypatch.setattr(main_module.time, "time", lambda: 1_000_000_000.0)
        record = _audio(**{TRANSCRIPTION_KEY: _value(at=2_000_000_000.0)})
        window = _Window([record])

        window.delete_message_transcription(_JID, _ID, lambda ok: None)

        assert record[TRANSCRIPTION_KEY]["deleted"] is True
        assert record[TRANSCRIPTION_KEY]["at"] > 2_000_000_000.0
        assert window.db.values == [record[TRANSCRIPTION_KEY]["at"]]

    def test_every_jid_the_chat_is_stored_under_gets_the_tombstone(self, inline_call_after):
        window = _Window([_audio(**{TRANSCRIPTION_KEY: _value()})])
        window._phone_to_lid = {_JID: _LID}
        answers = []
        window.delete_message_transcription(_JID, _ID, answers.append)
        assert window.db.calls == [("delete", _JID, _ID), ("delete", _LID, _ID)]
        assert answers == [True]

    def test_the_tombstone_reaches_the_panels_old_copy(self, inline_call_after):
        in_chat = _audio(**{TRANSCRIPTION_KEY: _value()})
        panel_copy = _audio(**{TRANSCRIPTION_KEY: _value()})
        window = _Window([in_chat])
        window.conversations_panel = _Panel({"remoteJid": _JID}, all_sorted=[panel_copy])

        window.delete_message_transcription(_JID, _ID, lambda ok: None)

        assert _text_of(panel_copy) is None


class TestOtherPathsThatHoldTheRecord:
    def test_a_message_deleted_for_everyone_loses_its_transcription(self):
        class _Stub:
            def __init__(self):
                self.db = _RecordingDb()
                self._msg_bg_executor = _Inline()

            def _schedule_set_chats(self):
                pass

            _apply_remote_revoke = MainWindow._apply_remote_revoke

        existing = _audio(**{TRANSCRIPTION_KEY: _value()})
        revoke = {"key": dict(existing["key"]), "messageType": "protocolMessage",
                  "message": {"protocolMessage": {"type": 3}}}

        assert _Stub()._apply_remote_revoke(existing, revoke, _JID) is True
        assert TRANSCRIPTION_KEY not in existing

    def test_the_media_request_to_wppconnect_never_carries_it(self, monkeypatch):
        """get_base64_from_media() posts the record itself as the body."""
        sent = []

        def _fake_post(url, json=None, **kwargs):
            sent.append(json)
            raise main_module.MediaExpiredError()

        monkeypatch.setattr(main_module, "api_post", _fake_post)

        class _Stub:
            _phone_to_lid = {}
            wpp_server = "http://127.0.0.1"
            wpp_port = 6300
            token = "t"
            _normalize_jid = staticmethod(MainWindow._normalize_jid)
            _serialize_msg_id = MainWindow._serialize_msg_id
            get_base64_from_media = MainWindow.get_base64_from_media

        record = _audio(**{TRANSCRIPTION_KEY: _value()})
        with pytest.raises(main_module.MediaExpiredError):
            _Stub().get_base64_from_media(record)
        assert sent and TRANSCRIPTION_KEY not in sent[0]
        assert "cofre" not in repr(sent[0])
        assert TRANSCRIPTION_KEY in record, "the caller's record itself is untouched"

    def test_pruning_leaves_it_alone(self):
        from core.utils import prune_message_record

        record = _audio(**{TRANSCRIPTION_KEY: _value()})
        prune_message_record(record)
        assert _text_of(record) == _SECRET


# =============================================================================
#  Privacy — the log
# =============================================================================

_NEW_CODE = (
    MainWindow._transcription_copies,
    MainWindow._transcription_storage_jids,
    MainWindow._say_transcription_not_stored,
    MainWindow.store_message_transcription,
    MainWindow.delete_message_transcription,
    ConversationsPanel._load_conversation_page_from_db,
    DatabaseManager._with_known_local_fields,
    DatabaseManager._needs_stored_row,
    DatabaseManager._apply_known_local_fields,
    DatabaseManager._carries_local_fields,
    DatabaseManager._stored_message,
    DatabaseManager.set_message_transcription,
    DatabaseManager.delete_message_transcription,
    DatabaseManager._rewrite_transcription,
)


def test_the_new_code_logs_nothing_private():
    offenders = _scan(stored.__file__)
    for function in _NEW_CODE:
        offenders += _scan_source(inspect.getsource(function), function.__qualname__)
    assert offenders == [], offenders


def test_the_scan_would_see_an_id_in_a_log_line():
    """The scan above passing proves nothing unless it can fail."""
    bad = 'def f(message_id):\n    log.info("x %s", message_id)\n'
    assert _scan_source(bad, "probe")


def test_the_sync_count_line_names_no_chat():
    source = inspect.getsource(MainWindow.sync_chat_messages)
    [line] = [ln for ln in source.splitlines() if "saved transcription(s)" in ln]
    assert "remote_jid" not in line


async def test_nothing_private_reaches_the_log_at_run_time(in_memory_db, caplog, inline_call_after):
    # INFO, the level setup_logging() gives the app: aiosqlite's own DEBUG
    # lines quote every SQL parameter and never reach log.log.
    caplog.set_level(logging.INFO)
    await in_memory_db.insert_message(_JID, _audio())
    await in_memory_db.set_message_transcription(_JID, _ID, _value())
    await in_memory_db.set_message_transcription(_JID, "NAO-EXISTE", _value())
    await in_memory_db.delete_message_transcription(_JID, _ID, 500.0)
    await in_memory_db.insert_messages_batch(_JID, [_audio(**{TRANSCRIPTION_KEY: _value(at=1.0)})])

    window = _Window([_audio(), _audio("P", _local_pending=True, _local_id="P")])
    window.store_message_transcription(_JID, _ID, _value())
    window.store_message_transcription(_JID, "P", _value())
    window.db.fail = True
    window.store_message_transcription(_JID, _ID, _value())
    window.delete_message_transcription(_JID, _ID, lambda ok: None)

    for record in caplog.records:
        text = record.getMessage()
        for private in ("cofre", _ID, "5511988887777", "NAO-EXISTE"):
            assert private not in text, text


async def test_the_merge_logs_neither_the_text_nor_the_id(in_memory_db, caplog):
    """merge_or_rename_chat() already names the two chats (its own line,
    from before); what it now carries over is counted, never named."""
    caplog.set_level(logging.INFO)
    await in_memory_db.insert_message(_JID, _audio())
    await in_memory_db.insert_message(_LID, _audio(jid=_LID, **{TRANSCRIPTION_KEY: _value(at=900.0)}))
    await in_memory_db.merge_or_rename_chat(_LID, _JID)
    lines = [r.getMessage() for r in caplog.records]
    assert any("kept local fields of 1 duplicate(s)" in line for line in lines), lines
    for line in lines:
        assert "cofre" not in line and _ID not in line, line
