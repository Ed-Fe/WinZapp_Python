"""The refusals in the format the WinZapp client recognizes (responses.py)."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace as N

import responses

ROOT = Path(__file__).resolve().parent.parent


def _body(response):
    return json.loads(response.text)


def test_disconnected_is_the_404_of_the_real_servers_middleware():
    response = responses.disconnected()
    body = _body(response)
    # client/api_patches/src/middleware/statusConnection.ts: disconnected()
    assert response.status == 404
    assert body["status"] == "Disconnected" and body["response"] is None
    assert body["message"]


def test_disconnected_goes_through_the_clients_reader():
    """sending.py `_check_wa_connection_closed` and message_edit.response_not_sent
    read the 404 status and the `status` field (case-insensitively)."""
    response = responses.disconnected()
    body = _body(response)
    assert response.status == 404 and str(body.get("status", "")).lower() == "disconnected"
    assert body.get("reason") != "probe_timeout"     # it is not the "weak" 404


def test_without_connection_only_answers_when_there_is_no_connection():
    connected = N(client=object(), status="CONNECTED")
    assert responses.without_connection(connected) is None
    assert responses.without_connection(N(client=None, status="CONNECTED")).status == 404
    assert responses.without_connection(N(client=object(), status="CLOSED")).status == 404


def _client_reader():
    """The client's core/reply_failure.py, without importing the rest of it."""
    path = ROOT / "client" / "core" / "reply_failure.py"
    spec = importlib.util.spec_from_file_location("client_reply_failure", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_missing_quote_is_recognized_by_the_client_as_a_refusal_before_sending():
    response = responses.missing_quote()
    body = _body(response)
    fake = N(status_code=response.status, json=lambda: body)
    assert _client_reader().quote_missing_before_send(fake) is True
    assert response.status == 500        # never 4xx: old clients would drop the quote and resend


def test_edit_refused_carries_the_text_the_client_looks_for():
    response = responses.edit_refused("outside_window")
    assert "Cannot edit this message" in response.text      # message_actions.py
    assert 400 <= response.status < 500
