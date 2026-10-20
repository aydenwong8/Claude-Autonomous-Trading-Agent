"""Alpaca clients, account and pending-order state, and order execution."""

from __future__ import annotations

import math
import os
from typing import Any

import config
import markettime
from models import PendingOrder, PortfolioState, Position, ProposedTrade, ValidatedTrade


# ---------------------------------------------------------------------------
# Broker client helpers
# ---------------------------------------------------------------------------

def _with_timeout(client):
    """Give every request the client makes a timeout.

    alpaca-py calls `session.request` with no `timeout`, and requests waits
    forever by default, so a connection that dies mid-response hangs the cycle
    instead of failing it. A timeout raises, and the cycle fails closed like
    any other broker error. A timed-out order POST may still have landed, but
    a rerun is safe: client_order_id makes the repeat a no-op.
    """
    request = client._session.request

    def request_with_timeout(method, url, **kwargs):
        kwargs.setdefault("timeout", config.ALPACA_TIMEOUT)
        return request(method, url, **kwargs)

    client._session.request = request_with_timeout
    return client


def _trading_client(mode: str):
    from alpaca.trading.client import TradingClient

    # Enforced here rather than only in main(): this is the single chokepoint
    # every live order and account read must pass through, so no call path can
    # reach a real account without clearing the lock.
    config.assert_mode_allowed(mode)

    return _with_timeout(TradingClient(
        api_key=os.environ["ALPACA_API_KEY"],
        secret_key=os.environ["ALPACA_SECRET_KEY"],
        paper=(mode == "paper"),
    ))


def _data_client():
    from alpaca.data.historical.stock import StockHistoricalDataClient

    return _with_timeout(StockHistoricalDataClient(
        api_key=os.environ["ALPACA_API_KEY"],
        secret_key=os.environ["ALPACA_SECRET_KEY"],
    ))


def _news_client():
    from alpaca.data.historical.news import NewsClient

    return _with_timeout(NewsClient(
        api_key=os.environ["ALPACA_API_KEY"],
        secret_key=os.environ["ALPACA_SECRET_KEY"],
    ))


# ---------------------------------------------------------------------------
# Stage 1: Gather state
# ---------------------------------------------------------------------------

def _finite(value: Any, what: str) -> float:
    """A broker figure as a float, or ValueError if it is missing or not finite.

    Every guardrail is a `<`/`>` comparison, and every comparison against NaN
    is False, so a NaN cash or equity figure would pass the cash buffer and the
    15% cap rather than fail them. Refuse it here and the cycle stops before
    Claude is asked, instead of validating trades against a broken snapshot.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"broker returned a non-numeric {what}: {value!r}") from None
    if not math.isfinite(number):
        raise ValueError(f"broker returned a non-finite {what}: {value!r}")
    return number


def fetch_pending_orders(client) -> dict[str, list[PendingOrder]]:
    """Open (unfilled) orders at the broker, grouped by symbol.

    A failure here stops the cycle. An empty result would read as "nothing
    pending", and validation would then let cash already promised to unfilled
    buys be spent a second time — the overdraw `price_pending_orders` exists
    to prevent. Skipping a day is recoverable; breaching the buffer is not.
    """
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus

    pending: dict[str, list[PendingOrder]] = {}
    try:
        orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
    except Exception as exc:
        raise RuntimeError(
            f"could not read open orders ({type(exc).__name__}: {exc})"
        ) from exc

    for o in orders:
        pending.setdefault(o.symbol, []).append(
            PendingOrder(
                symbol=o.symbol,
                side=str(o.side).split(".")[-1].lower(),
                notional_usd=(_finite(o.notional, f"{o.symbol} pending order notional")
                              if o.notional is not None else None),
                qty=_finite(o.qty, f"{o.symbol} pending order qty") if o.qty is not None else None,
                status=str(o.status).split(".")[-1].lower(),
                submitted_at=o.submitted_at.isoformat() if o.submitted_at else None,
            )
        )
    return pending


def price_pending_orders(portfolio: PortfolioState, prices: dict[str, float]) -> None:
    """Value the unfilled orders against `prices` and record them on `portfolio`.

    Both sides of the book are double-countable while an order sits unfilled,
    because neither `cash` nor `positions` has moved yet:

    - A pending **buy** means cash alone overstates what can be spent, by
      exactly the size of the order. Spending it twice is how an account
      breaches its own cash buffer with no single decision looking wrong: cycle
      29 left a $2,900 buy in flight, and cycle 30 was shown $3,093 of
      "deployable" cash when barely $190 was genuinely free.
    - A pending **sell** means the position overstates what can still be sold.
      Selling the same shares twice would take a long-only account short.

    Needs prices because an order placed outside the agent may be sized in
    shares rather than dollars. The two unvaluable cases are handled
    differently, because caution points opposite ways: an unvaluable buy is
    merely named (the commitment becomes a floor, and the prompt says so),
    while an unvaluable sell blocks further sells of that symbol outright —
    there is no safe way to guess how much of a holding is still disposable.
    """
    def value_of(o: PendingOrder) -> float | None:
        if o.notional_usd is not None:
            return o.notional_usd
        if o.qty is not None and prices.get(o.symbol):
            return o.qty * prices[o.symbol]
        return None

    committed = 0.0
    unpriced: list[str] = []
    sell_value: dict[str, float] = {}
    unvaluable_sells: list[str] = []

    for symbol, orders in portfolio.pending_orders.items():
        for o in orders:
            value = value_of(o)
            if o.side == "buy":
                if value is None:
                    unpriced.append(symbol)
                else:
                    committed += value
            else:
                if value is None:
                    unvaluable_sells.append(symbol)
                else:
                    sell_value[symbol] = sell_value.get(symbol, 0.0) + value

    portfolio.pending_buy_commitment = committed
    portfolio.unpriced_pending_buys = unpriced
    portfolio.pending_sell_value = sell_value
    portfolio.unvaluable_pending_sells = unvaluable_sells


def fetch_portfolio_state(mode: str) -> PortfolioState:
    """Pull current positions, cash, and P&L from the Alpaca API."""
    client = _trading_client(mode)
    account = client.get_account()
    raw_positions = client.get_all_positions()

    positions = {
        p.symbol: Position(
            symbol=p.symbol,
            qty=_finite(p.qty, f"{p.symbol} qty"),
            market_value=_finite(p.market_value, f"{p.symbol} market_value"),
            avg_entry_price=_finite(p.avg_entry_price, f"{p.symbol} avg_entry_price"),
        )
        for p in raw_positions
    }

    return PortfolioState(
        cash=_finite(account.cash, "account cash"),
        equity=_finite(account.equity, "account equity"),
        positions=positions,
        timestamp=markettime.now_utc(),
        pending_orders=fetch_pending_orders(client),
    )


# ---------------------------------------------------------------------------
# Stage 4: Execute
# ---------------------------------------------------------------------------

def _client_order_id(trade: ProposedTrade, session_date: str) -> str:
    """A broker-side idempotency key: one identical order per symbol per session.

    Orders are placed before the cycle is logged, so a crash between the two
    leaves real fills the database never recorded — and rerunning the day would
    place them a second time. The broker rejects a duplicate client_order_id,
    which turns that second attempt into a no-op instead of a double position.

    Keyed on what the order *is* (session, symbol, side, amount in cents) rather
    than on its position in the list: a rerun re-asks Claude and gets a fresh
    decision, so an index would suppress whichever unrelated trade happened to
    land in the same slot while letting a genuine repeat through. Since the
    agent decides once per session, a second identical order the same day is a
    rerun artifact by construction.
    """
    symbol = "".join(ch for ch in trade.symbol.upper() if ch.isalnum())
    cents = int(round(trade.notional_usd * 100))
    return f"agt-{session_date}-{symbol}-{trade.side}-{cents}"


def execute_trades(
    trades: list[ValidatedTrade], mode: str, session_date: str
) -> list[dict[str, Any]]:
    """Place validated orders through the Alpaca trading API.

    Args:
        trades: Trades that passed validation (only is_valid=True trades are sent).
        mode: "paper" or "live" — selects which Alpaca base URL to use.
        session_date: Market-calendar date ('2026-09-03') scoping the
            idempotency key, so a rerun of the same session cannot double-order.

    Returns:
        Order confirmation records as returned by Alpaca.

    A failure on one order must not abort the loop. Earlier orders in the list
    are already live at the broker, and letting the exception propagate would
    skip logging entirely — leaving real fills with no row in `trades`, no
    thesis, and no decision_cycle. Each failure is recorded against its own
    trade and the cycle proceeds to the logging stage.
    """
    from alpaca.trading.requests import MarketOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce

    client = _trading_client(mode)
    orders: list[dict[str, Any]] = []

    for validated in trades:
        if not validated.is_valid:
            continue
        trade = validated.trade
        order_record: dict[str, Any] = {
            "symbol": trade.symbol,
            "side": trade.side,
            "notional_usd": trade.notional_usd,
            "order_id": None,
            "status": None,
        }
        try:
            order_request = MarketOrderRequest(
                symbol=trade.symbol,
                notional=round(trade.notional_usd, 2),
                side=OrderSide.BUY if trade.side == "buy" else OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                client_order_id=_client_order_id(trade, session_date),
            )
            order = client.submit_order(order_data=order_request)
            order_record["order_id"] = str(order.id)
            order_record["status"] = str(order.status)
        except Exception as exc:
            # Submission failed, so no fill exists for this trade — but the
            # attempt still belongs in the log. A thesis must not be opened off
            # an order that never reached the broker.
            #
            # A rejected duplicate key is called out separately because it means
            # the opposite of the other failures: the order is not missing, it
            # already exists from an earlier run of this same session, and the
            # guard did its job. It is still submit_failed for this run — this
            # invocation placed nothing, and the fill it is pointing at was
            # already logged (or lost) by the run that actually made it.
            duplicate = "client_order_id" in str(exc).lower() or "duplicate" in str(exc).lower()
            prefix = "duplicate_suppressed" if duplicate else "submit_failed"
            order_record["status"] = f"{prefix}: {type(exc).__name__}: {exc}"
            validated.order = order_record
            validated.submit_failed = True
            orders.append(order_record)
            continue

        validated.order = order_record
        orders.append(order_record)

    return orders
