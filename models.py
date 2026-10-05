"""Data model shared across the pipeline: portfolio, market, theses, trades."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Position:
    """A single held position."""
    symbol: str
    qty: float
    market_value: float
    avg_entry_price: float


@dataclass
class PendingOrder:
    """An order accepted by the broker but not yet filled.

    The agent decides after the close, so its orders queue until the next
    session's open. Neither `cash` nor `positions` reflects them — the money
    has not left and the shares have not arrived — so without this a cycle
    that runs before the fill sees a portfolio that looks untouched and can
    place the same order again.
    """
    symbol: str
    side: str
    notional_usd: float | None
    qty: float | None
    status: str
    submitted_at: str | None


@dataclass
class PortfolioState:
    """Snapshot of account positions, cash, and P&L pulled from the broker."""
    cash: float
    equity: float
    positions: dict[str, Position]
    timestamp: datetime
    # Orders already at the broker and not yet filled, keyed by symbol. A
    # symbol can carry more than one.
    pending_orders: dict[str, list[PendingOrder]] = field(default_factory=dict)
    # Dollars that pending BUYS will consume from `cash` when they fill. Set
    # once per cycle by `price_pending_orders` and read by both the prompt and
    # validation, so the two can never disagree about what is spendable.
    # Pending sells contribute nothing: their proceeds are unsettled, the same
    # reason validation refuses to credit a sell within its own cycle.
    pending_buy_commitment: float = 0.0
    # Pending buys whose dollar value could not be established. Their cost is
    # NOT in the commitment above, so it understates what is spoken for.
    unpriced_pending_buys: list[str] = field(default_factory=list)
    # Dollars of each position already being sold by an unfilled order. That
    # much of the holding is spoken for and cannot be sold a second time.
    pending_sell_value: dict[str, float] = field(default_factory=dict)
    # Symbols with an unfilled sell of unknowable size. How much of the holding
    # remains disposable cannot be computed, so further sells are refused
    # outright rather than guessed at.
    unvaluable_pending_sells: list[str] = field(default_factory=list)


@dataclass
class NewsArticle:
    """One Alpaca news item, as archived in `news_articles`.

    `id` is Alpaca's own article id, which is what makes the archive
    idempotent: the same story fetched on ten consecutive days is one row.
    """
    id: int
    headline: str
    summary: str
    source: str
    url: str
    published_at: str   # UTC ISO
    symbols: list[str]


@dataclass
class MarketContext:
    """Fresh prices and news for current holdings + watchlist, injected into the prompt.

    `articles` is everything fetched this cycle, for the archive. Each held
    position gets its own lookup, so once archived its news is complete for the
    window — an empty result means "no recent news", not "we didn't look" —
    except for the symbols in `news_lookup_failed`, where it means exactly that.
    `market_news` is a broad sample across the watchlist — it cannot be complete
    at watchlist scale and must not be read as per-symbol coverage.
    """
    prices: dict[str, float]
    articles: list[NewsArticle]
    news_lookup_failed: set[str]
    market_news: list[NewsArticle]
    watchlist: list[str]
    # Per-symbol screen statistics (momentum, 52-week range position, realised
    # volatility, liquidity) for every watchlist name that had enough history.
    # Held positions are looked up here too, so a holding and a candidate can be
    # compared on identical axes — which is what makes a relative verdict possible.
    screen: dict[str, dict[str, Any]]
    # The shortlist actually shown to the model, drawn across four buckets.
    candidates: list[str]
    # True when the bar fetch behind `screen` failed. An empty shortlist then
    # means "not looked up", and the prompt has to say so: on cycle 48 a
    # connection reset produced `"candidates": []`, which the model read as a
    # screen that found nothing worth considering.
    screen_failed: bool = False


@dataclass
class Thesis:
    """The standing investment case for a held position.

    Written by Claude when it opens a position and carried forward into every
    later cycle's prompt, so the agent reasons against its own prior case
    instead of re-deriving an opinion from scratch each day.
    """
    symbol: str
    opened_at: str
    thesis: str          # why we own it
    target: str          # what would count as the case playing out
    invalidation: str    # what would prove the case wrong
    status: str = "open"  # "open" | "closed"
    closed_at: str | None = None
    exit_trigger: str | None = None
    # A case is allowed to be re-anchored while the position is still held —
    # a target that has been passed stops being a decision criterion and starts
    # being noise. `revision` is 1 at open and increments on every rewrite;
    # every version is kept in `thesis_revisions`.
    updated_at: str | None = None
    revision: int = 1


@dataclass
class PositionReview:
    """Claude's daily verdict on one held position. Not a trade — a judgement.

    Every open position must be reviewed each cycle, which is what makes a
    no-trade day an active decision rather than an absence of one.
    """
    symbol: str
    verdict: str  # "hold" | "trim" | "exit"
    note: str
    # Two uses, both persisted to `theses`: adopting a position that has no
    # stored case yet (opened before thesis tracking, or outside the agent),
    # and re-anchoring the case on a position that already has one. All three
    # must be supplied together — a case patched field-by-field is incoherent.
    thesis: str | None = None
    target: str | None = None
    invalidation: str | None = None


@dataclass
class ProposedTrade:
    """A single trade proposed by Claude, before validation."""
    symbol: str
    side: str  # "buy" or "sell"
    notional_usd: float
    reasoning: str
    # Buys must open a thesis; sells must name which exit trigger fired.
    thesis: str | None = None
    target: str | None = None
    invalidation: str | None = None
    exit_trigger: str | None = None


@dataclass
class ValidatedTrade:
    """A proposed trade after guardrail validation."""
    trade: ProposedTrade
    is_valid: bool
    rejection_reason: str | None = None
    order: dict[str, Any] | None = field(default=None)
    # Set when submission raised. The trade passed validation and has an order
    # record for the audit trail, but nothing reached the broker — so it must
    # not move the thesis lifecycle.
    submit_failed: bool = False


@dataclass(frozen=True)
class AgentVersion:
    """Which agent made a decision — recorded on every cycle.

    The instructions change often, and performance can only be attributed to a
    version if each decision says which version made it. `code_commit` covers
    everything in the repo (prompt builder, guardrails, config); the prompt
    hash lets cycles be grouped across commits that left the system prompt
    alone; model and effort are recorded because config.py can change them
    without anything else moving.
    """
    code_commit: str | None
    # Uncommitted edits to tracked .py files: the commit alone does not
    # describe the code that ran. dashboard.html is regenerated every run and
    # is not code, so it is deliberately not counted.
    code_dirty: bool | None
    system_prompt_sha: str
    model: str
    effort: str
