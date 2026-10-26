"""The error of the pairing-code request, in the format the client reads.

The client (websocket_client.on_wpp_phone_code_error) shows `name: message` and
decides whether WhatsApp refused because of the quota with `rateLimited` (or by
reading "rate-overlimit" in the text). The quota is per phone number and
outlives the process: repeating a request refused because of it is what keeps
it alive (docs/traps/pairing-flow.md). Pure functions, tested in test_pairing.py.
"""
import re

_QUOTA = re.compile(r"rate-?overlimit|\b429\b", re.IGNORECASE)
_LONG_NUMBERS = re.compile(r"\d{7,}")
MESSAGE_LIMIT = 200


def code_error(error) -> dict:
    """The `phoneCodeError` payload for the exception `error`. The text never
    carries a phone number: sequences of 7 or more digits become `<n>`."""
    name = type(error).__name__
    text = _LONG_NUMBERS.sub("<n>", str(error))
    return {
        "name": "LinkCodeError",
        "message": f"{name}: {text}"[:MESSAGE_LIMIT] if text else name,
        "rateLimited": bool(_QUOTA.search(f"{name} {text}")),
    }
