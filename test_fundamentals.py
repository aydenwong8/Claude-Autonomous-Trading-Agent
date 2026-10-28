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
    assert gd["valuation"]["pe_ttm_5y_range"] == "16.7 to 23.6"
    assert gd["growth_and_quality"]["operating_margin_5y_range_pct"] == "9.5 to 11.5"
    assert gd["earnings"]["next_report"] == "2026-10-22 before open"
    assert gd["earnings"]["days_until"] == 17
    assert gd["earnings"]["consensus_eps"] == 4.25
    assert gd["earnings"]["last_4_quarters"] == [
        "2026Q2: EPS 4.24 vs 4.09 est (+3.7%)",
        "2026Q1: EPS 4.1 vs 3.79 est (+8.2%)",
        "2025Q4: EPS 4.17 vs 4.22 est (-1.1%)",
        "2025Q3: EPS 3.88 vs 3.81 est (+1.8%)",
    ]
    results = gd["reported_results"]
    assert results["source"] == "10-Q, quarter ended 2026-04-05, filed 2026-04-29"
    assert results["revenue_usd_bn"] == 13.48
    assert results["same_quarter_last_year"]["revenue_usd_bn"] == 12.22
    # The SEC's facts service had not processed GD's Q2 10-Q on 2026-10-05.
    assert results["newer_filing_not_yet_in_sec_data"] == "10-Q filed 2026-07-29"
    assert "diluted_eps" in gd["not_available"]
    assert "lookup_failed" not in gd


def test_filings_are_material_only_one_line_each(tmp_path):
    filings = blocks(tmp_path)[0]["GD"]["material_filings_90d"]
    # Newest first; insider forms dropped; 9.01 (exhibits) dropped beside other items.
    assert filings[:4] == [
        "2026-08-07 8-K: 5.02 Officer or director change",
        "2026-08-04 8-K: 8.01 Other events",
        "2026-07-29 10-Q",
        "2026-07-29 8-K: 2.02 Results of operations",
    ]
    assert not any("Form 4" in x or x.split()[1] == "4" for x in filings)


def test_only_exhibits_keeps_the_item():
    submissions = {"filings": {"recent": {
        "form": ["8-K"], "filingDate": ["2026-10-01"], "items": ["9.01"],
        "acceptanceDateTime": ["2026-10-01T12:00:00.000Z"], "accessionNumber": ["x"],
    }}}
    assert f.material_filings(submissions, TODAY, NOW) == [
        "2026-10-01 8-K: 9.01 Financial statements and exhibits"]


def test_bank_line_items(tmp_path):
    jpm = blocks(tmp_path)[0]["JPM"]
    assert jpm["reported_results"]["revenue_usd_bn"] == 57.35  # RevenuesNetOfInterestExpense
    assert jpm["reported_results"]["diluted_eps"] == 7.7
    assert "operating_income_usd_bn" in jpm["not_available"]
    assert "operating_margin_5y_range_pct" in jpm["not_available"]
    assert jpm["material_filings_90d"]


# GD on 2026-10-28, as Finnhub returned it: Q3 dated 22 Oct (GD reports 30 Oct)
# with no result, so the next date shown was January's Q4 and its consensus.
GD_CALENDAR_LATE = {"earningsCalendar": [
    {"symbol": "GD", "date": "2027-01-26", "hour": "amc", "quarter": 4, "year": 2026,
     "epsEstimate": 4.6794, "epsActual": None, "revenueEstimate": 15156457158},
    {"symbol": "GD", "date": "2026-10-22", "hour": "bmo", "quarter": 3, "year": 2026,
     "epsEstimate": 4.2502, "epsActual": None, "revenueEstimate": 14142873020},
]}
GD_HISTORY_TO_Q2 = [
    {"symbol": "GD", "estimate": 4.0905, "actual": 4.24, "period": "2026-06-30",
     "surprisePercent": 3.6548, "year": 2026, "quarter": 2},
]


def test_a_passed_date_with_no_result_is_shown_as_not_yet_reported():
    out = f.earnings_block("GD", GD_CALENDAR_LATE, GD_HISTORY_TO_Q2, date(2026, 10, 28), [])
    assert out["next_report"] == (
        "2026Q3 not reported yet: the calendar dated it 2026-10-22 before open, "
        "which has passed, and gives no new date"
    )
    assert "days_until" not in out
    assert out["consensus_eps"] == 4.25  # Q3's, not January's Q4
    assert out["consensus_revenue_usd_bn"] == 14.14
    assert out["report_after_that"] == "2027-01-26 after close"


def test_a_passed_date_that_was_reported_is_not_flagged():
    iqv_calendar = {"earningsCalendar": [
        {"symbol": "IQV", "date": "2027-02-03", "hour": "amc", "quarter": 4, "year": 2026,
         "epsEstimate": 3.6817, "epsActual": None},
        {"symbol": "IQV", "date": "2026-10-27", "hour": "bmo", "quarter": 3, "year": 2026,
         "epsEstimate": 3.3079, "epsActual": 3.34},
    ]}
    out = f.earnings_block("IQV", iqv_calendar, [], date(2026, 10, 28), [])
    assert out["next_report"] == "2027-02-03 after close"
    assert out["days_until"] == 98
    assert "report_after_that" not in out
    # Finnhub's history can list the quarter before the calendar's actual is filled.
    gd_reported = [dict(GD_HISTORY_TO_Q2[0], quarter=3, period="2026-09-30")]
    out = f.earnings_block("GD", GD_CALENDAR_LATE, gd_reported, date(2026, 10, 28), [])
    assert out["next_report"] == "2027-01-26 after close"


def test_the_calendar_is_asked_for_recent_past_dates_too():
    get = FakeGet()
    limiter = f.RateLimiter(1000, 1.0, clock=lambda: 0.0, sleep=lambda s: None)
    f.FinnhubClient("KEY", limiter, get=get).calendar("GD", date(2026, 10, 28))
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(get.urls[0]).query))
    assert query["from"] == "2026-09-28"


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
    assert newest.startswith("2026-08-07 8-K")  # accepted 2026-08-07T21:11:06Z
    assert newest.endswith(" [new since your last cycle]") is expected


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


# --- review fixes: one bad payload must not blank the feed --------------------


@pytest.mark.parametrize("needle, payload", [
    ("/stock/earnings", {"error": "unexpected"}),          # history as a dict
    ("/calendar/earnings", {"earningsCalendar": {"x": 1}}),  # calendar rows as a dict
    ("/stock/metric", ["not", "a", "dict"]),                # metrics as a list
    ("/stock/metric", {"metric": {"peTTM": "n/a"}}),        # a number that isn't
])
def test_malformed_finnhub_payload_flags_only_that_name(tmp_path, needle, payload):
    class Odd(FakeGet):
        def __call__(self, url, headers, limiter):
            if needle in url and "symbol=GD" in url:
                self.urls.append(url)
                return payload
            return super().__call__(url, headers, limiter)

    out = blocks(tmp_path, symbols=("GD", "JPM"), get=Odd())[0]
    assert out["GD"]["lookup_failed"] == {"finnhub": "unexpected response shape"}
    assert out["GD"]["reported_results"]["revenue_usd_bn"] == 13.48  # EDGAR still shown
    assert "lookup_failed" not in out["JPM"]


def test_mismatched_sec_lists_flag_only_that_name(tmp_path):
    class Ragged(FakeGet):
        def __call__(self, url, headers, limiter):
            if "submissions" in url and "CIK0000040533" in url:
                self.urls.append(url)
                return {"filings": {"recent": {"form": ["8-K", "8-K"], "filingDate": ["2026-09-01"],
                        "accessionNumber": ["a"], "items": [""], "acceptanceDateTime": [None]}}}
            return super().__call__(url, headers, limiter)

    out = blocks(tmp_path, symbols=("GD", "JPM"), get=Ragged())[0]
    assert out["GD"]["lookup_failed"] == {"edgar": "unexpected response shape"}
    assert "lookup_failed" not in out["JPM"]


def test_truncated_body_is_a_lookup_failure():
    import http.client
    with pytest.raises(f.LookupFailed, match="IncompleteRead"):
        get_json([http.client.IncompleteRead(b""), http.client.IncompleteRead(b"")])


@pytest.mark.parametrize("header, waited", [
    ("Wed, 21 Oct 2026 07:28:00 GMT", 2.0),  # an HTTP-date: fall back to the default
    ("3600", 60.0),                          # capped, so one 429 can't stall the cycle
])
def test_retry_after_is_parsed_defensively(header, waited):
    sleeps = []
    result, _ = get_json([http_error(429, header), b"{}"], sleeps)
    assert result == {} and sleeps == [waited]


def test_a_10k_without_quarterly_figures_is_named():
    # Q3 from a 10-Q; then the 10-K, which carries only the full year.
    def fact(start, end, form, accn, val):
        return {"start": start, "end": end, "form": form, "accn": accn,
                "filed": "2026-02-10" if form == "10-K" else "2025-10-28", "val": val}
    facts = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [
        fact("2025-07-01", "2025-09-30", "10-Q", "q3", 10e9),
        fact("2025-01-01", "2025-12-31", "10-K", "k", 41e9),
    ]}}}}}
    missing = []
    out = f.reported_results_block(
        facts, missing, {"form": "10-K", "filed": "2026-02-10", "accession": "k"})
    assert out["source"] == "10-Q, quarter ended 2025-09-30, filed 2025-10-28"
    assert out["newer_filing_not_yet_in_sec_data"] == (
        "10-K filed 2026-02-10 (it reports the full year, with no separate quarter)")


def test_a_10q_whose_figures_are_missing_is_named_without_the_10k_wording():
    facts = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [
        {"start": "2026-01-01", "end": "2026-03-31", "form": "10-Q", "accn": "q1",
         "filed": "2026-04-28", "val": 1e9},
        # The newest quarter's figure, but carried under an older accession:
        {"start": "2026-04-01", "end": "2026-06-30", "form": "10-Q", "accn": "q1",
         "filed": "2026-07-28", "val": 2e9},
    ]}}, "DocumentType": {"units": {"pure": [{"accn": "q2b", "form": "10-Q"}]}}}}}
    out = f.reported_results_block(
        facts, [], {"form": "10-Q", "filed": "2026-07-28", "accession": "q2b"})
    assert out["newer_filing_not_yet_in_sec_data"] == (
        "10-Q filed 2026-07-28 (its quarterly figures are not in the SEC's data yet)")
