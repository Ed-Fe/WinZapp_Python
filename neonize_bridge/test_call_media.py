"""Voice calls through the call layer: the audio conversion, the states the
client reads and the call/* routes, against a fake neonize.calls.Calls; the
real bindings only where they need no network."""
import array
import asyncio
import base64
import threading
from types import SimpleNamespace as N

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import call_media as cm
import calls


def pcm(*samples):
    return array.array("h", samples).tobytes()


# ------------------------------------------------------------------ pure
def test_downsample_averages_each_3_samples_and_carries_the_rest():
    out, carry = cm.downsample(pcm(3, 6, 9, 30, 30, 30, 7))
    assert array.array("h", out).tolist() == [6, 30]
    assert carry == pcm(7)
    out, carry = cm.downsample(pcm(8, 9) + b"\x01", carry)     # half a sample too
    assert array.array("h", out).tolist() == [8]
    assert carry == b"\x01"


def test_the_assembler_makes_60_ms_frames_at_16_khz_from_20_ms_at_48_khz():
    assembler = cm.FrameAssembler()
    twenty_ms = pcm(*([300] * 960))                 # what the client sends
    assert assembler.push(twenty_ms) == []
    assert assembler.push(twenty_ms) == []
    frames = assembler.push(twenty_ms)
    assert len(frames) == 1 and len(frames[0]) == cm.FRAME_BYTES
    assert set(array.array("h", frames[0])) == {300}
    assembler.push(twenty_ms)
    assembler.clear()
    assert assembler.push(twenty_ms) == []


@pytest.mark.parametrize("reason, expected", [
    ("rejected", ("ended", "REJECTED")), ("timeout", ("timeout", "NOT_ANSWERED")),
    ("accepted_elsewhere", ("ended", "HANDLED_REMOTELY")), ("hangup", ("ended", "ENDED")),
    ("", ("ended", "ENDED")), ("server:503", ("ended", "FAILED")),
    ("offer_failed", ("ended", "FAILED")),
])
def test_end_reasons_become_the_clients_terminal_states(reason, expected):
    assert cm.end_state(reason) == expected


def test_dial_target():
    assert cm.dial_target("5511999990000@c.us") == "5511999990000"
    assert cm.dial_target("5511999990000:3@s.whatsapp.net") == "5511999990000"
    assert cm.dial_target("123@lid", lambda lid: "5511999990000@s.whatsapp.net") == "5511999990000"
    assert cm.dial_target("123@lid", lambda lid: "") == "123@lid"
    for unreachable in ("120363@g.us", "x@newsletter", "", "abc@c.us"):
        with pytest.raises(ValueError):
            cm.dial_target(unreachable)


# ------------------------------------------------------------- CallMedia
class FakeCalls:
    def __init__(self, remote=()):
        self.dialed, self.answered, self.hung, self.written = [], [], [], []
        self.remote = list(remote)          # frames the other side "sends"
        self.events = []
        self.enabled_flag = True

    def enabled(self):
        return self.enabled_flag

    def dial(self, target):
        self.dialed.append(target)
        return {"call": "OUT1", "peer": "123@lid"}

    def answer(self, call_id):
        self.answered.append(call_id)
        return True

    def hangup(self, call_id):
        self.hung.append(call_id)
        return True

    def read_audio(self, call_id, timeout_ms):
        return self.remote.pop(0) if self.remote else None

    def write_audio(self, call_id, frame):
        self.written.append((call_id, frame))
        return True

    def next_event(self, timeout_ms):
        if self.events:
            return self.events.pop(0)
        raise cm.layer_error()("stopped")


def media(fake=None):
    sent = []
    m = cm.CallMedia("s1", fake or FakeCalls(), lambda event, data: sent.append((event, data)))
    m._start_audio = lambda call_id: m._pump_audio(call_id)    # no thread in a test
    return m, sent


def states(sent):
    return [d["data"]["state"] for e, d in sent if e == "callstate"]


def test_an_outgoing_call_is_active_only_once_the_other_person_answers():
    fake = FakeCalls(remote=[b"\x01\x00" * 960, b"", None])
    m, sent = media(fake)
    assert m.dial("5511999990000@c.us") == "OUT1" and fake.dialed == ["5511999990000"]
    m.handle({"ev": "state", "call": "OUT1", "phase": 2})
    m.handle({"ev": "state", "call": "OUT1", "phase": 4})     # the relay, not the answer
    assert states(sent) == ["CALLING", "RINGING"]
    m.handle({"ev": "accepted", "call": "OUT1"})
    assert states(sent) == ["CALLING", "RINGING", "ACTIVE"]
    first = [d for e, d in sent if e == "callstate"][0]["data"]
    assert first["outgoing"] is True and first["peerJid"] == "5511999990000@c.us"
    audio = [d for e, d in sent if e == "call:audio:remote"]
    assert len(audio) == 1 and audio[0]["sampleRate"] == 16000
    assert base64.b64decode(audio[0]["pcm"]) == b"\x01\x00" * 960
    m.handle({"ev": "end", "call": "OUT1", "reason": "hangup"})
    end = [d for e, d in sent if e == "callstate"][-1]["data"]
    assert (end["state"], end["event"]) == ("ENDED", "ended")
    assert m.status("OUT1") == {"id": "OUT1", "state": "ENDED", "live": False,
                                "isVideo": False, "engine": "meowcaller"}


def test_the_microphone_reaches_only_a_connected_call():
    fake = FakeCalls(remote=[None])
    m, _ = media(fake)
    sixty_ms = {"pcm": base64.b64encode(pcm(*([5] * 2880))).decode(), "sampleRate": 48000}
    m.dial("5511999990000@c.us")
    m.microphone(sixty_ms)                       # still ringing: dropped
    assert fake.written == []
    m.handle({"ev": "accepted", "call": "OUT1"})
    m.microphone(sixty_ms)
    assert len(fake.written) == 1 and fake.written[0][0] == "OUT1"
    assert len(fake.written[0][1]) == cm.FRAME_BYTES
    m.microphone({"pcm": "not base64!", "sampleRate": 48000})
    m.microphone({"pcm": sixty_ms["pcm"], "sampleRate": 16000})     # not what the client sends
    assert len(fake.written) == 1


def test_an_incoming_call_answered_here_and_hung_up():
    fake = FakeCalls(remote=[None])
    m, sent = media(fake)
    m.remember_offer({"id": "IN1", "peerJid": "5511999990000@c.us"})
    m.handle({"ev": "incoming", "call": "IN1", "peer": "123@lid"})    # the offer's peer stays
    assert m.answer("") and fake.answered == ["IN1"]
    active = [d for e, d in sent if e == "callstate"][-1]["data"]
    assert (active["state"], active["peerJid"], active["outgoing"]) == (
        "ACTIVE", "5511999990000@c.us", False)
    assert m.status("IN1")["live"] is True
    assert m.hangup("") and fake.hung == ["IN1"]


def test_a_ringing_call_rejected_and_unknown_ids():
    m, sent = media()
    m.remember_offer({"id": "IN2", "peerJid": "5511@c.us"})
    assert m.knows("IN2") and not m.knows("nope")
    m.handle({"ev": "end", "call": "IN2", "reason": "rejected"})
    assert states(sent) == ["REJECTED"] and not m.knows("IN2")
    m.handle({"ev": "end", "call": "IN2", "reason": "rejected"})    # once only
    assert states(sent) == ["REJECTED"]
    m.handle({"ev": "state", "call": "", "phase": 4})              # no id: nothing
    assert m.answer("") is False


def test_one_event_pump_per_session(monkeypatch):
    fake = FakeCalls()
    fake.events = [{"ev": "incoming", "call": "IN3", "peer": "1@lid"}]
    m, _ = media(fake)
    started = []
    # only call_media's view of threading: the pump runs right here
    monkeypatch.setattr(cm, "threading", N(Lock=threading.Lock, Thread=lambda target, args, **kw: N(
        start=lambda: started.append(target(*args)))))
    assert m.start_pump(lambda: True) is True
    assert m.knows("IN3") and not m._running        # the fake stopped: the pump ended
    m._running = True
    assert m.start_pump(lambda: True) is False


# ------------------------------------------------------------- the routes
def run(s, requests):
    async def main():
        app = web.Application()
        calls.register_routes(app.router, session=lambda req: s,
                              ok=lambda **kw: web.json_response(kw))
        async with TestClient(TestServer(app)) as http:
            out = []
            for path, body in requests:
                response = await http.post("/api/tok" + path, json=body)
                out.append((response.status, await response.json()))
            return out
    return asyncio.run(main())


def test_the_routes_with_the_call_layer():
    fake = FakeCalls(remote=[None])
    m, _ = media(fake)
    s = N(client=object(), status="CONNECTED", call_media=m, ringing_calls=calls.Pending())
    (st, offer), (st2, status), (st3, end), (st4, video), (st5, group), (st6, bad) = run(s, [
        ("/call/offer", {"to": "5511999990000@c.us", "isVideo": False}),
        ("/call/status", {"callId": "OUT1"}),
        ("/call/end", {"callId": "OUT1"}),
        ("/call/offer", {"to": "5511999990000@c.us", "isVideo": True}),
        ("/call/group/offer", {"groupJid": "1@g.us"}),
        ("/call/offer", {"to": "120363@g.us"}),
    ])
    assert st == 200 and offer["response"]["id"] == "OUT1"
    assert st2 == 200 and status["response"]["live"] is True
    assert st3 == 200 and fake.hung == ["OUT1"]
    assert (st4, st5, st6) == (501, 501, 400)
    s.status = "CLOSED"
    (st7, _), = run(s, [("/call/accept", {"callId": "X"})])
    assert st7 == 400


def test_reject_goes_through_the_call_layer_when_it_holds_the_call():
    fake = FakeCalls()
    m, _ = media(fake)
    rejected = []
    s = N(client=N(reject_call=lambda who, call_id: rejected.append(call_id)),
          status="CONNECTED", call_media=m, ringing_calls=calls.Pending())
    s.ringing_calls.remember("IN4", "creator", 100)
    s.ringing_calls.remember("IN5", "creator", 100)
    m.remember_offer({"id": "IN4", "peerJid": "5511@c.us"})
    (status, _), = run(s, [("/call/reject", {})])
    assert status == 200 and fake.hung == ["IN4"] and rejected == ["IN5"]


# ------------------------------------------------------------- real bindings
def test_the_real_bindings_answer_without_a_connected_client():
    layer = pytest.importorskip("neonize.calls")
    if not layer.available():
        pytest.skip("neonize without WinZapp's call layer (build_neonize.py builds it)")
    calls_of_nobody = layer.Calls(b"no-such-client")
    assert calls_of_nobody.enabled() is False
    with pytest.raises(layer.CallError):
        calls_of_nobody.next_event(10)
    with pytest.raises(layer.CallError):
        calls_of_nobody.dial("5511999990000")
    assert calls_of_nobody.read_audio("x", 10) is None
    assert calls_of_nobody.write_audio("x", b"\0" * layer.FRAME_BYTES) is False
    with pytest.raises(ValueError):
        calls_of_nobody.write_audio("x", b"\0")


def test_a_call_answered_here_is_remembered_past_its_end_with_its_video():
    m, _sent = media()
    m.remember_offer({"id": "IN1", "peerJid": "5511999990000@c.us"})
    assert m.answered_here("IN1") is None
    assert m.answer("IN1")
    answered = m.answered_here("IN1")
    assert answered["video"] is False and answered["at"] > 0
    m._mark_video("IN1")                                    # upgraded to video mid-call
    m.handle({"ev": "end", "call": "IN1", "reason": ""})
    assert m.knows("IN1") is False
    assert m.answered_here("IN1")["video"] is True


def test_only_the_latest_answered_calls_are_kept():
    m, _sent = media()
    for n in range(cm.ANSWERED_KEPT + 5):
        m.remember_offer({"id": f"C{n}", "peerJid": "1@c.us"})
        m.answer(f"C{n}")
    assert m.answered_here("C0") is None and m.answered_here(f"C{cm.ANSWERED_KEPT + 4}")
