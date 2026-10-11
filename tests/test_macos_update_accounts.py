"""The other accounts and the update slot of a Sparkle update
(macos/winzapp_mac/update_accounts.py).

The decisions are plain functions that take the client collaborators as
arguments, so they are tested here on every platform with fakes; sparkle_mac
(Cocoa, tested on the Mac in macos/tests/test_sparkle_mac.py) passes the real
ones. Nothing here touches a process, a file or a window.
"""

import pathlib
import sys

import pytest

_MACOS = pathlib.Path(__file__).resolve().parents[1] / "macos"

pytestmark = pytest.mark.skipif(not (_MACOS / "winzapp_mac" / "update_accounts.py").is_file(),
                                reason="no macOS layer in this checkout")

if str(_MACOS) not in sys.path:
    sys.path.insert(0, str(_MACOS))

from winzapp_mac import update_accounts as A  # noqa: E402


class TestOtherAccounts:
    def test_this_account_a_corrupt_record_and_a_missing_id_are_left_out(self):
        leases = [{"account_id": "b"}, {"account_id": "me"}, {"account_id": "c", "_corrupt": True},
                  {}, {"account_id": ""}, {"account_id": "a"}, {"account_id": "b"}]
        assert A.other_accounts("me", leases) == ["a", "b"]

    def test_without_an_account_id_there_is_no_other_account(self):
        assert A.other_accounts(None, [{"account_id": "a"}]) == []

    def test_no_leases_no_accounts(self):
        assert A.other_accounts("me", []) == []


class TestQuitAccounts:
    def test_each_account_is_asked_in_order(self):
        asked = []
        A.quit_accounts(["a", "b"], asked.append)
        assert asked == ["a", "b"]

    def test_a_failure_does_not_stop_the_next_account(self):
        asked = []

        def request(account):
            asked.append(account)
            if account == "a":
                raise OSError("no answer")
        A.quit_accounts(["a", "b"], request)
        assert asked == ["a", "b"]

    def test_nobody_to_ask_asks_nobody(self):
        A.quit_accounts([], lambda account: pytest.fail("asked"))


class TestClaimSlot:
    def test_the_token_of_the_first_free_try(self):
        sleeps = []
        assert A.claim_slot(lambda: {"token": 1}, 5, 0.5, sleeps.append) == {"token": 1}
        assert sleeps == []

    def test_waits_for_the_accounts_that_are_still_quitting(self):
        answers = iter([None, None, "slot"])
        sleeps = []
        assert A.claim_slot(lambda: next(answers), 5, 0.5, sleeps.append) == "slot"
        assert sleeps == [0.5, 0.5]

    def test_an_account_that_never_quits_means_no_slot_after_the_attempts(self):
        calls, sleeps = [], []
        assert A.claim_slot(lambda: calls.append(1), 3, 0.5, sleeps.append) is None
        assert len(calls) == 3 and sleeps == [0.5] * 3

    def test_an_exception_is_a_refusal_at_once(self):
        calls = []

        def boom():
            calls.append(1)
            raise RuntimeError("lease file")
        assert A.claim_slot(boom, 5, 0, lambda wait: None) is None
        assert calls == [1]

    def test_no_attempts_no_slot(self):
        assert A.claim_slot(lambda: "slot", 0, 0, lambda wait: None) is None


class TestReleaseSlot:
    def test_the_token_goes_back(self):
        ended = []
        A.release_slot({"token": 1}, ended.append)
        assert ended == [{"token": 1}]

    def test_no_token_gives_nothing_back(self):
        A.release_slot(None, lambda t: pytest.fail("ended"))
        A.release_slot({}, lambda t: pytest.fail("ended"))

    def test_a_failure_to_end_it_does_not_raise(self):
        def boom(token):
            raise OSError("gone")
        A.release_slot("slot", boom)


class TestAccountsToReopen:
    def test_one_that_never_quit_is_still_running_and_is_not_started_twice(self):
        assert A.accounts_to_reopen(["a", "b", "c"], ["b"]) == ["a", "c"]

    def test_all_closed_all_reopened_in_order(self):
        assert A.accounts_to_reopen(["b", "a"], []) == ["b", "a"]

    def test_nothing_closed_nothing_reopened(self):
        assert A.accounts_to_reopen([], ["a"]) == []


class TestReopenAccounts:
    def test_each_is_started_and_the_user_is_told_once(self):
        spawned, told = [], []
        A.reopen_accounts(["a", "b"], spawned.append, lambda: told.append(1))
        assert spawned == ["a", "b"] and told == [1]

    def test_nothing_to_reopen_says_nothing(self):
        A.reopen_accounts([], lambda a: pytest.fail("spawned"), lambda: pytest.fail("told"))

    def test_a_failure_is_logged_and_does_not_raise(self):
        told = []

        def spawn(account):
            raise OSError("cannot start")
        A.reopen_accounts(["a"], spawn, lambda: told.append(1))
        assert told == []


class TestPopVerdict:
    def test_a_fresh_answer_is_used_once(self):
        verdicts = {"v1": (100.0, (True, "ok"))}
        assert A.pop_verdict(verdicts, "v1", 150.0, 600) == (True, "ok")
        assert A.pop_verdict(verdicts, "v1", 150.0, 600) is None

    def test_an_old_answer_is_dropped_and_asked_again(self):
        verdicts = {"v1": (100.0, (True, "ok"))}
        assert A.pop_verdict(verdicts, "v1", 100.0 + 601, 600) is None
        assert verdicts == {}

    def test_an_answer_exactly_at_the_limit_still_counts(self):
        assert A.pop_verdict({"v1": (100.0, (False, "x"))}, "v1", 700.0, 600) == (False, "x")

    def test_another_tags_answer_is_not_used(self):
        verdicts = {"v2": (100.0, (True, "ok"))}
        assert A.pop_verdict(verdicts, "v1", 100.0, 600) is None
        assert "v2" in verdicts
