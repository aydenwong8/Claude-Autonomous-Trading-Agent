"""fundamentals.py against recorded Finnhub and EDGAR responses. No network.

Fixtures in tests/data/fundamentals/ are trimmed real responses recorded on
2026-10-05 for GD (an ordinary company), JPM (a bank: different line items),
BRK.B (a share class: BRK-B at the SEC, BRK.A at Finnhub's calendar) and SPY
(a fund). Re-record with tests/data/fundamentals/record.py.
"""

import io
import json
import urllib.error
import urllib.parse
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

import fundamentals as f

FIXTURES = Path(__file__).parent / "tests" / "data" / "fundamentals"
TODAY = date(2026, 10, 5)
NOW = datetime(2026, 10, 5, 20, 30, tzinfo=timezone.utc)
LAST_CYCLE = datetime(2026, 10, 2, 20, 30, tzinfo=timezone.utc)
CIKS = {40533: "GD", 19617: "JPM", 1067983: "BRK_B", 884394: "SPY"}


def load(name):
    data = json.loads((FIXTURES / name).read_text())
    if isinstance(data, dict) and "__http_error__" in data:
        raise f.LookupFailed(f"not found (HTTP {data['__http_error__']})")
    return data


class FakeGet:
    """Serves fixtures by URL, and records every URL asked for."""

    def __init__(self, fail=None):
        self.urls = []
        self.fail = fail or {}  # substring of URL -> LookupFailed message

    def __call__(self, url, headers, limiter):
        self.urls.append(url)
        for needle, message in self.fail.items():
            if needle in url:
                raise f.LookupFailed(message)
        if "company_tickers" in url:
            return load("sec_company_tickers.json")
        for cik, slug in CIKS.items():
            if f"CIK{cik:010d}" in url:
                kind = "submissions" if "submissions" in url else "companyfacts"
                return load(f"edgar_{kind}_{slug}.json")
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        slug = query["symbol"].replace(".", "_")
        kind = ("metric" if "/stock/metric" in url
                else "calendar" if "/calendar/" in url else "earnings")
        return load(f"finnhub_{kind}_{slug}.json")


def clients(tmp_path, get=None):
    get = get or FakeGet()
    limiter = f.RateLimiter(1000, 1.0, clock=lambda: 0.0, sleep=lambda s: None)
    return (
        f.FinnhubClient("KEY", limiter, get=get),
        f.EdgarClient("UA", limiter, tmp_path, TODAY, get=get),
        get,
    )


def blocks(tmp_path, symbols=("GD", "JPM", "BRK.B", "SPY"), get=None, last=LAST_CYCLE):
    finnhub, edgar, get = clients(tmp_path, get)
    return f.fetch_fundamentals(list(symbols), last, NOW, finnhub=finnhub, edgar=edgar), get


# --- small pieces ------------------------------------------------------------


def test_share_classes_use_a_hyphen_at_the_sec():
    assert f.edgar_symbol("BRK.B") == "BRK-B"
    assert f.edgar_symbol("GD") == "GD"


def test_rate_limiter_spaces_calls_evenly():
    now = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        now[0] += s

    limiter = f.RateLimiter(60, 60.0, clock=lambda: now[0], sleep=sleep)
    for _ in range(3):
        limiter.wait()
    assert slept == [1.0, 1.0]


def test_five_year_range_and_median():
    series = [{"period": f"20{20 + i // 4}-0{i % 4 + 1}-01", "v": float(i)} for i in range(24)]
    rng, median = f.five_year(series)
    # The newest 20 points only: values 4..23.
    assert rng == [4.0, 23.0] and median == 13.5


def test_five_year_needs_eight_points():
    series = [{"period": f"2026-0{i + 1}-01", "v": 1.0} for i in range(7)]
    assert f.five_year(series) is None


# --- http_get_json -----------------------------------------------------------


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, retry_after=None):
    headers = {"Retry-After": retry_after} if retry_after else {}
    return urllib.error.HTTPError("https://x/?token=SECRET", code, "err", headers, None)


def opener_from(outcomes):
    calls = []

    def opener(request, timeout):
        calls.append(request.full_url)
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(outcome)

    return opener, calls


def get_json(outcomes, sleeps=None):
    opener, calls = opener_from(outcomes)
    limiter = f.RateLimiter(1000, 1.0, clock=lambda: 0.0, sleep=lambda s: None)
    result = f.http_get_json(
        "https://x/?token=SECRET", {}, limiter, opener=opener,
        sleep=(sleeps.append if sleeps is not None else lambda s: None),
    )
    return result, calls


def test_http_success():
    assert get_json([b'{"a": 1}'])[0] == {"a": 1}


def test_http_retries_once_after_a_server_error():
    result, calls = get_json([http_error(500), b'{"ok": true}'])
    assert result == {"ok": True} and len(calls) == 2


def test_http_429_waits_for_retry_after():
    sleeps = []
    result, _ = get_json([http_error(429, "7"), b"[]"], sleeps)
    assert result == [] and sleeps == [7.0]


def test_http_404_is_final():
    opener, calls = opener_from([http_error(404)])
    with pytest.raises(f.LookupFailed, match="404"):
        f.http_get_json("https://x/", {}, f.RateLimiter(1000, 1.0), opener=opener)
    assert len(calls) == 1


def test_http_failure_reason_never_contains_the_url():
    with pytest.raises(f.LookupFailed) as e:
        get_json([http_error(500), http_error(503)])
    assert str(e.value) == "HTTP 503" and "SECRET" not in str(e.value)


def test_http_malformed_json_is_a_lookup_failure():
    with pytest.raises(f.LookupFailed, match="JSONDecodeError"):
        get_json([b"<html>", b"<html>"])


# --- whole blocks from recorded data -----------------------------------------


def test_gd_block(tmp_path):
    gd = blocks(tmp_path)[0]["GD"]
    assert gd["valuation"]["pe_ttm"] == 19.9
    assert gd["valuation"]["pe_ttm_5y_range"] == [16.7, 23.6]
    assert gd["growth_and_quality"]["operating_margin_5y_range_pct"] == [9.5, 11.5]
    assert gd["earnings"]["next_report"] == "2026-10-22 before open"
    assert gd["earnings"]["days_until"] == 17
    assert gd["earnings"]["consensus_eps"] == 4.25
    assert gd["earnings"]["last_4_quarters"][0] == {
        "quarter": "2026Q2", "eps_actual": 4.24, "eps_estimate": 4.09, "surprise_pct": 3.7,
    }
    results = gd["reported_results"]
    assert results["source"] == "10-Q, quarter ended 2026-04-05, filed 2026-04-29"
    assert results["revenue_usd_bn"] == 13.48
    assert results["same_quarter_last_year"]["revenue_usd_bn"] == 12.22
    # The SEC's facts service had not processed GD's Q2 10-Q on 2026-10-05.
    assert results["newer_filing_not_yet_in_sec_data"] == "10-Q filed 2026-07-29"
    assert "diluted_eps" in gd["not_available"]
    assert "lookup_failed" not in gd


def test_filings_are_material_only_and_items_are_translated(tmp_path):
    filings = blocks(tmp_path)[0]["GD"]["material_filings_90d"]
    assert filings and all(x["form"] in f.MATERIAL_FORMS for x in filings)
    results = [x for x in filings if "2.02" in x.get("items", "")]
    assert results[0]["items"].startswith("2.02 Results of operations")
    assert [x["date"] for x in filings] == sorted((x["date"] for x in filings), reverse=True)


def test_bank_line_items(tmp_path):
    jpm = blocks(tmp_path)[0]["JPM"]
    assert jpm["reported_results"]["revenue_usd_bn"] == 57.35  # RevenuesNetOfInterestExpense
    assert jpm["reported_results"]["diluted_eps"] == 7.7
    assert "operating_income_usd_bn" in jpm["not_available"]
    assert "operating_margin_5y_range_pct" in jpm["not_available"]
    assert jpm["material_filings_90d"]


def test_share_class_estimates_are_withheld(tmp_path):
    brk, get = blocks(tmp_path, symbols=("BRK.B",))
    brk = brk["BRK.B"]
    assert brk["earnings"]["next_report"] == "2026-10-30 time not given"
    assert "consensus_eps" not in brk["earnings"]
    assert "BRK.A" in brk["earnings"]["consensus_note"]
    assert "last_4_quarters" not in brk["earnings"]
    assert "last_4_quarters (reported under a different share class)" in brk["not_available"]
    assert brk["reported_results"]["revenue_usd_bn"] == 101.81
    assert any("CIK0001067983" in u for u in get.urls)


def test_funds_are_not_applicable_and_not_looked_up(tmp_path):
    out, get = blocks(tmp_path, symbols=("SPY",))
    assert out["SPY"] == {"not_applicable": "fund: no company financials or earnings"}
    assert get.urls == []


@pytest.mark.parametrize("last, expected", [
    (datetime(2026, 8, 7, 21, 0, tzinfo=timezone.utc), True),    # just before acceptance
    (datetime(2026, 8, 7, 21, 12, tzinfo=timezone.utc), False),  # just after
    (None, True),                                                # no previous cycle
])
def test_new_since_last_cycle_uses_the_acceptance_time(tmp_path, last, expected):
    gd = blocks(tmp_path, symbols=("GD",), last=last)[0]["GD"]
    newest = gd["material_filings_90d"][0]
    assert newest["date"] == "2026-08-07"  # accepted 2026-08-07T21:11:06Z
    assert newest["new_since_your_last_cycle"] is expected


def test_output_is_deterministic(tmp_path):
    a = json.dumps(blocks(tmp_path / "a")[0])
    b = json.dumps(blocks(tmp_path / "b")[0])
    assert a == b


# --- failures ----------------------------------------------------------------


def test_one_finnhub_call_failing_keeps_the_rest(tmp_path):
    get = FakeGet(fail={"/stock/metric": "HTTP 500"})
    gd = blocks(tmp_path, symbols=("GD",), get=get)[0]["GD"]
    assert gd["lookup_failed"] == {"finnhub": "metrics: HTTP 500"}
    assert "valuation" not in gd
    assert gd["earnings"]["next_report"] == "2026-10-22 before open"
    assert gd["reported_results"]["revenue_usd_bn"] == 13.48


def test_edgar_failing_keeps_finnhub(tmp_path):
    get = FakeGet(fail={"data.sec.gov": "URLError"})
    gd = blocks(tmp_path, symbols=("GD",), get=get)[0]["GD"]
    assert gd["lookup_failed"] == {"edgar": "URLError"}
    assert gd["valuation"]["pe_ttm"] == 19.9


def test_symbol_unknown_to_the_sec(tmp_path):
    out = blocks(tmp_path, symbols=("ZZZZ",), get=FakeGet(fail={"symbol=ZZZZ": "not found (HTTP 404)"}))[0]
    assert out["ZZZZ"]["lookup_failed"]["edgar"] == "no SEC company id for ZZZZ"


def test_unexpected_response_shape_is_a_lookup_failure(tmp_path):
    class Broken(FakeGet):
        def __call__(self, url, headers, limiter):
            if "submissions" in url:
                self.urls.append(url)
                return {"filings": {"recent": {"form": ["10-Q"]}}}  # no filingDate
            return super().__call__(url, headers, limiter)

    gd = blocks(tmp_path, symbols=("GD",), get=Broken())[0]["GD"]
    assert gd["lookup_failed"] == {"edgar": "unexpected response shape"}


def test_missing_credentials_flag_every_name_without_calling(monkeypatch, capsys):
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    out = f.fetch_fundamentals(["GD", "JPM"], LAST_CYCLE, NOW)
    for block in out.values():
        assert block == {"lookup_failed": {
            "finnhub": "FINNHUB_API_KEY is not set",
            "edgar": "SEC_USER_AGENT is not set",
        }}
    assert "FINNHUB_API_KEY is not set" in capsys.readouterr().out


# --- cache -------------------------------------------------------------------


def facts_fetches(get):
    return [u for u in get.urls if "companyfacts" in u]


def test_facts_are_refetched_while_the_latest_filing_is_missing(tmp_path):
    # GD's facts lack its newest 10-Q, so each cycle checks again.
    _, _, get = clients(tmp_path)
    for _ in range(2):
        f.fetch_fundamentals(["GD"], LAST_CYCLE, NOW, *clients(tmp_path, get)[:2])
    assert len(facts_fetches(get)) == 2


def test_complete_facts_are_reused(tmp_path):
    # JPM's facts include its newest 10-Q, so the second cycle reads the cache.
    get = FakeGet()
    for _ in range(2):
        f.fetch_fundamentals(["JPM"], LAST_CYCLE, NOW, *clients(tmp_path, get)[:2])
    assert len(facts_fetches(get)) == 1


def test_ticker_map_is_fetched_once_a_day(tmp_path):
    get = FakeGet()
    finnhub, edgar, _ = clients(tmp_path, get)
    f.fetch_fundamentals(["GD", "JPM"], LAST_CYCLE, NOW, finnhub=finnhub, edgar=edgar)
    f.fetch_fundamentals(["GD"], LAST_CYCLE, NOW, *clients(tmp_path, get)[:2])
    assert sum("company_tickers" in u for u in get.urls) == 1
    tomorrow = f.EdgarClient("UA", f.RateLimiter(1000, 1.0), tmp_path, date(2026, 10, 6), get=get)
    tomorrow.cik("GD")
    assert sum("company_tickers" in u for u in get.urls) == 2


def test_prune_removes_only_stale_cache_files(tmp_path):
    import os
    edgar = f.EdgarClient("UA", f.RateLimiter(1000, 1.0), tmp_path, TODAY, get=FakeGet())
    old, fresh, tickers = (tmp_path / "CIK0000000001.json", tmp_path / "CIK0000000002.json",
                           tmp_path / "company_tickers.json")
    for p in (old, fresh, tickers):
        p.write_text("{}")
    now = 2_000_000_000.0
    os.utime(old, (now - 31 * 86_400,) * 2)
    os.utime(fresh, (now - 29 * 86_400,) * 2)
    os.utime(tickers, (now - 99 * 86_400,) * 2)
    assert edgar.prune(now) == 1
    assert not old.exists() and fresh.exists() and tickers.exists()
