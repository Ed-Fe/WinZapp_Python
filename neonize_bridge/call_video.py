"""The video of a session's calls: the client's camera to the other side and
the other side's video to the client, through video_codec.py and the DLL's
video functions (neonize_patches/neonize/0008, neonize.calls).

The client speaks to the bridge as to WPPConnect (core/websocket_client.py,
api_patches/src/util/callMediaBridge.ts):

- `call:video:camera` {jpeg (base64), epoch}: one camera frame, 10 a second,
  sent while the call is a video call and the camera is on;
- `call:video:camera:stop` {native}: stop sending; `native` when the USER
  turned the camera off, which tells WhatsApp too (camera state 0); without
  it (the call ended, the capture was discarded) only the frames stop;
- `call:video:camera:start`: the user turned the camera back on (state 1);
- `call:video:remote` {session, jpeg (base64)}: one frame of the other side.

An encoder per call starts with the first camera frame; meowcaller holds our
video back until the other side has acknowledged it, and refuses the access
units until then (dropped here). The other side's units are read on one
thread per call from the moment it is connected and decoded once the first
arrives. Everything of a call is closed when it ends (`end`).
"""
import base64
import logging
import threading

import video_codec

log = logging.getLogger("bridge")

VIDEO_REQUEST, VIDEO_ACCEPT, VIDEO_STOP, VIDEO_ENABLE, VIDEO_DISABLE = 1, 2, 3, 4, 5


def camera_jpeg(data):
    """The JPEG of a `call:video:camera`, or None when it is not one."""
    if not isinstance(data, dict):
        return None
    raw = data.get("jpeg")
    if isinstance(raw, str):
        try:
            raw = base64.b64decode(raw, validate=True)
        except (ValueError, TypeError):
            return None
    if not isinstance(raw, (bytes, bytearray)) or not raw.startswith(b"\xff\xd8"):
        return None
    return bytes(raw) if len(raw) <= video_codec.MAX_JPEG else None


class CallVideo:
    """`calls`: neonize.calls.Calls (or a fake); `codec`: what makes encoders
    and decoders (video_codec.FfmpegCodec); `emit(event, data)` to the
    session's clients."""

    def __init__(self, name, calls, emit, codec):
        self.name, self.calls, self.emit, self.codec = name, calls, emit, codec
        self._lock = threading.Lock()
        self._encoders = {}
        self._decoders = {}
        self._receiving = set()

    # ------------------------------------------------------------- outgoing
    def camera_frame(self, call_id, data):
        jpeg = camera_jpeg(data)
        if not call_id or jpeg is None:
            return
        with self._lock:
            encoder = self._encoders.get(call_id)
            if encoder is None:
                encoder = self._encoders[call_id] = self.codec.encoder(
                    lambda unit: self._send(call_id, unit))
                log.info("call video: camera encoder started")
        encoder.push(jpeg)

    def _send(self, call_id, unit):
        # False until the other side acknowledged our video: nothing to do
        self.calls.write_video(call_id, unit, video_codec.FRAME_MS)

    def camera_stop(self, call_id, native=False):
        with self._lock:
            encoder = self._encoders.pop(call_id, None)
        if encoder is not None:
            encoder.close()
        if native and call_id:
            self.calls.video(call_id, VIDEO_DISABLE)

    def camera_start(self, call_id):
        return bool(call_id) and self.calls.video(call_id, VIDEO_ENABLE)

    # ------------------------------------------------------------- incoming
    def start_receiving(self, call_id):
        """Reads the other side's video of the call until it ends (once)."""
        with self._lock:
            if not call_id or call_id in self._receiving:
                return
            self._receiving.add(call_id)
        threading.Thread(target=self._pump, args=(call_id,), daemon=True,
                         name="call-video").start()

    def _pump(self, call_id):
        try:
            while True:
                unit = self.calls.read_video(call_id, 500)
                if unit is None:
                    return
                if unit:
                    self._decoder(call_id).push(unit)
        except Exception:
            log.exception("call video: receiving")
        finally:
            with self._lock:
                self._receiving.discard(call_id)

    def _decoder(self, call_id):
        with self._lock:
            decoder = self._decoders.get(call_id)
            if decoder is None:
                decoder = self._decoders[call_id] = self.codec.decoder(self._show)
                log.info("call video: the other side's video started")
            return decoder

    def _show(self, jpeg):
        self.emit("call:video:remote", {"session": self.name,
                                        "jpeg": base64.b64encode(jpeg).decode("ascii")})

    # ----------------------------------------------------------------- end
    def end(self, call_id):
        with self._lock:
            pipes = [self._encoders.pop(call_id, None), self._decoders.pop(call_id, None)]
        for pipe in pipes:
            if pipe is not None:
                pipe.close()
