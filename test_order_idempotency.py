"""Order idempotency: a rerun of the same session must not double a position.

Orders are placed before the cycle is logged, so a crash between the two leaves
fills the database never recorded. Every order carries a deterministic
client_order_id, and the broker rejects a repeat — which execute_trades must
record as `duplicate_suppressed`, distinct from a submission that never landed.
"""

from types import SimpleNamespace

import pytest

import broker
from models import ProposedTrade, ValidatedTrade


def trade(symbol="AAPL", side="buy", usd=500.0):
    return ProposedTrade(symbol=symbol, side=side, notional_usd=usd, reasoning="r")


class FakeClient:
    """Accepts each client_order_id once, as Alpaca does."""

    def __init__(self, error=None):
        self.seen: list[str] = []
        self.error = error

    def submit_order(self, order_data):
        if self.error:
            raise self.error
        key = order_data.client_order_id
        if key in self.seen:
            raise RuntimeError(f"client_order_id must be unique: {key}")
        self.seen.append(key)
        return SimpleNamespace(id=f"order-{len(self.seen)}", status="OrderStatus.ACCEPTED")


@pytest.fixture
def client(monkeypatch):
    holder = {"client": FakeClient()}
    monkeypatch.setattr(broker, "_trading_client", lambda mode: holder["client"])
    return holder


def test_key_is_deterministic_for_the_same_order():
    assert broker._client_order_id(trade(), "2026-10-14") == broker._client_order_id(trade(), "2026-10-14")
    assert broker._client_order_id(trade(), "2026-10-14") == "agt-2026-10-14-AAPL-buy-50000"


@pytest.mark.parametrize("other", [
    trade(symbol="MSFT"),
    trade(side="sell"),
    trade(usd=500.01),
])
def test_key_differs_when_the_order_differs(other):
    assert broker._client_order_id(other, "2026-10-14") != broker._client_order_id(trade(), "2026-10-14")


def test_key_differs_across_sessions():
    assert broker._client_order_id(trade(), "2026-10-14") != broker._client_order_id(trade(), "2026-10-15")


def test_share_class_punctuation_is_stripped_from_the_key():
    assert broker._client_order_id(trade(symbol="BRK.B"), "2026-10-14") == "agt-2026-10-14-BRKB-buy-50000"


def test_a_rerun_of_the_same_session_is_suppressed_not_doubled(client):
    first = broker.execute_trades([ValidatedTrade(trade(), True)], "paper", "2026-10-14")
    rerun = [ValidatedTrade(trade(), True)]
    second = broker.execute_trades(rerun, "paper", "2026-10-14")

    assert first[0]["order_id"] == "order-1"
    assert client["client"].seen == ["agt-2026-10-14-AAPL-buy-50000"]
    assert second[0]["status"].startswith("duplicate_suppressed:")
    assert second[0]["order_id"] is None
    assert rerun[0].submit_failed


def test_an_ordinary_submit_failure_is_not_labelled_a_duplicate(client):
    client["client"] = FakeClient(error=ConnectionError("timeout"))
    out = broker.execute_trades([ValidatedTrade(trade(), True)], "paper", "2026-10-14")
    assert out[0]["status"].startswith("submit_failed:")


def test_invalid_trades_are_never_submitted(client):
    out = broker.execute_trades([ValidatedTrade(trade(), False, "nope")], "paper", "2026-10-14")
    assert out == [] and client["client"].seen == []
