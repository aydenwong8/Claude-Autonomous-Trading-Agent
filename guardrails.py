"""Code-enforced guardrails: every proposed trade is validated here."""

from __future__ import annotations

import math

import config
from models import PortfolioState, ProposedTrade, ValidatedTrade
from broker import _trading_client


# ---------------------------------------------------------------------------
# Stage 3: Validate
# ---------------------------------------------------------------------------

def validate_trades(
    trades: list[ProposedTrade], portfolio: PortfolioState, mode: str
) -> list[ValidatedTrade]:
    """Check every proposed trade against the code-enforced guardrails.

    Guardrails (all enforced here, see README.md#guardrails):
        - Max position size: 15% of portfolio per ticker
        - Universe: long-only, whitelisted US equities/ETFs
        - No leverage, margin, or options
        - Minimum cash buffer: 5%
        - Every symbol verified against the broker asset list before ordering

    Invalid trades are rejected, never silently adjusted.
    """
    results: list[ValidatedTrade] = []
    # Cash the account holds, less what orders already at the broker will take
    # out of it when they fill. Validating against raw cash would approve a buy
    # that breaches the buffer the moment a pending order fills — no single
    # decision looking wrong, the account overdrawn against its own floor
    # anyway. This is the same principle as refusing to credit an unsettled
    # sell: only money that is both present and unspoken-for can fund a buy.
    running_cash = portfolio.cash - portfolio.pending_buy_commitment
    # Two baselines, because the buy cap and the sell check need opposite
    # kinds of caution about the same pending orders.
    #
    # `position_values` is what is still disposable, and gates sells. A pending
    # buy must NOT count here: those shares have not arrived, and treating them
    # as sellable is how a long-only account ends up short. A pending sell IS
    # netted off, or the same shares could be sold twice — once by the order in
    # flight and again by this cycle's proposal.
    #
    # `cap_values` is where the book is heading, and gates the 15% cap. A
    # pending buy DOES count here, or a second buy could take a position past
    # the cap the moment both fill. Pending sells are not netted off there:
    # assuming a position is already smaller would loosen the cap on the
    # strength of an order that has not filled, and the cap should only ever
    # err toward strictness.
    position_values = {s: p.market_value for s, p in portfolio.positions.items()}
    cap_values = dict(position_values)
    for symbol, sold in portfolio.pending_sell_value.items():
        if symbol in position_values:
            position_values[symbol] = max(0.0, position_values[symbol] - sold)
    for symbol, orders in portfolio.pending_orders.items():
        for o in orders:
            if o.side == "buy" and o.notional_usd is not None:
                cap_values[symbol] = cap_values.get(symbol, 0.0) + o.notional_usd
    equity = portfolio.equity
    min_cash_required = config.MIN_CASH_BUFFER_PCT * equity

    trading_client = None  # lazily created only if a ticker actually needs checking

    for trade in trades:
        if trade.side not in ("buy", "sell"):
            results.append(ValidatedTrade(trade, False, f"unrecognized side '{trade.side}'"))
            continue

        # The whitelist bounds what we may *acquire*. Applying it to sells too
        # would strand any holding whose ticker later leaves the list (renames,
        # index changes — WATCHLIST is a hand-maintained snapshot), and with no
        # human override that capital could never be recovered. Exiting a
        # position is risk-reducing and stays permitted.
        if trade.side == "buy" and trade.symbol not in config.WATCHLIST:
            results.append(ValidatedTrade(trade, False, "symbol not in whitelisted universe"))
            continue

        # isfinite rejects NaN and ±Infinity. NaN in particular would satisfy
        # every guardrail below by making each comparison False.
        if not math.isfinite(trade.notional_usd) or trade.notional_usd <= 0:
            results.append(ValidatedTrade(trade, False, "notional must be a positive, finite number"))
            continue

        # Validate the amount that will actually be submitted — execute_trades
        # rounds to cents, and a value under a cent would round to a $0 order.
        if round(trade.notional_usd, 2) < config.MIN_TRADE_NOTIONAL_USD:
            results.append(ValidatedTrade(
                trade, False, f"notional below broker minimum (${config.MIN_TRADE_NOTIONAL_USD:.2f})"
            ))
            continue

        if trading_client is None:
            # Must match the execution environment: paper and live keys are not
            # valid against each other's endpoint, so a hardcoded "paper" here
            # would 401 on every symbol in live mode and reject every trade.
            trading_client = _trading_client(mode)
        try:
            asset = trading_client.get_asset(trade.symbol)
            if not asset.tradable:
                results.append(ValidatedTrade(trade, False, "symbol not tradable at broker"))
                continue
        except Exception:
            results.append(ValidatedTrade(trade, False, "symbol failed broker asset validation"))
            continue

        if trade.side == "buy":
            # A buy without a written case cannot be reviewed against later.
            missing = [
                f for f in ("thesis", "target", "invalidation")
                if not (getattr(trade, f) or "").strip()
            ]
            if missing:
                results.append(ValidatedTrade(
                    trade, False, f"buy missing required thesis field(s): {', '.join(missing)}"
                ))
                continue

            # Fail closed on a non-positive equity snapshot (API glitch, or a
            # mid-funding account): without a denominator the 15% cap cannot be
            # evaluated, and skipping it would silently disable the guardrail.
            if equity <= 0:
                results.append(ValidatedTrade(trade, False, "cannot size position against non-positive equity"))
                continue

            projected_position_value = cap_values.get(trade.symbol, 0.0) + trade.notional_usd
            if projected_position_value / equity > config.MAX_POSITION_PCT:
                results.append(ValidatedTrade(trade, False, "exceeds max position size (15%)"))
                continue
            if trade.notional_usd > running_cash:
                results.append(ValidatedTrade(trade, False, "insufficient cash (no margin/leverage)"))
                continue
            if running_cash - trade.notional_usd < min_cash_required:
                results.append(ValidatedTrade(trade, False, "would breach minimum cash buffer (5%)"))
                continue
            running_cash -= trade.notional_usd
            cap_values[trade.symbol] = projected_position_value
            # Bought shares are not sellable until they arrive, so this buy
            # does not raise what a later sell in this same list may dispose of.
            results.append(ValidatedTrade(trade, True))

        else:  # sell
            # A sell must name which trigger fired, so exits stay auditable.
            if trade.exit_trigger not in config.VALID_EXIT_TRIGGERS:
                results.append(ValidatedTrade(
                    trade, False,
                    f"sell has invalid or missing exit_trigger '{trade.exit_trigger}'",
                ))
                continue

            # An unfilled sell of unknowable size leaves no way to tell how much
            # of the holding is still disposable. Refuse rather than guess.
            if trade.symbol in portfolio.unvaluable_pending_sells:
                results.append(ValidatedTrade(
                    trade, False,
                    "unfilled sell of unknown size for this symbol; cannot size a further sell",
                ))
                continue

            held_value = position_values.get(trade.symbol, 0.0)
            if held_value <= 0:
                # Covers both "never held" and "the whole position is already
                # being sold by an order in flight".
                results.append(ValidatedTrade(trade, False, "no position held (long-only, no shorting)"))
                continue
            if trade.notional_usd > held_value:
                results.append(ValidatedTrade(
                    trade, False, "cannot sell more than held position (net of unfilled sells)"
                ))
                continue
            # Deliberately does NOT credit running_cash. The sell has not been
            # submitted yet, let alone filled or settled, so spending its
            # proceeds on a buy later in the same cycle would place an order
            # against money the account does not hold — which a margin account
            # funds as leverage rather than rejecting. It also made the outcome
            # depend on the order Claude happened to list the trades in.
            # Proceeds become spendable next cycle, from the real balance.
            position_values[trade.symbol] = held_value - trade.notional_usd
            results.append(ValidatedTrade(trade, True))

    return results
