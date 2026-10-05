"""Guardrail tests: at least one case per rule enforced by validate_trades.

Written against the unsplit agent.py and kept unchanged through the module
split, so they show the guardrails behave the same before and after it.
Every rejection reason is asserted verbatim: it is what the trades table
records, and what the agent is shown about its own rejected proposals.
"""

import math
from datetime import datetime, timezone

import pytest

import agent
import config

NOW = datetime(2026, 10, 2, 20, 30, tzinfo=timezone.utc)
OK = (True, None)


class FakeAsset:
    def __init__(self, tradable: bool):
        self.tradable = tradable


class FakeBroker:
    """Stands in for Alpaca's TradingClient; validation only calls get_asset."""

    def __init__(self):
        self.untradable: set[str] = set()
        self.unknown: set[str] = set()

    def get_asset(self, symbol):
        if symbol in self.unknown:
            raise RuntimeError(f"asset not found: {symbol}")
        return FakeAsset(symbol not in self.untradable)


@pytest.fixture
def broker(monkeypatch):
    # Patched in whichever module defines validate_trades, so this keeps
    # working unchanged after validate_trades moves out of agent.py.
    fake = FakeBroker()
    monkeypatch.setitem(
        agent.validate_trades.__globals__, "_trading_client", lambda mode: fake,
    )
    return fake


def portfolio(cash=20_000.0, equity=100_000.0, positions=None, **pending):
    held = {"AAPL": 10_000.0} if positions is None else positions
    return agent.PortfolioState(
        cash=cash,
        equity=equity,
        positions={
            s: agent.Position(symbol=s, qty=1.0, market_value=v, avg_entry_price=v)
            for s, v in held.items()
        },
        timestamp=NOW,
        **pending,
    )


def buy(symbol, usd, **case):
    fields = {"thesis": "t", "target": "g", "invalidation": "i"} | case
    return agent.ProposedTrade(
        symbol=symbol, side="buy", notional_usd=usd, reasoning="r", **fields,
    )


def sell(symbol, usd, trigger="risk"):
    return agent.ProposedTrade(
        symbol=symbol, side="sell", notional_usd=usd, reasoning="r", exit_trigger=trigger,
    )


def pending_buy(symbol, usd):
    return agent.PendingOrder(
        symbol=symbol, side="buy", notional_usd=usd, qty=None, status="new",
        submitted_at=None,
    )


def check(trades, state):
    return [
        (v.is_valid, v.rejection_reason)
        for v in agent.validate_trades(trades, state, "paper")
    ]


# --- 15% position cap ------------------------------------------------------


def test_buy_up_to_the_cap_passes_and_past_it_is_rejected(broker):
    assert check([buy("AAPL", 5_000)], portfolio()) == [OK]
    assert check([buy("AAPL", 6_000)], portfolio()) == [
        (False, "exceeds max position size (15%)")]


def test_cap_counts_unfilled_buys(broker):
    state = portfolio(pending_orders={"MSFT": [pending_buy("MSFT", 14_000)]})
    assert check([buy("MSFT", 2_000)], state) == [
        (False, "exceeds max position size (15%)")]


def test_two_buys_cannot_jointly_breach_the_cap(broker):
    assert check([buy("JNJ", 10_000), buy("JNJ", 6_000)], portfolio(cash=50_000)) == [
        OK, (False, "exceeds max position size (15%)")]


def test_buy_against_non_positive_equity_fails_closed(broker):
    assert check([buy("JNJ", 100)], portfolio(equity=0.0)) == [
        (False, "cannot size position against non-positive equity")]


def test_rejected_trades_are_recorded_unadjusted(broker):
    trade = buy("AAPL", 6_000)
    [result] = agent.validate_trades([trade], portfolio(), "paper")
    assert result.trade is trade and trade.notional_usd == 6_000


# --- cash and the 5% buffer --------------------------------------------------


def test_buy_larger_than_cash_is_rejected(broker):
    assert check([buy("JNJ", 4_000)], portfolio(cash=3_000)) == [
        (False, "insufficient cash (no margin/leverage)")]


def test_buy_that_breaches_the_buffer_is_rejected(broker):
    # 6,000 - 2,000 leaves 4,000, under the 5,000 floor (5% of 100,000).
    assert check([buy("JNJ", 2_000)], portfolio(cash=6_000)) == [
        (False, "would breach minimum cash buffer (5%)")]


def test_buffer_is_measured_net_of_unfilled_buys(broker):
    # 20,000 of cash, but 14,000 is already committed to an order in flight.
    state = portfolio(cash=20_000, pending_buy_commitment=14_000)
    assert check([buy("JNJ", 2_000)], state) == [
        (False, "would breach minimum cash buffer (5%)")]
    assert check([buy("JNJ", 1_000)], state) == [OK]


def test_sell_proceeds_are_not_spendable_in_the_same_cycle(broker):
    # Credited, the 5,000 sale would make the 3,000 buy affordable.
    assert check([sell("AAPL", 5_000), buy("JNJ", 3_000)], portfolio(cash=6_000)) == [
        OK, (False, "would breach minimum cash buffer (5%)")]


# --- trade count -------------------------------------------------------------


def test_at_most_five_trades_per_cycle(broker):
    assert config.MAX_TRADES_PER_CYCLE == 5
    results = check([sell("AAPL", 10)] * 6, portfolio())
    assert results[:5] == [OK] * 5
    assert results[5] == (False, "exceeds max trades per decision cycle")


# --- universe and ticker validation ------------------------------------------


def test_whitelist_gates_buys(broker):
    assert "ZZZZ" not in config.WATCHLIST
    assert check([buy("ZZZZ", 100)], portfolio()) == [
        (False, "symbol not in whitelisted universe")]


def test_held_name_outside_the_whitelist_stays_sellable(broker):
    assert check([sell("ZZZZ", 500)], portfolio(positions={"ZZZZ": 1_000.0})) == [OK]


def test_untradable_ticker_is_rejected(broker):
    broker.untradable.add("JNJ")
    assert check([buy("JNJ", 100)], portfolio()) == [
        (False, "symbol not tradable at broker")]


def test_ticker_the_broker_cannot_find_is_rejected(broker):
    broker.unknown.add("JNJ")
    assert check([buy("JNJ", 100)], portfolio()) == [
        (False, "symbol failed broker asset validation")]


# --- long-only ---------------------------------------------------------------


def test_cannot_sell_a_name_not_held(broker):
    assert check([sell("MSFT", 100)], portfolio()) == [
        (False, "no position held (long-only, no shorting)")]


def test_cannot_sell_more_than_held(broker):
    assert check([sell("AAPL", 10_001)], portfolio()) == [
        (False, "cannot sell more than held position (net of unfilled sells)")]


def test_sells_are_checked_cumulatively(broker):
    assert check([sell("AAPL", 6_000), sell("AAPL", 6_000)], portfolio()) == [
        OK, (False, "cannot sell more than held position (net of unfilled sells)")]


def test_shares_already_being_sold_cannot_be_sold_again(broker):
    state = portfolio(pending_sell_value={"AAPL": 8_000.0})
    assert check([sell("AAPL", 3_000)], state) == [
        (False, "cannot sell more than held position (net of unfilled sells)")]
    assert check([sell("AAPL", 2_000)], state) == [OK]


def test_unfilled_buys_are_not_sellable(broker):
    state = portfolio(pending_orders={"MSFT": [pending_buy("MSFT", 5_000)]})
    assert check([sell("MSFT", 100)], state) == [
        (False, "no position held (long-only, no shorting)")]


def test_unvaluable_unfilled_sell_blocks_further_sells(broker):
    state = portfolio(unvaluable_pending_sells=["AAPL"])
    assert check([sell("AAPL", 100)], state) == [
        (False, "unfilled sell of unknown size for this symbol; cannot size a further sell")]


# --- required fields ---------------------------------------------------------


@pytest.mark.parametrize("missing", ["thesis", "target", "invalidation"])
def test_buy_needs_the_full_case(broker, missing):
    assert check([buy("JNJ", 100, **{missing: None})], portfolio()) == [
        (False, f"buy missing required thesis field(s): {missing}")]


def test_blank_case_fields_count_as_missing(broker):
    assert check([buy("JNJ", 100, thesis="  ", target="")], portfolio()) == [
        (False, "buy missing required thesis field(s): thesis, target")]


@pytest.mark.parametrize("trigger", [None, "price_drift", "position_gone"])
def test_sell_needs_a_valid_exit_trigger(broker, trigger):
    assert check([sell("AAPL", 100, trigger)], portfolio()) == [
        (False, f"sell has invalid or missing exit_trigger '{trigger}'")]


def test_every_listed_exit_trigger_is_accepted(broker):
    trades = [sell("AAPL", 10, t) for t in sorted(config.VALID_EXIT_TRIGGERS)]
    assert check(trades, portfolio()) == [OK] * len(trades)


# --- sizes and sides ---------------------------------------------------------


@pytest.mark.parametrize("usd", [math.nan, math.inf, -math.inf, 0.0, -50.0])
def test_size_must_be_positive_and_finite(broker, usd):
    assert check([buy("JNJ", usd)], portfolio()) == [
        (False, "notional must be a positive, finite number")]


def test_size_below_the_broker_minimum_is_rejected(broker):
    assert check([buy("JNJ", 0.5)], portfolio()) == [
        (False, "notional below broker minimum ($1.00)")]


def test_unrecognised_side_is_rejected(broker):
    trade = agent.ProposedTrade(symbol="AAPL", side="short", notional_usd=100, reasoning="r")
    assert check([trade], portfolio()) == [(False, "unrecognized side 'short'")]


# --- live lock ---------------------------------------------------------------


def test_live_mode_is_locked_without_the_unlock_variable(monkeypatch):
    monkeypatch.delenv(config.LIVE_UNLOCK_ENV, raising=False)
    with pytest.raises(SystemExit):
        agent._trading_client("live")
