"""The Claude call and parsing of its response."""

from __future__ import annotations

import json

import config
from models import PositionReview, ProposedTrade
from prompt import SYSTEM_PROMPT


def _parse_trades_response(
    raw_text: str,
) -> tuple[list[ProposedTrade], list[PositionReview], str]:
    """Parse Claude's JSON response into ProposedTrade and PositionReview objects.

    Returns the trades, the per-position review verdicts, and the raw response
    text (for logging).
    """
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    # json.loads accepts the bare literals NaN/Infinity by default. A NaN
    # notional would pass every guardrail below — each one is a `<`/`>`
    # comparison, and every comparison against NaN is False — so refuse the
    # value at the door rather than relying on downstream checks to catch it.
    def _reject_non_finite(literal: str) -> float:
        raise ValueError(f"non-finite number in response: {literal}")

    payload = json.loads(text, parse_constant=_reject_non_finite)
    trades = [
        ProposedTrade(
            symbol=t["symbol"],
            side=t["side"],
            notional_usd=float(t["notional_usd"]),
            reasoning=t.get("reasoning", ""),
            thesis=t.get("thesis"),
            target=t.get("target"),
            invalidation=t.get("invalidation"),
            exit_trigger=t.get("exit_trigger"),
        )
        for t in payload.get("trades", [])
    ]
    reviews = [
        PositionReview(
            symbol=r["symbol"],
            verdict=r.get("verdict", ""),
            note=r.get("note", ""),
            thesis=r.get("thesis"),
            target=r.get("target"),
            invalidation=r.get("invalidation"),
        )
        for r in payload.get("reviews", [])
    ]
    return trades, reviews, raw_text


def get_claude_decision(
    prompt: str,
) -> tuple[list[ProposedTrade], list[PositionReview], str, tuple[int | None, int | None]]:
    """Call the Anthropic API (/v1/messages) and parse the structured JSON
    trade proposals and position reviews from the response.

    Returns the parsed trades and reviews plus the raw response text (for logging).
    """
    import anthropic

    client = anthropic.Anthropic()
    # Streamed, not messages.create: the SDK refuses a non-streaming call whose
    # max_tokens implies more than ten minutes of generation, and at
    # CLAUDE_MAX_TOKENS=32000 it raises before sending anything. We still want
    # the whole message at once, so collect the stream and take the final one.
    with client.messages.stream(
        model=config.CLAUDE_MODEL,
        max_tokens=config.CLAUDE_MAX_TOKENS,
        # Nested under output_config — `effort` is not a top-level parameter and
        # is silently ignored if passed as one.
        output_config={"effort": config.CLAUDE_EFFORT},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        response = stream.get_final_message()
    if response.stop_reason == "refusal":
        # A safety-classifier decline returns HTTP 200 with little or no text,
        # which would otherwise surface as an opaque JSON decode error. No
        # fallback model is configured on purpose: a decision made by a
        # different model would be recorded under config.CLAUDE_MODEL.
        details = getattr(response, "stop_details", None)
        raise RuntimeError(
            f"{config.CLAUDE_MODEL} declined the request (stop_reason=refusal, "
            f"category={getattr(details, 'category', None)}). No trades placed."
        )
    raw_text = "".join(block.text for block in response.content if block.type == "text")
    if response.stop_reason == "max_tokens":
        # Truncated JSON would surface as an opaque decode error; say what it is.
        raise RuntimeError(
            f"Claude response hit the {config.CLAUDE_MAX_TOKENS}-token cap and was "
            "truncated mid-JSON — raise config.CLAUDE_MAX_TOKENS, or lower "
            f"config.CLAUDE_EFFORT (currently {config.CLAUDE_EFFORT}; thinking "
            "tokens count against the same cap). No trades placed."
        )
    # Recorded so cost stops being an estimate. output_tokens includes thinking,
    # which is billed but never lands in raw_text.
    usage = (
        getattr(response.usage, "input_tokens", None),
        getattr(response.usage, "output_tokens", None),
    )
    trades, reviews, text = _parse_trades_response(raw_text)
    return trades, reviews, text, usage
