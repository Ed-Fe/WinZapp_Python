"""core/transcription/waiting_runs.py: the inline transcriptions waiting for
the one under way. The flow side is in tests/test_transcription_without_dialog.py."""

from types import SimpleNamespace

from core.transcription import waiting_runs
from core.transcription.waiting_runs import MAX_WAITING, WaitingRuns


def _flow(message_id):
    flow = SimpleNamespace(id=message_id)
    flow.targets = lambda other: other.id == flow.id
    return flow


def test_oldest_first_with_positions():
    queue = WaitingRuns()
    a, b = _flow("A"), _flow("B")
    assert queue.add(a) == 1 and queue.add(b) == 2
    assert queue.next() is a and queue.next() is b and queue.next() is None


def test_withdraw_takes_out_the_same_message_only():
    queue = WaitingRuns()
    queue.add(_flow("A"))
    queue.add(_flow("B"))
    assert queue.withdraw(_flow("B")) is True
    assert queue.withdraw(_flow("Z")) is False
    assert len(queue) == 1 and queue.next().id == "A"


def test_bounded():
    queue = WaitingRuns()
    for n in range(MAX_WAITING):
        assert queue.add(_flow(str(n)))
    assert queue.add(_flow("one too many")) == 0
    assert len(queue) == MAX_WAITING


def test_one_queue_per_window_made_on_first_use():
    window = SimpleNamespace()
    queue = waiting_runs.of(window)
    assert waiting_runs.of(window) is queue is window._waiting_transcriptions
    queue.add(_flow("A"))
    queue.clear()
    assert len(queue) == 0
