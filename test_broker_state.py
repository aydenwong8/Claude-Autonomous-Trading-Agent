"""Account state must fail closed: an unreadable or non-numeric snapshot stops
the cycle before Claude is asked, rather than reaching validation looking valid.

Validation's limits are all `<`/`>` comparisons, and every comparison against
NaN is False, so a NaN cash or equity figure would pass the cash buffer and the
15% cap instead of failing them. Unreadable open orders would read as "nothing
pending" and let cash already promised to unfilled buys be spent twice.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import broker


def account(cash="10000", equity="20000"):
    return SimpleNamespace(cash=cash, equity=equity)


def position(symbol="AAPL", qty="10", market_value="2000", avg_entry_price="190"):
    return SimpleNamespace(
        symbol=symbol, qty=qty, market_value=market_value, avg_entry_price=avg_entry_price,
    )


def order(symbol="JNJ", side="OrderSide.BUY", notional="500", qty=None):
    return SimpleNamespace(
        symbol=symbol, side=side, notional=notional, qty=qty,
        status="OrderStatus.NEW",
        submitted_at=datetime(2026, 10, 14, 14, 0, tzinfo=timezone.utc),
    )


class FakeClient:
    def __init__(self, acct=None, positions=None, orders=None, orders_error=None):
        self.acct = acct or account()
        self.positions = positions if positions is not None else [position()]
        self.orders = orders if orders is not None else []
        self.orders_error = orders_error

    def get_account(self):
        return self.acct

    def get_all_positions(self):
        return self.positions

    def get_orders(self, _request):
        if self.orders_error:
            raise self.orders_error
        return self.orders


@pytest.fixture
def client(monkeypatch):
    holder = {}
    monkeypatch.setattr(broker, "_trading_client", lambda mode: holder["client"])
    return holder


def test_a_clean_snapshot_is_read(client):
    client["client"] = FakeClient(orders=[order()])
    state = broker.fetch_portfolio_state("paper")
    assert (state.cash, state.equity) == (10_000.0, 20_000.0)
    assert state.positions["AAPL"].market_value == 2_000.0
    assert state.pending_orders["JNJ"][0].notional_usd == 500.0


def test_unreadable_open_orders_stop_the_cycle(client):
    client["client"] = FakeClient(orders_error=ConnectionError("timeout"))
    with pytest.raises(RuntimeError, match="could not read open orders"):
        broker.fetch_portfolio_state("paper")


@pytest.mark.parametrize("field", ["cash", "equity"])
@pytest.mark.parametrize("bad", ["NaN", "inf", "-Infinity", None, "", "n/a"])
def test_non_numeric_account_figures_stop_the_cycle(client, field, bad):
    client["client"] = FakeClient(acct=account(**{field: bad}))
    with pytest.raises(ValueError, match=f"account {field}"):
        broker.fetch_portfolio_state("paper")


@pytest.mark.parametrize("field", ["qty", "market_value", "avg_entry_price"])
def test_non_numeric_position_figures_stop_the_cycle(client, field):
    client["client"] = FakeClient(positions=[position(**{field: "NaN"})])
    with pytest.raises(ValueError, match=f"AAPL {field}"):
        broker.fetch_portfolio_state("paper")


@pytest.mark.parametrize("field", ["notional", "qty"])
def test_non_numeric_pending_order_figures_stop_the_cycle(client, field):
    client["client"] = FakeClient(orders=[order(**{"notional": None, field: "NaN"})])
    with pytest.raises(ValueError, match="JNJ"):
        broker.fetch_portfolio_state("paper")


def test_an_order_sized_only_in_shares_is_still_accepted(client):
    # A missing notional is a real broker shape, not bad data: it is valued
    # later against prices, and refused there if it cannot be.
    client["client"] = FakeClient(orders=[order(notional=None, qty="3")])
    pending = broker.fetch_portfolio_state("paper").pending_orders["JNJ"][0]
    assert (pending.notional_usd, pending.qty) == (None, 3.0)
