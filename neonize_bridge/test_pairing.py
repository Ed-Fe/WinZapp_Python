"""The pairing code error in the format the client reads."""
import pytest

import pairing


def test_common_error_carries_name_and_message_and_is_not_quota():
    info = pairing.code_error(RuntimeError("connection reset"))
    assert info == {"name": "LinkCodeError", "message": "RuntimeError: connection reset",
                    "rateLimited": False}


@pytest.mark.parametrize("text", [
    "iq error: rate-overlimit", "IQErrorRateOverlimit", "server said 429", "RATE-OVERLIMIT"])
def test_refusal_by_quota_is_recognized(text):
    assert pairing.code_error(Exception(text))["rateLimited"] is True


def test_number_that_only_contains_429_is_not_quota():
    assert pairing.code_error(Exception("id 14290001"))["rateLimited"] is False


def test_phone_never_goes_in_the_message():
    info = pairing.code_error(Exception("invalid number 5511999998888 for user"))
    assert "5511999998888" not in info["message"] and "<n>" in info["message"]


def test_empty_message_keeps_only_the_name_and_the_size_has_a_limit():
    assert pairing.code_error(ValueError())["message"] == "ValueError"
    assert len(pairing.code_error(Exception("x" * 5000))["message"]) <= pairing.MESSAGE_LIMIT
