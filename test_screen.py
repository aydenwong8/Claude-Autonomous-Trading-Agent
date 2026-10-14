"""The screen's bar fetch: chunked, each chunk retried with backoff.

Runs on a fake fetcher — no network. The real failure this guards against is a
connection reset partway through one ~1,500-symbol paginated request, which
cost the whole screen on 2026-09-28, 10-07, 10-09 and 10-12.
"""

import config
from screen import fetch_daily_bars


def _bars_for(symbols):
    return {s: [f"{s}-bar"] for s in symbols}


def test_symbols_are_fetched_in_chunks_and_merged():
    calls = []

    def get(chunk):
        calls.append(list(chunk))
        return _bars_for(chunk)

    symbols = [f"S{i:04d}" for i in range(config.SCREEN_BAR_CHUNK * 2 + 7)]
    bars = fetch_daily_bars(symbols, get=get, sleep=lambda s: None)

    assert bars == _bars_for(symbols)
    assert len(calls) == 3
    assert all(len(c) <= config.SCREEN_BAR_CHUNK for c in calls)


def test_a_reset_costs_one_chunk_retry_not_the_screen():
    attempts = {}
    waits = []

    def get(chunk):
        key = chunk[0]
        attempts[key] = attempts.get(key, 0) + 1
        if key == "S0000" and attempts[key] == 1:
            raise ConnectionResetError(54, "Connection reset by peer")
        return _bars_for(chunk)

    symbols = [f"S{i:04d}" for i in range(config.SCREEN_BAR_CHUNK + 1)]
    bars = fetch_daily_bars(symbols, get=get, sleep=waits.append)

    assert bars == _bars_for(symbols)
    assert attempts == {"S0000": 2, symbols[config.SCREEN_BAR_CHUNK]: 1}
    assert waits == [config.SCREEN_RETRY_WAITS[0]]


def test_a_chunk_that_never_succeeds_fails_the_screen():
    waits = []

    def get(chunk):
        raise ConnectionResetError(54, "Connection reset by peer")

    assert fetch_daily_bars(["AAA", "BBB"], get=get, sleep=waits.append) is None
    # Every configured wait is used once, then it gives up.
    assert waits == list(config.SCREEN_RETRY_WAITS)
