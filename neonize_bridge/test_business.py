"""Business routes: what the client reads from `commerce/account`."""
import sys
from pathlib import Path

import business

CLIENT = Path(__file__).resolve().parents[1] / "client"


def test_account_without_commerce_is_read_by_the_client_as_without_commerce_features():
    sys.path.insert(0, str(CLIENT))
    try:
        from core import commerce
    finally:
        sys.path.remove(str(CLIENT))
    account = commerce.parse_account(business.account_without_commerce())
    assert account.business is False
    assert account.cart_enabled is None
    assert account.sellers == frozenset()


def test_each_call_returns_a_new_body():
    business.account_without_commerce()["sellers"].append("x@c.us")
    assert business.account_without_commerce()["sellers"] == []
