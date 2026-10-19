"""A video message (type "ptv") is a video, not an incompatible message (#536)."""
from client.core.websocket_client import WebSocketClient

PEER = "5511999999999@s.whatsapp.net"


class _Normalizer:
    _normalize_wpp_message = WebSocketClient._normalize_wpp_message
    _clean_jid = WebSocketClient._clean_jid


def _raw_ptv():
    return {
        "id": f"false_{PEER}_3EB0ABCDEF",
        "type": "ptv", "t": 1790222611, "timestamp": 1790222611,
        "from": PEER, "to": "5511888888888@s.whatsapp.net", "fromMe": False,
        "duration": 7, "mimetype": "video/mp4", "caption": "",
    }


def test_ptv_is_normalized_as_a_video_message():
    result = _Normalizer()._normalize_wpp_message(_raw_ptv())
    assert result["messageType"] == "videoMessage"
    assert "videoMessage" in result["message"]
