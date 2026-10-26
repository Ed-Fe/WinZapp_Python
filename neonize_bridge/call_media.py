"""Calls carried by the bridge: WinZapp's neonize build places, answers and
hangs up calls through meowcaller (neonize/calls.py,
neonize_patches/neonize/0003, 0005 and 0008), and the audio crosses here.
Voice and video calls, one-to-one or to a group; the video itself is
call_video.py (`video`, None without ffmpeg or on an older build, when video
stays refused).

The client talks to the bridge as it talks to WPPConnect (main_window/calls.py,
core/websocket_client.py):

- routes `call/offer` (`isVideo`), `call/group/offer` {groupJid, isVideo},
  `call/upgrade-video`, `call/accept`, `call/end`, `call/ensure-ended`,
  `call/status`, `call/audio/enable` (calls.py wires them here when a session
  has calls);
- a voice call the other side turns into a video call is accepted at once,
  as WhatsApp Web does (this side's camera stays off until the user turns it
  on), and its `callstate` then says `isVideo`;
- `callstate` events for the call's life (`ACTIVE` makes the client attach its
  audio and say "connected"; ENDED, REJECTED, NOT_ANSWERED, HANDLED_REMOTELY
  and FAILED end it). The offer of an incoming call still comes from
  whatsmeow's CallOfferEv (calls.py), which carries more than meowcaller's;
- audio: the client sends `call:audio:mic` (base64 PCM s16le mono 48 kHz,
  20 ms) and plays `call:audio:remote` (PCM with its `sampleRate`). meowcaller
  works at 16 kHz in 60 ms frames: the microphone is brought down to 16 kHz
  and regrouped here; the other side goes out at 16 kHz and the client
  resamples it.

The pure functions are at the top (tested in test_call_media.py); `CallMedia`
holds a session's calls and the threads that pump events and audio.
"""
import array
import base64
import logging
import threading
import time

import msgmap
from call_video import VIDEO_ACCEPT, VIDEO_REQUEST

log = logging.getLogger("bridge")

CLIENT_RATE = 48000          # what the client's microphone frames are
CALL_RATE = 16000            # what meowcaller carries
FRAME_BYTES = 1920           # one meowcaller frame: 960 samples, 60 ms
_RATIO = CLIENT_RATE // CALL_RATE

STATE_OF_PHASE = {1: "CALLING", 2: "RINGING", 3: "CONNECTING", 4: "ACTIVE"}
LIVE_STATES = ("CALLING", "RINGING", "CONNECTING", "ACTIVE")


def downsample(pcm48: bytes, carry: bytes = b"") -> tuple:
    """48 kHz s16le mono -> 16 kHz, averaging each 3 samples (a box filter that
    also keeps most of what would alias). Returns (pcm16, carry): the bytes that
    did not make a whole group of 3 samples wait for the next call."""
    data = carry + pcm48
    whole = len(data) - len(data) % (2 * _RATIO)
    samples = array.array("h")
    samples.frombytes(data[:whole])
    out = array.array("h", (
        (samples[i] + samples[i + 1] + samples[i + 2]) // 3
        for i in range(0, len(samples), _RATIO)))
    return out.tobytes(), data[whole:]


class FrameAssembler:
    """Regroups any amount of 16 kHz PCM into meowcaller's 1920-byte frames."""

    def __init__(self):
        self._pending = b""
        self._carry = b""

    def push(self, pcm48: bytes) -> list:
        pcm16, self._carry = downsample(pcm48, self._carry)
        self._pending += pcm16
        frames = []
        while len(self._pending) >= FRAME_BYTES:
            frames.append(self._pending[:FRAME_BYTES])
            self._pending = self._pending[FRAME_BYTES:]
        return frames

    def clear(self):
        self._pending = self._carry = b""


def end_state(reason) -> tuple:
    """meowcaller's end reason -> (event, state) of `callstate`."""
    text = str(reason or "").lower()
    if text.startswith("server:") or text == "offer_failed":
        return "ended", "FAILED"
    if "timeout" in text:
        return "timeout", "NOT_ANSWERED"
    if "reject" in text:
        return "ended", "REJECTED"
    if "accept" in text:            # accepted_elsewhere: answered on another device
        return "ended", "HANDLED_REMOTELY"
    return "ended", "ENDED"


def dial_target(to: str, phone_of_lid=None) -> str:
    """What CallDial takes: the number's digits, or a LID address when no number
    is known for it. ValueError for what cannot be called (a group, a channel)."""
    jid = msgmap.jid_whatsmeow(str(to or "").strip())
    user, _, server = jid.partition("@")
    user = user.split(":")[0]
    if server == "lid" and phone_of_lid is not None:
        number = phone_of_lid(f"{user}@lid")
        if number:
            return number.split("@")[0].split(":")[0]
        return f"{user}@lid"
    if server in ("s.whatsapp.net", "") and user.isdigit():
        return user
    raise ValueError("only a person can be called")


def layer_error():
    """What neonize.calls raises once a client has no call layer (stopped)."""
    try:
        from neonize.calls import CallError
    except ImportError:             # a build without the layer never gets this far
        return LookupError
    return CallError


def state_event(call, *, state, event="state", now=None) -> dict:
    """The `data` of `callstate` for one of our calls (`call`: a dict with
    id, peer, outgoing, and video and group when it has them)."""
    group = call.get("group") or ""
    return {"id": call["id"], "peerJid": call["peer"], "state": state, "event": event,
            "outgoing": call["outgoing"], "isVideo": bool(call.get("video")),
            "isGroup": bool(group), "groupJid": group,
            "observedAt": int(now if now is not None else time.time())}


def group_target(group_jid) -> str:
    """The group's address as CallGroupDial takes it; ValueError when it is
    not a group."""
    jid = msgmap.jid_whatsmeow(str(group_jid or "").strip())
    user, _, server = jid.partition("@")
    if server != "g.us" or not user.replace("-", "").isdigit():
        raise ValueError("groupJid is not a group")
    return jid


class CallMedia:
    """The calls of one session. `calls` is a neonize.calls.Calls (or a fake);
    `emit(event, data)` sends to the session's clients."""

    def __init__(self, name, calls, emit, phone_of_lid=None, video=None, group_calls=False):
        self.name, self.calls, self.emit = name, calls, emit
        self.phone_of_lid = phone_of_lid
        self.video = video                # call_video.CallVideo, or None
        self.group_calls = group_calls
        self._lock = threading.Lock()
        self._calls = {}              # id -> {"id", "peer", "outgoing", "state"}
        self._mic = FrameAssembler()
        self._running = False

    # ---------------------------------------------------------------- state
    def _known(self, call_id):
        with self._lock:
            return dict(self._calls[call_id]) if call_id in self._calls else None

    def _set_state(self, call_id, state, event="state"):
        """Records the call's state and tells the client, once per change."""
        with self._lock:
            call = self._calls.get(call_id)
            if call is None or call["state"] == state:
                return False
            call["state"] = state
            data = state_event(call, state=state, event=event)
        self.emit("callstate", {"session": self.name, "data": data})
        return True

    def remember_offer(self, data):
        """calls.py hands over each incoming offer (whatsmeow's view of it: the
        peer by number), so this call's later states carry the same peer."""
        with self._lock:
            self._calls.setdefault(data["id"], {
                "id": data["id"], "peer": data.get("peerJid", ""), "outgoing": False,
                "state": "INCOMING_RING", "video": bool(data.get("isVideo")),
                "group": data.get("groupJid") or ""})

    def live_call(self):
        """The id of the call whose audio flows (the newest live one)."""
        with self._lock:
            live = [c for c in self._calls.values() if c["state"] in LIVE_STATES]
        return live[-1]["id"] if live else ""

    # ---------------------------------------------------------------- events
    def handle(self, ev):
        """One event of the call layer."""
        kind, call_id = ev.get("ev"), str(ev.get("call") or "")
        if not call_id:
            return
        if kind == "incoming":
            with self._lock:
                call = self._calls.setdefault(call_id, {
                    "id": call_id, "peer": msgmap.jid_wpp(str(ev.get("peer") or "")),
                    "outgoing": False, "state": "INCOMING_RING", "group": ""})
                call["video"] = bool(call.get("video") or ev.get("video"))
            return
        if kind == "video":
            self._peer_video(call_id, ev)
            return
        if kind == "state":
            state = STATE_OF_PHASE.get(int(ev.get("phase") or 0))
            call = self._known(call_id)
            # an outgoing call is only ACTIVE once the other person answered
            # ("accepted"); meowcaller's Active can come with the relay alone
            if state and not (state == "ACTIVE" and call and call["outgoing"]
                              and call["state"] != "ACTIVE"):
                self._set_state(call_id, state)
            return
        if kind in ("accepted", "ready"):
            if self._set_state(call_id, "ACTIVE"):
                self._start_audio(call_id)
            return
        if kind == "end":
            event, state = end_state(ev.get("reason"))
            self._set_state(call_id, state, event)
            with self._lock:
                self._calls.pop(call_id, None)
            if self.video is not None:
                self.video.end(call_id)
            if not self.live_call():
                self._mic.clear()

    def start_pump(self, keep_going):
        """Starts pump_events on its own thread, unless it is already going (a
        reconnection starts no second one). True if it started one."""
        with self._lock:
            if self._running:
                return False
            self._running = True
        threading.Thread(target=self.pump_events, args=(keep_going,), daemon=True,
                         name="call-events").start()
        return True

    def pump_events(self, keep_going):
        """The call layer's events, while `keep_going()` (start_pump runs it)."""
        stopped = layer_error()
        try:
            while keep_going():
                try:
                    ev = self.calls.next_event(500)
                except stopped:
                    return                  # the client stopped
                if ev:
                    try:
                        self.handle(ev)
                    except Exception:
                        log.exception("call event")
        finally:
            self._running = False

    # ---------------------------------------------------------------- audio
    def _start_audio(self, call_id):
        threading.Thread(target=self._pump_audio, args=(call_id,), daemon=True,
                         name="call-audio").start()
        if self.video is not None:
            self.video.start_receiving(call_id)

    def _pump_audio(self, call_id):
        """The other side's frames, to the client, until the call ends."""
        while True:
            frame = self.calls.read_audio(call_id, 200)
            if frame is None:
                return
            if frame:
                self.emit("call:audio:remote", {
                    "session": self.name, "encoding": "base64", "sampleRate": CALL_RATE,
                    "pcm": base64.b64encode(frame).decode("ascii")})

    def _connected_call(self):
        with self._lock:
            active = [c["id"] for c in self._calls.values() if c["state"] == "ACTIVE"]
        return active[-1] if active else ""

    def microphone(self, data):
        """A `call:audio:mic` from the client, to the connected call. The client
        opens its microphone when it dials; what it says before the answer is
        dropped, or meowcaller's 3 s queue would send it late."""
        call_id = self._connected_call()
        if not call_id or not isinstance(data, dict):
            return
        try:
            pcm = base64.b64decode(data.get("pcm") or "", validate=True)
        except (ValueError, TypeError):
            return
        if int(data.get("sampleRate") or CLIENT_RATE) != CLIENT_RATE:
            return                      # the client always sends 48 kHz
        for frame in self._mic.push(pcm):
            if not self.calls.write_audio(call_id, frame):
                break

    def stop_microphone(self):
        self._mic.clear()

    # ---------------------------------------------------------------- video
    def _mark_video(self, call_id):
        """The call became a video call: the client is told once."""
        with self._lock:
            call = self._calls.get(call_id)
            if call is None or call.get("video"):
                return False
            call["video"] = True
            data = state_event(call, state=call["state"] or "CALLING")
        self.emit("callstate", {"session": self.name, "data": data})
        return True

    def _peer_video(self, call_id, ev):
        """A <video> of the other side: an upgrade is accepted at once (the
        camera here stays off), and an active camera makes it a video call."""
        if ev.get("upgrade"):
            if self.video is None or not self.calls.video(call_id, VIDEO_ACCEPT):
                log.warning("call video: the other side's upgrade was not accepted")
                return
        if ev.get("upgrade") or ev.get("active"):
            self._mark_video(call_id)

    def upgrade_video(self, call_id=""):
        """Asks the other side to turn the connected call into a video call.
        Returns (ok, already_video); LookupError when no call is connected."""
        call_id = call_id or self._connected_call()
        call = self._known(call_id) if call_id else None
        if call is None or call["state"] != "ACTIVE":
            raise LookupError("there is no ongoing call to upgrade to video")
        if call.get("video"):
            return True, True
        if self.video is None or not self.calls.video(call_id, VIDEO_REQUEST):
            return False, False
        self._mark_video(call_id)
        return True, False

    def camera_frame(self, data):
        if self.video is not None:
            self.video.camera_frame(self._connected_call(), data)

    def camera_stop(self, data=None):
        if self.video is not None:
            native = isinstance(data, dict) and data.get("native") is True
            self.video.camera_stop(self._connected_call(), native)

    def camera_start(self):
        if self.video is not None:
            self.video.camera_start(self._connected_call())

    # ---------------------------------------------------------------- actions
    def dial(self, to, video=False):
        """Places a call; returns its id (CallError / ValueError on failure)."""
        if video and self.video is None:
            raise ValueError("video calls are not available here")
        target = dial_target(to, self.phone_of_lid)
        result = self.calls.dial(target, video=True) if video else self.calls.dial(target)
        peer = msgmap.jid_wpp(msgmap.jid_whatsmeow(to))
        return self._placed(str(result["call"]), peer, video, "")

    def group_dial(self, group_jid, video=False):
        """Calls every member of the group; returns the call's id."""
        if not self.group_calls or (video and self.video is None):
            raise ValueError("group calls are not available here")
        target = group_target(group_jid)
        result = self.calls.group_dial(target, video=video)
        group = msgmap.jid_wpp(target)
        return self._placed(str(result["call"]), group, video, group)

    def _placed(self, call_id, peer, video, group):
        with self._lock:
            self._calls[call_id] = {"id": call_id, "peer": peer, "outgoing": True, "state": "",
                                    "video": bool(video), "group": group}
        self._set_state(call_id, "CALLING")
        return call_id

    def answer(self, call_id):
        call_id = call_id or self._ringing()
        if not call_id or not self.calls.answer(call_id):
            return False
        if self._set_state(call_id, "ACTIVE"):
            self._start_audio(call_id)
        return True

    def _ringing(self):
        with self._lock:
            ringing = [c["id"] for c in self._calls.values() if c["state"] == "INCOMING_RING"]
        return ringing[-1] if ringing else ""

    def hangup(self, call_id=""):
        """Ends (or rejects while ringing) the call; with no id, the live one."""
        call_id = call_id or self.live_call() or self._ringing()
        return bool(call_id) and self.calls.hangup(call_id)

    def status(self, call_id):
        call = self._known(call_id)
        state = call["state"] if call else "ENDED"
        return {"id": call_id, "state": state, "live": state in LIVE_STATES,
                "isVideo": bool(call and call.get("video")), "engine": "meowcaller"}

    def knows(self, call_id):
        return self._known(call_id) is not None
