"""Which calls the connection engine takes (core/call_logic.engine_refuses_call):
WPPConnect all of them; the Neonize bridge one-to-one voice calls, and only
when its /send-capabilities says it carries calls."""

import pytest

from core.call_logic import engine_refuses_call


@pytest.mark.parametrize("placing, is_video, is_group", [
    (True, False, False), (True, True, False), (True, False, True), (False, True, True),
])
def test_wppconnect_takes_every_call(placing, is_video, is_group):
    assert not engine_refuses_call(False, False, placing=placing,
                                   is_video=is_video, is_group=is_group)


def test_a_bridge_without_the_call_layer_takes_none():
    assert engine_refuses_call(True, False, placing=True)
    assert engine_refuses_call(True, False, placing=False)


def test_a_bridge_with_calls_places_one_to_one_voice_calls_only():
    assert not engine_refuses_call(True, True, placing=True)
    assert engine_refuses_call(True, True, placing=True, is_video=True)
    assert engine_refuses_call(True, True, placing=True, is_group=True)


def test_it_answers_any_incoming_call_with_voice():
    assert not engine_refuses_call(True, True, placing=False, is_video=True, is_group=True)
