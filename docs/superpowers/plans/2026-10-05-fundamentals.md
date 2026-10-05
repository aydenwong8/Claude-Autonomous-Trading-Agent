# Fundamentals feed (Finnhub + EDGAR) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Attach a facts-only `fundamentals` block (valuation with 5-year history, growth and quality, earnings dates with consensus, the last 4 quarters' EPS against estimate, the latest reported quarter, material SEC filings) to every holding and candidate in the decision prompt.

**Architecture:** A new `fundamentals.py` with stdlib-HTTP clients for Finnhub (rate-limited to 55 calls/min) and SEC EDGAR (≤5 req/s, with a disk cache keyed on the latest 10-Q/10-K and pruned after 30 days). It also contains pure parsing functions over the raw JSON, and `fetch_fundamentals()`, which never raises for data or network problems. `agent.fundamentals_for()` calls it for holdings ∪ candidates, catches any bug, and passes the result to `build_decision_prompt`. The system prompt gains one descriptive section.

**Tech Stack:** Python 3 stdlib (`urllib`, `json`, `statistics`), pytest, pyflakes. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-05-fundamentals-design.md`

## Global Constraints

- **Facts only:** no verdict words ("cheap", "beat", "miss") in data or prompt text. No guardrail, schema, or decision-rule change.
- **The cycle never fails because of fundamentals.** Every data or network problem becomes a per-name `lookup_failed`; a bug becomes `{"fundamentals": "internal error"}`.
- **Secrets:** `FINNHUB_API_KEY` and `SEC_USER_AGENT` are read from `.env` only. A `LookupFailed` message must never contain a URL, because the Finnhub key travels in the query string. **Never put the operator's email anywhere in code, tests or fixtures.**
- **Tests make no network calls.** Fakes are injected through function parameters, or patched in the defining module's globals (`fn.__globals__`), **never via `agent.X`**.
- **The golden prompt changes on purpose here,** and is re-captured once in Task 3. Every other existing test passes unchanged.
- Work on branch `feature/fundamentals` in a separate worktree. The main folder stays on `main` at **16:30 ET on weekdays**.
- Commit messages: imperative subject, a summary body in the repo's style, **no `Co-Authored-By` or "Generated with" trailers.**
- Use `venv/bin/python` from the repo root.

## Review Focus

- **Key leakage:** a Finnhub failure reason that reaches the prompt must not contain `token=`. Pinned by `test_http_failure_reason_never_contains_the_url` (Task 2). The reviewer should also grep the re-captured golden prompt for `token`.
- **Stale SEC data presented as current:** GD's Q2 10-Q was missing from the SEC's facts service on 2026-10-05. The block must name the newer filing (`newer_filing_not_yet_in_sec_data`), and the cache must not pin the stale copy. Pinned by `test_gd_block` and `test_facts_are_refetched_while_the_latest_filing_is_missing` (Task 2).
- **Share-class per-share figures:** Finnhub files BRK.B's estimates under BRK.A, ~1,500× a Class B share. They must be withheld, not shown. Pinned by `test_share_class_estimates_are_withheld` (Task 2).
- **Unbounded disk use:** each companyfacts file is ~3.6 MB and candidates rotate daily. Pinned by `test_prune_removes_only_stale_cache_files` (Task 2).
- **Cycle runtime and prompt size:** about 144 Finnhub calls add ~2.6 min, and the block adds ~250 tokens per name. Measured, not assumed, by the read-only dry run in Task 3, which must report both before merge.

---

### Task 1: Settings, ignore rules, and recorded fixtures

**Files:**
- Modify: `config.py` (append), `.gitignore`, `.env.example`
- Create: `tests/data/fundamentals/record.py`, `tests/data/fundamentals/*.json` (21 recorded responses)

**Interfaces:**
- Produces: `config.FINNHUB_CALLS_PER_MIN = 55`, `EDGAR_CALLS_PER_SEC = 5`, `EDGAR_CACHE_DIR = "cache/edgar"`, `EDGAR_CACHE_MAX_AGE_DAYS = 30`, `FILINGS_LOOKBACK_DAYS = 90`, `FUNDAMENTALS_HISTORY_QUARTERS = 20`, `FUNDAMENTALS_MIN_HISTORY_POINTS = 8`, `EARNINGS_LOOKAHEAD_DAYS = 120`. Also the fixture files named `finnhub_{metric,calendar,earnings}_{GD,JPM,BRK_B,SPY}.json`, `edgar_{submissions,companyfacts}_{GD,JPM,BRK_B,SPY}.json` and `sec_company_tickers.json`.

- [ ] **Step 1: Create the worktree and branch**

```bash
git worktree add -b feature/fundamentals ../Claude-Autonomous-Trading-Agent-fundamentals main
cd ../Claude-Autonomous-Trading-Agent-fundamentals
ln -s ../Claude-Autonomous-Trading-Agent/.env .env
ln -s ../Claude-Autonomous-Trading-Agent/venv venv
echo venv >> "$(git rev-parse --git-path info/exclude)"
mkdir -p tests/fixtures && cp ../Claude-Autonomous-Trading-Agent/tests/fixtures/* tests/fixtures/
venv/bin/python -m pytest -q
```

Expected: `92 passed`.

- [ ] **Step 2: Append the settings to `config.py`**

```python
# --- Fundamentals (fundamentals.py) -----------------------------------------
# Finnhub's free tier allows 60 calls/min; 55 leaves headroom for clock skew.
FINNHUB_CALLS_PER_MIN = 55
# The SEC asks for no more than 10 requests/second.
EDGAR_CALLS_PER_SEC = 5
EDGAR_CACHE_DIR = "cache/edgar"
# Cached companyfacts files unused this long are deleted (~3.6 MB each).
EDGAR_CACHE_MAX_AGE_DAYS = 30
FILINGS_LOOKBACK_DAYS = 90
# 20 quarters = the "5y" in pe_ttm_5y_range; fewer than 8 points shows no range.
FUNDAMENTALS_HISTORY_QUARTERS = 20
FUNDAMENTALS_MIN_HISTORY_POINTS = 8
EARNINGS_LOOKAHEAD_DAYS = 120
```

- [ ] **Step 3: Ignore the cache, and document the two new variables**

In `.gitignore`, add the line `cache/` after `tests/fixtures/`. Append to `.env.example`:

```
# Fundamentals (fundamentals.py). Finnhub free tier: https://finnhub.io
FINNHUB_API_KEY=
# The SEC requires contact details in every request's User-Agent:
# "<project name> <email you choose>". Without it, EDGAR data is flagged as a
# failed lookup for every name.
SEC_USER_AGENT=
```

- [ ] **Step 4: Add the recorder and the recorded fixtures**

Create `tests/data/fundamentals/record.py`:

```python
"""Re-record the fundamentals test fixtures from the live APIs.

Writes trimmed real Finnhub and SEC EDGAR responses for GD, JPM, BRK.B and SPY
into this directory. Uses FINNHUB_API_KEY and SEC_USER_AGENT from .env.

The tests pin values from the 2026-10-05 recording (e.g. GD reports in 17
days), so re-recording will change them: update test_fundamentals.py's
expected values and TODAY together with the fixtures.

    venv/bin/python tests/data/fundamentals/record.py
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
load_dotenv(ROOT / ".env")
KEY = os.environ["FINNHUB_API_KEY"].strip()
UA = os.environ["SEC_USER_AGENT"].strip()
SYMBOLS = {"GD": 40533, "JPM": 19617, "BRK.B": 1067983, "SPY": 884394}
CONCEPTS = [
    "Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenuesNetOfInterestExpense", "InterestAndDividendIncomeOperating",
    "OperatingIncomeLoss", "NetIncomeLoss", "ProfitLoss", "EarningsPerShareDiluted",
]
MATERIAL = {"8-K", "8-K/A", "10-Q", "10-K", "10-Q/A", "10-K/A", "10-12B", "10-12G", "S-4", "425"}


def get(url, headers=None):
    time.sleep(1.1)  # stays under both sources' rate limits
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"__http_error__": e.code}


def finnhub(path, **params):
    return get(f"https://finnhub.io/api/v1{path}?" + urllib.parse.urlencode({**params, "token": KEY}))


def save(name, obj):
    (HERE / name).write_text(json.dumps(obj, indent=1, sort_keys=True))


def main():
    today = date.today()
    tickers = get("https://www.sec.gov/files/company_tickers.json", {"User-Agent": UA})
    keep = {"GD", "JPM", "BRK-B", "SPY", "BF-B", "AAPL"}
    save("sec_company_tickers.json", {k: v for k, v in tickers.items() if v["ticker"] in keep})
    for symbol, cik in SYMBOLS.items():
        slug = symbol.replace(".", "_")
        metric = finnhub("/stock/metric", symbol=symbol, metric="all")
        if "series" in metric:
            q = metric["series"].get("quarterly", {})
            metric["series"] = {"quarterly": {k: q[k] for k in ("peTTM", "operatingMargin") if k in q}}
        save(f"finnhub_metric_{slug}.json", metric)
        save(f"finnhub_calendar_{slug}.json", finnhub(
            "/calendar/earnings", symbol=symbol,
            **{"from": today.isoformat(), "to": (today + timedelta(days=120)).isoformat()}))
        save(f"finnhub_earnings_{slug}.json", finnhub("/stock/earnings", symbol=symbol, limit=4))

        sub = get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", {"User-Agent": UA})
        if "filings" in sub:
            r = sub["filings"]["recent"]
            # Keep the head of the list plus every material form: JPM's recent
            # filings are mostly prospectuses, which would crowd them out.
            rows = [i for i, form in enumerate(r["form"]) if i < 20 or form in MATERIAL][:80]
            sub["filings"] = {"recent": {k: [r[k][i] for i in rows] for k in (
                "accessionNumber", "filingDate", "acceptanceDateTime", "form", "items",
                "primaryDocDescription")}}
            sub = {k: sub[k] for k in ("cik", "name", "entityType", "sicDescription", "tickers", "filings") if k in sub}
        save(f"edgar_submissions_{slug}.json", sub)

        facts = get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json", {"User-Agent": UA})
        if "facts" in facts:
            gaap = facts["facts"].get("us-gaap", {})
            facts = {"cik": facts.get("cik"), "entityName": facts.get("entityName"),
                     "facts": {"us-gaap": {c: gaap[c] for c in CONCEPTS if c in gaap}}}
        save(f"edgar_companyfacts_{slug}.json", facts)
        print(symbol, "recorded")


if __name__ == "__main__":
    main()
```

The tests in Task 2 pin values from the **2026-10-05** recording, e.g. `pe_ttm == 19.9`, which drifts daily. So copy those exact files rather than re-recording:

```bash
cp /private/tmp/claude-501/-Users-aydenwong-Projects-Claude-Autonomous-Trading-Agent/3af021fa-f032-44e8-b62d-fb209a74f305/scratchpad/full/tests/data/fundamentals/*.json tests/data/fundamentals/
ls tests/data/fundamentals/*.json | wc -l
```

Expected: `21`. If that scratch directory no longer exists, run `venv/bin/python tests/data/fundamentals/record.py`, then update `TODAY`, `NOW` and the pinned values in Task 2's tests to match the new recording. Record that as a ruling.

- [ ] **Step 5: Check that no personal data is in the fixtures**

Run: `grep -rlI "@" tests/data/fundamentals/*.json | wc -l; grep -c "token" tests/data/fundamentals/*.json | grep -v ":0" | wc -l`
Expected: `0` and `0`. Fixtures are public market data; the User-Agent and the key are never stored.

- [ ] **Step 6: Run the suite and commit**

Run: `venv/bin/python -m pytest -q`. Expected: `92 passed`, since nothing reads the new settings yet.

```bash
git add config.py .gitignore .env.example tests/data/fundamentals
git commit -F - <<'EOF'
Add fundamentals settings and recorded API fixtures

- config.py: call rates, the EDGAR cache location and age limit, the
  filings window, history length and earnings lookahead.
- .gitignore: cache/. .env.example: FINNHUB_API_KEY and SEC_USER_AGENT.
- tests/data/fundamentals: trimmed real Finnhub and EDGAR responses
  recorded 2026-10-05 for GD, JPM (bank line items), BRK.B (share
  class) and SPY (fund), plus the recorder used to make them.
EOF
```

---

### Task 2: `fundamentals.py`

**Files:**
- Create: `test_fundamentals.py`, then `fundamentals.py`

**Interfaces:**
- Consumes: Task 1's `config` constants and fixtures; `markettime.fmt_et_date(datetime) -> "YYYY-MM-DD"`.
- Produces: `fundamentals.fetch_fundamentals(symbols: list[str], last_cycle_at: datetime | None, now: datetime, finnhub: FinnhubClient | None = None, edgar: EdgarClient | None = None) -> dict[str, dict[str, Any]]`. Also `LookupFailed`, `RateLimiter(calls, per_seconds, clock=, sleep=)`, `http_get_json(url, headers, limiter, opener=, sleep=)`, `FinnhubClient(api_key, limiter, get=)`, `EdgarClient(user_agent, limiter, cache_dir, today, get=)` with `.cik()`, `.submissions()`, `.facts()`, `.prune(now_epoch)`, `edgar_symbol(symbol)`, `five_year(series, scale=1.0)`, `MATERIAL_FORMS` and `FUND_SYMBOLS`.

- [ ] **Step 1: Write the tests**

Create `test_fundamentals.py`:

```python
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
```

- [ ] **Step 2: Run them and watch them fail**

Run: `venv/bin/python -m pytest -q test_fundamentals.py`
Expected: an error collecting `test_fundamentals.py` with `ModuleNotFoundError: No module named 'fundamentals'`.

- [ ] **Step 3: Write the module**

Create `fundamentals.py`:

```python
"""Fundamentals for every name in the prompt, from Finnhub and SEC EDGAR.

The agent kept deferring decisions it could not make from prices and news alone:
it could not tell a broken stock from a mispriced one, did not know when a
holding reported, and could not read the results when they came. This module
supplies those facts and nothing else. It returns figures and dates, never
verdicts ("cheap", "beat"), so the judgement stays the model's.

- Finnhub (free tier): current valuation and quality metrics with their
  quarterly history, the next earnings date with consensus, and the last four
  quarters of EPS against estimate.
- SEC EDGAR (no key): the latest reported quarter against the same quarter a
  year earlier, and material filings (8-K, 10-Q, 10-K, spin-off registrations).

Nothing here raises for a data or network problem. A failed lookup is named in
the block as `lookup_failed`, because an empty block would read as "nothing to
report" when it means "not looked up".
"""

from __future__ import annotations

import json
import os
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import config
import markettime

FINNHUB_URL = "https://finnhub.io/api/v1"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SEC_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

# Funds have no company financials at either source. Named explicitly rather
# than inferred: the watchlist holds exactly these two, and a guess that
# misfires would hide a real company's data behind "not applicable".
FUND_SYMBOLS = frozenset({"SPY", "QQQ"})

# First concept with a value wins, per figure. Companies file under different
# names, and banks under different names again.
REVENUE_CONCEPTS = (
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenuesNetOfInterestExpense",
    "InterestAndDividendIncomeOperating",
)
OPERATING_INCOME_CONCEPTS = ("OperatingIncomeLoss",)
NET_INCOME_CONCEPTS = ("NetIncomeLoss", "ProfitLoss")
EPS_CONCEPTS = ("EarningsPerShareDiluted",)

REPORT_FORMS = frozenset({"10-Q", "10-K", "10-Q/A", "10-K/A"})
MATERIAL_FORMS = frozenset({"8-K", "8-K/A", "10-Q", "10-K", "10-12B", "10-12G", "S-4", "425"})
EIGHT_K_ITEMS = {
    "1.01": "Material definitive agreement",
    "1.02": "Termination of a material agreement",
    "1.03": "Bankruptcy or receivership",
    "2.01": "Completion of acquisition or disposition",
    "2.02": "Results of operations",
    "2.03": "New material debt obligation",
    "2.05": "Exit or disposal costs",
    "2.06": "Material impairment",
    "3.01": "Delisting or listing-rule notice",
    "4.02": "Prior financials no longer reliable",
    "5.01": "Change in control",
    "5.02": "Officer or director change",
    "5.03": "Charter or bylaw amendment",
    "5.07": "Shareholder vote results",
    "7.01": "Regulation FD disclosure",
    "8.01": "Other events",
    "9.01": "Financial statements and exhibits",
}
EARNINGS_HOURS = {"bmo": "before open", "amc": "after close"}


class LookupFailed(Exception):
    """A call that could not produce data. The message is shown to the model,
    so it must never contain a URL (the Finnhub key travels in the query)."""


class RateLimiter:
    """Spaces calls evenly so a burst never exceeds `calls` per `per_seconds`."""

    def __init__(
        self,
        calls: int,
        per_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_interval = per_seconds / calls
        self.clock = clock
        self.sleep = sleep
        self.next_at = 0.0

    def wait(self) -> None:
        now = self.clock()
        if now < self.next_at:
            self.sleep(self.next_at - now)
            now = self.next_at
        self.next_at = now + self.min_interval


def http_get_json(
    url: str,
    headers: dict[str, str],
    limiter: RateLimiter,
    opener: Callable[..., Any] = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """GET and decode JSON. One retry; a 429 waits for Retry-After first.
    A 404 is final. Raises LookupFailed with a URL-free reason."""
    reason = "no response"
    for attempt in range(2):
        limiter.wait()
        try:
            with opener(urllib.request.Request(url, headers=headers), timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise LookupFailed("not found (HTTP 404)") from None
            reason = f"HTTP {e.code}"
            if e.code == 429 and attempt == 0:
                sleep(float(e.headers.get("Retry-After") or 2))
        except (urllib.error.URLError, OSError, ValueError) as e:
            # URLError is an OSError; ValueError covers malformed JSON.
            reason = type(e).__name__
    raise LookupFailed(reason)


class FinnhubClient:
    def __init__(self, api_key: str, limiter: RateLimiter, get: Callable[..., Any] = http_get_json):
        self.api_key = api_key
        self.limiter = limiter
        self.get = get

    def _call(self, path: str, **params: Any) -> Any:
        query = urllib.parse.urlencode({**params, "token": self.api_key})
        return self.get(f"{FINNHUB_URL}{path}?{query}", {}, self.limiter)

    def metrics(self, symbol: str) -> dict[str, Any]:
        return self._call("/stock/metric", symbol=symbol, metric="all")

    def calendar(self, symbol: str, today: date) -> dict[str, Any]:
        # Per symbol: a date-range query without one is capped at 1,500 rows
        # and silently drops names.
        end = today + timedelta(days=config.EARNINGS_LOOKAHEAD_DAYS)
        return self._call("/calendar/earnings", symbol=symbol,
                          **{"from": today.isoformat(), "to": end.isoformat()})

    def earnings_history(self, symbol: str) -> list[dict[str, Any]]:
        return self._call("/stock/earnings", symbol=symbol, limit=4)


def edgar_symbol(symbol: str) -> str:
    """EDGAR writes share classes with a hyphen: BRK.B is BRK-B there."""
    return symbol.replace(".", "-")


class EdgarClient:
    """Ticker map refreshed daily; companyfacts re-downloaded only when a new
    10-Q/10-K appears (it is ~3.6 MB per company and changes quarterly)."""

    def __init__(
        self,
        user_agent: str,
        limiter: RateLimiter,
        cache_dir: Path,
        today: date,
        get: Callable[..., Any] = http_get_json,
    ):
        self.headers = {"User-Agent": user_agent}
        self.limiter = limiter
        self.cache_dir = Path(cache_dir)
        self.today = today
        self.get = get
        self._ciks: dict[str, int] | None = None

    def _fetch(self, url: str) -> Any:
        return self.get(url, self.headers, self.limiter)

    def cik(self, symbol: str) -> int:
        if self._ciks is None:
            path = self.cache_dir / "company_tickers.json"
            cached = _read_json(path)
            if cached and cached.get("fetched") == self.today.isoformat():
                self._ciks = cached["ciks"]
            else:
                raw = self._fetch(SEC_TICKERS_URL)
                self._ciks = {v["ticker"]: int(v["cik_str"]) for v in raw.values()}
                _write_json(path, {"fetched": self.today.isoformat(), "ciks": self._ciks})
        try:
            return self._ciks[edgar_symbol(symbol)]
        except KeyError:
            raise LookupFailed(f"no SEC company id for {symbol}") from None

    def submissions(self, cik: int) -> dict[str, Any]:
        return self._fetch(SEC_SUBMISSIONS_URL.format(cik=cik))

    def facts(self, cik: int, report_accession: str | None) -> dict[str, Any]:
        # Reused only once it contains the latest 10-Q/10-K. The SEC's facts
        # service can lag a filing by months (GD's 2026-07-29 10-Q was still
        # absent on 2026-10-05); keying the cache on the accession alone would
        # pin the stale copy until the next quarter.
        path = self.cache_dir / f"CIK{cik:010d}.json"
        cached = _read_json(path)
        if (cached and report_accession and cached.get("complete")
                and cached.get("accession") == report_accession):
            path.touch()  # in use: keep it out of prune()
            return cached["facts"]
        facts = self._fetch(SEC_FACTS_URL.format(cik=cik))
        complete = report_accession is not None and report_accession in facts_accessions(facts)
        _write_json(path, {"accession": report_accession, "complete": complete, "facts": facts})
        return facts


    def prune(self, now_epoch: float) -> int:
        """Delete cached companyfacts unused for EDGAR_CACHE_MAX_AGE_DAYS.

        Candidates rotate daily and each file is ~3.6 MB, so without this the
        cache grows by every name the screen has ever surfaced."""
        cutoff = now_epoch - config.EDGAR_CACHE_MAX_AGE_DAYS * 86_400
        removed = 0
        for path in self.cache_dir.glob("CIK*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                pass
        return removed


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _write_json(path: Path, obj: Any) -> None:
    # The cache is an optimisation: failing to write it must not fail a lookup.
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj))
    except OSError:
        pass


# --- parsing: pure functions over the raw responses -------------------------


def _round(value: Any, digits: int) -> float | None:
    return None if value is None else round(float(value), digits)


def five_year(series: list[dict[str, Any]] | None, scale: float = 1.0) -> tuple[list[float], float] | None:
    """[min, max] and median of the last N quarterly points, or None if too few."""
    points = sorted(
        (p for p in series or [] if p.get("v") is not None),
        key=lambda p: p["period"], reverse=True,
    )[: config.FUNDAMENTALS_HISTORY_QUARTERS]
    if len(points) < config.FUNDAMENTALS_MIN_HISTORY_POINTS:
        return None
    values = [p["v"] * scale for p in points]
    return [round(min(values), 1), round(max(values), 1)], round(statistics.median(values), 1)


def valuation_block(metric_json: dict[str, Any], missing: list[str]) -> dict[str, Any]:
    m = metric_json.get("metric") or {}
    quarterly = (metric_json.get("series") or {}).get("quarterly") or {}
    out: dict[str, Any] = {}
    _put(out, missing, "pe_ttm", _round(m.get("peTTM"), 1))
    history = five_year(quarterly.get("peTTM"))
    if history:
        out["pe_ttm_5y_range"], out["pe_ttm_5y_median"] = history
    else:
        missing.append("pe_ttm_5y_range")
    _put(out, missing, "forward_pe", _round(m.get("forwardPE"), 1))
    _put(out, missing, "ev_ebitda_ttm", _round(m.get("evEbitdaTTM"), 1))
    _put(out, missing, "price_to_sales_ttm", _round(m.get("psTTM"), 1))
    _put(out, missing, "dividend_yield_pct", _round(m.get("dividendYieldIndicatedAnnual"), 1))
    return out


def growth_block(metric_json: dict[str, Any], missing: list[str]) -> dict[str, Any]:
    m = metric_json.get("metric") or {}
    quarterly = (metric_json.get("series") or {}).get("quarterly") or {}
    out: dict[str, Any] = {}
    _put(out, missing, "revenue_growth_yoy_ttm_pct", _round(m.get("revenueGrowthTTMYoy"), 1))
    _put(out, missing, "revenue_growth_yoy_last_quarter_pct", _round(m.get("revenueGrowthQuarterlyYoy"), 1))
    _put(out, missing, "eps_growth_yoy_ttm_pct", _round(m.get("epsGrowthTTMYoy"), 1))
    _put(out, missing, "operating_margin_ttm_pct", _round(m.get("operatingMarginTTM"), 1))
    # The quarterly series is a fraction; the TTM figure above is a percent.
    history = five_year(quarterly.get("operatingMargin"), scale=100.0)
    if history:
        out["operating_margin_5y_range_pct"] = history[0]
    else:
        missing.append("operating_margin_5y_range_pct")
    _put(out, missing, "roe_ttm_pct", _round(m.get("roeTTM"), 1))
    _put(out, missing, "debt_to_equity", _round(m.get("totalDebt/totalEquityQuarterly"), 2))
    return out


def earnings_block(
    symbol: str,
    calendar_json: dict[str, Any] | None,
    history_json: list[dict[str, Any]] | None,
    today: date,
    missing: list[str],
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if calendar_json is not None:
        upcoming = sorted(
            (e for e in calendar_json.get("earningsCalendar") or []
             if e.get("date") and e["date"] >= today.isoformat()),
            key=lambda e: e["date"],
        )
        if upcoming:
            e = upcoming[0]
            hour = EARNINGS_HOURS.get(e.get("hour") or "", "time not given")
            out["next_report"] = f"{e['date']} {hour}"
            out["days_until"] = (date.fromisoformat(e["date"]) - today).days
            if e.get("symbol") not in (None, symbol):
                # BRK.B's entry is filed under BRK.A, whose per-share figures are
                # ~1,500x a Class B share's. The date holds; the estimate doesn't.
                out["consensus_note"] = (
                    f"estimates withheld: the calendar lists {e['symbol']}, a "
                    "different share class, so its per-share figures do not apply"
                )
            else:
                _put(out, missing, "consensus_eps", _round(e.get("epsEstimate"), 2))
                revenue = e.get("revenueEstimate")
                _put(out, missing, "consensus_revenue_usd_bn",
                     None if revenue is None else round(revenue / 1e9, 2))
        else:
            out["next_report"] = (
                f"none scheduled in the next {config.EARNINGS_LOOKAHEAD_DAYS} days"
            )
    if history_json is not None:
        own = [h for h in history_json if h.get("symbol") in (None, symbol)]
        if len(own) < len(history_json):
            missing.append("last_4_quarters (reported under a different share class)")
        elif own:
            out["last_4_quarters"] = [
                {
                    "quarter": f"{h['year']}Q{h['quarter']}",
                    "eps_actual": _round(h.get("actual"), 2),
                    "eps_estimate": _round(h.get("estimate"), 2),
                    "surprise_pct": _round(h.get("surprisePercent"), 1),
                }
                for h in sorted(own, key=lambda h: h.get("period") or "", reverse=True)
            ]
    return out


def _quarter_values(facts: dict[str, Any], concepts: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """{period_end: fact} for ~3-month periods from 10-Q/10-K filings.

    The SEC's "frame" labels are sparse (GD's fiscal quarters end off-calendar
    and mostly carry none), so quarters are found by duration instead. Earlier
    concepts in the list win; later ones only fill periods still missing.
    Within a period, the most recently filed value wins (restatements).
    """
    gaap = (facts.get("facts") or {}).get("us-gaap") or {}
    found: dict[str, dict[str, Any]] = {}
    for concept in concepts:
        units = (gaap.get(concept) or {}).get("units") or {}
        for entries in units.values():
            for e in entries:
                if e.get("form") not in REPORT_FORMS or not e.get("start") or not e.get("end"):
                    continue
                days = (date.fromisoformat(e["end"]) - date.fromisoformat(e["start"])).days
                if not 80 <= days <= 100:
                    continue
                current = found.get(e["end"])
                if current is None:
                    found[e["end"]] = {**e, "_concept": concept}
                elif current["_concept"] == concept and e.get("filed", "") > current.get("filed", ""):
                    found[e["end"]] = {**e, "_concept": concept}
    return found


def _year_earlier(values: dict[str, dict[str, Any]], end: str) -> dict[str, Any] | None:
    target = date.fromisoformat(end) - timedelta(days=364)
    near = [e for k, e in values.items() if abs((date.fromisoformat(k) - target).days) <= 10]
    return near[0] if near else None


def facts_accessions(facts: dict[str, Any]) -> set[str]:
    return {
        e.get("accn")
        for concepts in (facts.get("facts") or {}).values()
        for concept in concepts.values()
        for entries in (concept.get("units") or {}).values()
        for e in entries
    }


def reported_results_block(
    facts: dict[str, Any], missing: list[str], latest_report: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    figures = {
        "revenue_usd_bn": (_quarter_values(facts, REVENUE_CONCEPTS), 1e9),
        "operating_income_usd_bn": (_quarter_values(facts, OPERATING_INCOME_CONCEPTS), 1e9),
        "net_income_usd_bn": (_quarter_values(facts, NET_INCOME_CONCEPTS), 1e9),
        "diluted_eps": (_quarter_values(facts, EPS_CONCEPTS), 1.0),
    }
    ends = [end for values, _ in figures.values() for end in values]
    if not ends:
        missing.append("reported_results")
        return None
    latest = max(ends)
    anchor = next(values[latest] for values, _ in figures.values() if latest in values)
    out: dict[str, Any] = {
        "source": f"{anchor['form']}, quarter ended {latest}, filed {anchor['filed']}",
    }
    earlier: dict[str, Any] = {}
    for key, (values, divisor) in figures.items():
        fact = values.get(latest)
        if fact is None:
            missing.append(key)
            continue
        out[key] = round(fact["val"] / divisor, 2)
        prior = _year_earlier(values, latest)
        if prior is not None:
            earlier[key] = round(prior["val"] / divisor, 2)
    if earlier:
        out["same_quarter_last_year"] = earlier
    if latest_report and latest_report["accession"] not in facts_accessions(facts):
        out["newer_filing_not_yet_in_sec_data"] = (
            f"{latest_report['form']} filed {latest_report['filed']}"
        )
    return out


def _accepted_at(recent: dict[str, list[Any]], i: int) -> str:
    accepted = (recent.get("acceptanceDateTime") or [None] * (i + 1))[i]
    return accepted or f"{recent['filingDate'][i]}T00:00:00Z"


def latest_report(submissions: dict[str, Any]) -> dict[str, str] | None:
    """The newest 10-Q/10-K in the filings list (which is newest first)."""
    recent = (submissions.get("filings") or {}).get("recent") or {}
    for i, form in enumerate(recent.get("form") or []):
        if form in REPORT_FORMS:
            return {"form": form, "filed": recent["filingDate"][i],
                    "accession": recent["accessionNumber"][i]}
    return None


def material_filings(
    submissions: dict[str, Any], today: date, last_cycle_at: datetime | None,
) -> list[dict[str, Any]]:
    recent = (submissions.get("filings") or {}).get("recent") or {}
    cutoff = (today - timedelta(days=config.FILINGS_LOOKBACK_DAYS)).isoformat()
    out = []
    for i, form in enumerate(recent.get("form") or []):
        filed = recent["filingDate"][i]
        if form not in MATERIAL_FORMS or filed < cutoff:
            continue
        entry: dict[str, Any] = {"date": filed, "form": form}
        codes = [c for c in ((recent.get("items") or [""] * (i + 1))[i] or "").split(",") if c]
        if codes:
            entry["items"] = "; ".join(f"{c} {EIGHT_K_ITEMS.get(c, '')}".strip() for c in codes)
        accepted = datetime.fromisoformat(_accepted_at(recent, i).replace("Z", "+00:00"))
        entry["new_since_your_last_cycle"] = last_cycle_at is None or accepted > last_cycle_at
        out.append(entry)
    return sorted(out, key=lambda e: e["date"], reverse=True)


def _put(out: dict[str, Any], missing: list[str], key: str, value: Any) -> None:
    if value is None:
        missing.append(key)
    else:
        out[key] = value


# --- assembly ---------------------------------------------------------------


def symbol_block(
    symbol: str,
    finnhub: FinnhubClient | None,
    edgar: EdgarClient | None,
    today: date,
    last_cycle_at: datetime | None,
    unavailable: dict[str, str],
) -> dict[str, Any]:
    """One name's block. `unavailable` names sources that cannot be called at
    all this cycle (e.g. no API key), with the reason to show."""
    if symbol in FUND_SYMBOLS:
        return {"not_applicable": "fund: no company financials or earnings"}
    block: dict[str, Any] = {}
    missing: list[str] = []
    failed: dict[str, str] = dict(unavailable)

    if finnhub is not None:
        metric = calendar = history = None
        problems = []
        for label, call in (
            ("metrics", lambda: finnhub.metrics(symbol)),
            ("calendar", lambda: finnhub.calendar(symbol, today)),
            ("earnings", lambda: finnhub.earnings_history(symbol)),
        ):
            try:
                result = call()
            except LookupFailed as e:
                problems.append(f"{label}: {e}")
                continue
            if label == "metrics":
                metric = result
            elif label == "calendar":
                calendar = result
            else:
                history = result
        if metric is not None:
            block["valuation"] = valuation_block(metric, missing)
            block["growth_and_quality"] = growth_block(metric, missing)
        earnings = earnings_block(symbol, calendar, history, today, missing)
        if earnings:
            block["earnings"] = earnings
        if problems:
            failed["finnhub"] = "; ".join(problems)

    if edgar is not None:
        try:
            cik = edgar.cik(symbol)
            submissions = edgar.submissions(cik)
            report = latest_report(submissions)
            facts = edgar.facts(cik, report["accession"] if report else None)
            results = reported_results_block(facts, missing, report)
            if results:
                block["reported_results"] = results
            block["material_filings_90d"] = material_filings(submissions, today, last_cycle_at)
        except LookupFailed as e:
            failed["edgar"] = str(e)
        except (KeyError, TypeError, AttributeError, ValueError):
            failed["edgar"] = "unexpected response shape"

    if missing:
        block["not_available"] = missing
    if failed:
        block["lookup_failed"] = failed
    return block


def fetch_fundamentals(
    symbols: list[str],
    last_cycle_at: datetime | None,
    now: datetime,
    finnhub: FinnhubClient | None = None,
    edgar: EdgarClient | None = None,
) -> dict[str, dict[str, Any]]:
    """Blocks for every symbol, keyed by symbol. Clients are built from the
    environment unless passed in (tests pass fakes)."""
    today = date.fromisoformat(markettime.fmt_et_date(now))
    unavailable: dict[str, str] = {}
    if finnhub is None:
        key = os.environ.get("FINNHUB_API_KEY", "").strip()
        if key:
            finnhub = FinnhubClient(key, RateLimiter(config.FINNHUB_CALLS_PER_MIN, 60.0))
        else:
            unavailable["finnhub"] = "FINNHUB_API_KEY is not set"
    if edgar is None:
        agent = os.environ.get("SEC_USER_AGENT", "").strip()
        if agent:
            edgar = EdgarClient(
                agent, RateLimiter(config.EDGAR_CALLS_PER_SEC, 1.0),
                Path(config.EDGAR_CACHE_DIR), today,
            )
        else:
            unavailable["edgar"] = "SEC_USER_AGENT is not set"
    for source, reason in unavailable.items():
        print(f"warning: fundamentals: {source} unavailable ({reason})")
    blocks = {
        s: symbol_block(s, finnhub, edgar, today, last_cycle_at, unavailable)
        for s in symbols
    }
    if edgar is not None:
        edgar.prune(time.time())
    return blocks
```

- [ ] **Step 4: Run the tests**

Run: `venv/bin/python -m pytest -q test_fundamentals.py`
Expected: `28 passed`.

- [ ] **Step 5: Lint, run the whole suite, and commit**

Run: `venv/bin/python -m pyflakes fundamentals.py test_fundamentals.py && venv/bin/python -m pytest -q`
Expected: no pyflakes output, then `120 passed` (92 + 28).

```bash
git add fundamentals.py test_fundamentals.py
git commit -F - <<'EOF'
Add fundamentals.py: Finnhub and EDGAR facts per symbol

fetch_fundamentals() builds one facts-only block per symbol: valuation
against its own 5-year history, growth and quality, the next report
with consensus, the last 4 quarters' EPS against estimate, the latest
reported quarter against a year earlier, and material filings from
the last 90 days with new-since-last-cycle flags.

- Never raises for data or network problems: each source's failure is
  named per symbol in lookup_failed, and failure reasons never contain
  a URL (the Finnhub key travels in the query).
- Finnhub is queried per symbol (range queries are capped at 1,500 rows
  and drop names) and paced to 55 calls/min.
- Reported quarters are found by duration, not the SEC's sparse frame
  labels. A newer 10-Q/10-K missing from the SEC's data is named, and
  the cache is not reused until the data catches up.
- BRK.B's estimates, filed under BRK.A, are withheld rather than shown
  at ~1,500x a Class B share. Funds are not applicable.
- Cached companyfacts unused for 30 days are pruned.
EOF
```

---

### Task 3: Wire it into the prompt and the cycle; re-capture the golden; dry run

**Files:**
- Create: `test_prompt_fundamentals.py`
- Modify: `prompt.py` (system prompt section, `build_decision_prompt`), `agent.py` (import, `fundamentals_for`, the call in `run_decision_cycle`), `tests/golden.py` (`build_prompt`, `capture`)

**Interfaces:**
- Consumes: `fundamentals.fetch_fundamentals` (Task 2).
- Produces: `agent.fundamentals_for(conn, portfolio, market, now) -> dict[str, dict]`, and `build_decision_prompt(..., as_of, fundamentals: dict[str, dict] | None = None)`. With `None`, the prompt is byte-identical to before.

- [ ] **Step 1: Write the wiring tests**

Create `test_prompt_fundamentals.py`:

```python
"""The fundamentals block reaches the prompt on the right names, and fetching
it can never fail the cycle."""

import json
from datetime import datetime, timezone

import agent

NOW = datetime(2026, 10, 5, 20, 30, tzinfo=timezone.utc)
GD_BLOCK = {"valuation": {"pe_ttm": 19.9}}
IQV_BLOCK = {"lookup_failed": {"finnhub": "metrics: HTTP 500"}}


def portfolio():
    return agent.PortfolioState(
        cash=10_000.0, equity=20_000.0,
        positions={"GD": agent.Position("GD", 10.0, 10_000.0, 300.0)}, timestamp=NOW,
    )


def market():
    screen = {s: {"chg_5d": 1.0} for s in ("GD", "IQV", "XOM")}
    return agent.MarketContext(
        prices={"GD": 1_000.0}, articles=[], news_lookup_failed=set(), market_news=[],
        watchlist=list(screen), screen=screen, candidates=["IQV", "XOM"],
    )


def state(fundamentals):
    prompt = agent.build_decision_prompt(
        portfolio(), market(), {}, {}, {}, {}, NOW, fundamentals,
    )
    return json.loads(prompt.split("(JSON):\n", 1)[1].rsplit("\n\n", 1)[0])


def test_blocks_attach_to_holdings_and_candidates():
    s = state({"GD": GD_BLOCK, "IQV": IQV_BLOCK})
    assert s["positions"][0]["fundamentals"] == GD_BLOCK
    candidates = {c["symbol"]: c for c in s["candidates"]}
    assert candidates["IQV"]["fundamentals"] == IQV_BLOCK  # a flagged block, verbatim
    assert "fundamentals" not in candidates["XOM"]


def test_no_fundamentals_means_no_field():
    s = state(None)
    assert "fundamentals" not in s["positions"][0]
    assert all("fundamentals" not in c for c in s["candidates"])


def test_system_prompt_explains_the_block():
    assert 'WHAT "fundamentals" IS' in agent.SYSTEM_PROMPT
    assert "lookup_failed" in agent.SYSTEM_PROMPT


def test_fundamentals_for_asks_for_holdings_and_candidates(monkeypatch):
    conn = agent.init_db(":memory:")
    agent.log_decision_cycle(conn, "p", "r", [], [], portfolio(), NOW, "paper", (1, 2), None)
    seen = {}

    def fake(symbols, last_cycle_at, now):
        seen.update(symbols=symbols, last=last_cycle_at)
        return {s: {} for s in symbols}

    # Patched where fundamentals_for looks it up, never via another module.
    monkeypatch.setitem(agent.fundamentals_for.__globals__, "fetch_fundamentals", fake)
    agent.fundamentals_for(conn, portfolio(), market(), NOW)
    assert seen == {"symbols": ["GD", "IQV", "XOM"], "last": NOW}


def test_a_bug_in_fetching_never_fails_the_cycle(monkeypatch, capsys):
    def broken(symbols, last_cycle_at, now):
        raise RuntimeError("bug")

    monkeypatch.setitem(agent.fundamentals_for.__globals__, "fetch_fundamentals", broken)
    out = agent.fundamentals_for(agent.init_db(":memory:"), portfolio(), market(), NOW)
    assert out == {s: {"lookup_failed": {"fundamentals": "internal error"}}
                   for s in ("GD", "IQV", "XOM")}
    assert "RuntimeError: bug" in capsys.readouterr().err
```

- [ ] **Step 2: Run them and watch them fail**

Run: `venv/bin/python -m pytest -q test_prompt_fundamentals.py`
Expected: 5 failed. `build_decision_prompt` takes no 8th argument (TypeError), the system-prompt section is absent, and `agent.fundamentals_for` does not exist (AttributeError).

- [ ] **Step 3: Add the system-prompt section**

In `prompt.py`, directly after the paragraph ending `A cycle where you did \` / `not look is not.` (the end of `WHAT "candidates" IS`) and before the blank line preceding `Guardrails your proposals will be checked against:`, insert:

```
WHAT "fundamentals" IS

Every holding and every candidate carries a "fundamentals" block, looked up fresh \
each cycle from two sources:
- "valuation" and "growth_and_quality" (Finnhub): trailing-twelve-month (TTM) \
  figures. "_5y_range" and "_5y_median" give the same measure over the last 20 \
  quarters, so a current figure can be read against the company's own history.
- "earnings" (Finnhub): the next scheduled report with the consensus EPS and \
  revenue estimates, and the last four quarters' EPS against the estimate at the \
  time. Consensus is the average of analysts' forecasts, not a target.
- "reported_results" (SEC EDGAR, from the company's own 10-Q/10-K): the latest \
  quarter in the SEC's structured data against the same quarter a year earlier. \
  That data can lag a filing by weeks or months; when it does, \
  "newer_filing_not_yet_in_sec_data" names the filing it is missing, and the \
  figures are older than that filing.
- "material_filings_90d" (EDGAR): 8-Ks with their item descriptions, 10-Qs, 10-Ks \
  and spin-off or merger registrations from the last 90 days. \
  "new_since_your_last_cycle" marks filings you have not been shown before.

"not_available" names figures the source does not provide for that company (banks, \
for example, report no operating margin). "not_applicable" marks funds, which have \
no company financials. "lookup_failed" means that source was not reached this \
cycle, so its silence says nothing about the company.

These are reported figures and published estimates, not judgements. Nothing in the \
block says whether a stock is cheap, whether a quarter was good, or what to do.
```

(Keep the trailing backslash line continuations exactly as shown; `SYSTEM_PROMPT` is one string built that way.)

- [ ] **Step 4: Attach the blocks in `build_decision_prompt`**

In `prompt.py`, add the parameter after `as_of: datetime,`:

```python
    fundamentals: dict[str, dict[str, Any]] | None = None,
```

After the existing lines

```python
        if p.symbol in market.screen:
            row["screen"] = market.screen[p.symbol]
```

add:

```python
        if fundamentals and p.symbol in fundamentals:
            row["fundamentals"] = fundamentals[p.symbol]
```

In the `"candidates"` list comprehension, after `**market.screen[s],`, add:

```python
                **({"fundamentals": fundamentals[s]} if fundamentals and s in fundamentals else {}),
```

- [ ] **Step 5: Call it from the cycle**

In `agent.py`, add `from fundamentals import fetch_fundamentals` after `import markettime`. Then add this function directly above `def run_decision_cycle(`:

```python
def fundamentals_for(conn, portfolio, market, now: datetime) -> dict[str, dict]:
    """Fundamentals for every name in the prompt: holdings and candidates.

    Never fails the cycle. fetch_fundamentals already turns data and network
    problems into per-name `lookup_failed`; this also covers a bug in it, so
    the agent decides on prices and news alone rather than not at all.
    """
    symbols = sorted(set(portfolio.positions) | set(market.candidates))
    (last,) = conn.execute("SELECT MAX(timestamp) FROM decision_cycles").fetchone()
    last_cycle_at = datetime.fromisoformat(last) if last else None
    try:
        blocks = fetch_fundamentals(symbols, last_cycle_at, now)
    except Exception:
        traceback.print_exc()
        return {s: {"lookup_failed": {"fundamentals": "internal error"}} for s in symbols}
    flagged = sum("lookup_failed" in b for b in blocks.values())
    print(f"fundamentals: {len(blocks)} names, {flagged} with a failed lookup")
    return blocks
```

In `run_decision_cycle`, replace

```python
        prompt = build_decision_prompt(
            portfolio, market, theses, actions, news_history, candidate_news, now,
        )
```

with

```python
        fundamentals = fundamentals_for(conn, portfolio, market, now)
        prompt = build_decision_prompt(
            portfolio, market, theses, actions, news_history, candidate_news, now,
            fundamentals,
        )
```

- [ ] **Step 6: Run the wiring tests**

Run: `venv/bin/python -m pytest -q test_prompt_fundamentals.py && venv/bin/python -m pytest -q`
Expected: `5 passed`. Then the whole suite gives `1 failed, 124 passed`. The one failure is `test_system_prompt_is_unchanged`, **as intended**: the system prompt gained a section. `test_decision_prompt_is_byte_identical` still passes, which proves the hook adds nothing when no fundamentals are passed.

- [ ] **Step 7: Teach the golden fixture about fundamentals**

In `tests/golden.py`, change `build_prompt`'s return to pass the stored block:

```python
    return agent.build_decision_prompt(
        portfolio, market, theses, actions, news_history, candidate_news, now,
        inputs.get("fundamentals"),
    )
```

and in `capture()`, replace

```python
    agent.store_news(dst, market.articles, now)
    dst.close()

    inputs = {"now": now, "portfolio": portfolio, "market": market}
```

with

```python
    agent.store_news(dst, market.articles, now)
    fundamentals = agent.fundamentals_for(dst, portfolio, market, now)
    dst.close()

    inputs = {"now": now, "portfolio": portfolio, "market": market,
              "fundamentals": fundamentals}
```

- [ ] **Step 8: Re-capture the golden, which is also the read-only dry run**

This step uses the real APIs, read-only: Alpaca, Finnhub (~144 calls) and EDGAR. It makes no trades and no Claude call, and doesn't write `trading_agent.db`.

```bash
B=$(stat -f %m ../Claude-Autonomous-Trading-Agent/trading_agent.db)
time venv/bin/python -c "import sys; from pathlib import Path; sys.path.insert(0,'tests'); import golden; golden.capture(Path('../Claude-Autonomous-Trading-Agent/trading_agent.db'))"
[ "$(stat -f %m ../Claude-Autonomous-Trading-Agent/trading_agent.db)" = "$B" ] && echo "real DB untouched"
```

Expected: a `fundamentals: N names, M with a failed lookup` line with N ≈ 48 and M small (ideally 0), then `captured golden fixture …`, then `real DB untouched`. Expect a runtime of roughly 3–4 minutes.

- [ ] **Step 9: Measure coverage and prompt size**

```bash
venv/bin/python - <<'EOF'
import json, pickle, sys
from collections import Counter
sys.path.insert(0, "tests"); import golden
inputs = pickle.loads(golden.INPUTS.read_bytes())
blocks = inputs["fundamentals"]
full = [s for s, b in blocks.items() if "lookup_failed" not in b and "not_applicable" not in b and not b.get("not_available")]
flagged = {s: b["lookup_failed"] for s, b in blocks.items() if "lookup_failed" in b}
stale = [s for s, b in blocks.items() if "newer_filing_not_yet_in_sec_data" in b.get("reported_results", {})]
missing = Counter(x for b in blocks.values() for x in b.get("not_available", []))
print("names", len(blocks), "| complete", len(full), "| flagged", len(flagged), "| SEC data stale", len(stale))
print("flagged:", flagged)
print("most common not_available:", missing.most_common(6))
prompt = golden.PROMPT.read_text()
print("token in prompt:", "token=" in prompt)
import anthropic, config
from dotenv import load_dotenv; load_dotenv(".env")
c = anthropic.Anthropic()
def count(text): return c.messages.count_tokens(model=config.CLAUDE_MODEL, system=golden.agent.SYSTEM_PROMPT, messages=[{"role": "user", "content": text}]).input_tokens
old = open("../Claude-Autonomous-Trading-Agent/logs/prompts/53.txt").read()
print("tokens now", count(prompt), "| cycle 53 prompt log", count(old))
EOF
```

Expected: `token in prompt: False`. Fundamentals add roughly 12k tokens: compare `tokens now` with cycle 53's figure, keeping in mind the prompt-log file also carries a header. `count_tokens` is free. If the added amount exceeds **20k**, record a ruling and, per the spec, cut candidates' `last_4_quarters` to 2 before anything else.

- [ ] **Step 10: Run the full suite**

Run: `venv/bin/python -m pytest -q`
Expected: `125 passed`, with the golden pair green on the re-captured fixture.

- [ ] **Step 11: Lint and commit**

Run: `venv/bin/python -m pyflakes fundamentals.py prompt.py tests/golden.py test_prompt_fundamentals.py; venv/bin/python -m pyflakes agent.py | grep -v "imported but unused"`
Expected: no output.

```bash
git add prompt.py agent.py tests/golden.py test_prompt_fundamentals.py
git commit -F - <<'EOF'
Show the agent fundamentals for every name it is weighing

- agent.py: fundamentals_for() fetches blocks for holdings and
  candidates after the screen. A bug in it is caught and becomes
  lookup_failed on every name, so the cycle runs on prices and news
  rather than not at all.
- prompt.py: each holding and candidate carries its "fundamentals"
  block, and the system prompt gains WHAT "fundamentals" IS:
  where each part comes from, what not_available / not_applicable /
  lookup_failed mean, and that the figures are not judgements. With
  no fundamentals passed, the prompt is byte-identical to before.
- tests/golden.py: stores fundamentals with the snapshot. The golden
  fixture is deliberately re-captured; the dry run behind it measured
  coverage and prompt size against the real APIs.
EOF
```

---

### Task 4: Documentation

**Files:**
- Modify: `CLAUDE.md`, `README.md`

- [ ] **Step 1: CLAUDE.md**

- **Module table:** add `| \`fundamentals.py\` | Finnhub + SEC EDGAR facts per name (valuation vs own 5-year history, earnings dates and consensus, last 4 quarters vs estimate, latest reported quarter, material filings). Never raises; failures become per-name \`lookup_failed\`. Cache in \`cache/edgar/\` (gitignored, pruned after 30 days). |`.
- **"What the model can see of its own past":** add a bullet after the news bullet: "**The fundamentals** (`fundamentals_for` → `fetch_fundamentals`): facts, not verdicts, for every holding and candidate, attached as `fundamentals`. The SEC's structured data can lag a 10-Q by months (GD's 2026-07-29 10-Q was absent on 2026-10-05), which is named as `newer_filing_not_yet_in_sec_data`; Finnhub's EPS actuals arrive faster. BRK.B's estimates are filed under BRK.A, and are withheld."
- **"Other README divergences":** replace "Dependencies are exactly `alpaca-py`, `anthropic`, `python-dotenv`; prices, SPY closes, and news all come from Alpaca." with "Dependencies are exactly `alpaca-py`, `anthropic`, `python-dotenv`. Prices, SPY closes and news come from Alpaca; fundamentals from Finnhub (free tier, `FINNHUB_API_KEY`) and SEC EDGAR (`SEC_USER_AGENT`, which the SEC requires to carry contact details)."
- **Secrets line:** add `FINNHUB_API_KEY` and `SEC_USER_AGENT` to the list of variables in `.env`.
- **Tests paragraph:** add `test_fundamentals.py` (recorded fixtures in `tests/data/fundamentals/`, re-record with `record.py`) and `test_prompt_fundamentals.py`.

- [ ] **Step 2: README.md Data Sources**

Replace the section's first paragraph ("**Alpaca is the only data source.** …") with: "**Alpaca** covers positions, account state, order execution, prices and news. **Finnhub** (free tier) supplies valuation and its 5-year history, earnings dates with consensus, and recent EPS against estimate. **SEC EDGAR** supplies reported quarterly results and material filings. Claude's training data is stale for markets, so all of this is injected fresh every cycle and never recalled from the model." In the Tech Stack list, add "**Finnhub + SEC EDGAR**: fundamentals".

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md README.md
git commit -F - <<'EOF'
Document the fundamentals feed

CLAUDE.md: the module, what the agent now sees and the SEC data lag,
the new .env variables, and the new tests. README.md: data sources
are no longer Alpaca alone.
EOF
```

---

### Task 5: Merge before a cycle, then verify the first live run

- [ ] **Step 1: Final review, then merge** (merge only between cycles, with the operator's go-ahead)

```bash
cd ../Claude-Autonomous-Trading-Agent
git merge --ff-only feature/fundamentals
mkdir -p tests/fixtures && cp ../Claude-Autonomous-Trading-Agent-fundamentals/tests/fixtures/* tests/fixtures/
venv/bin/python -m pytest -q
(cd / && /Users/aydenwong/Projects/Claude-Autonomous-Trading-Agent/venv/bin/python /Users/aydenwong/Projects/Claude-Autonomous-Trading-Agent/agent.py --help | head -1)
git status --short
```

Expected: fast-forward; `125 passed`; the usage line; a clean tree apart from `dashboard.html`. Then remove the worktree (`git worktree remove ../Claude-Autonomous-Trading-Agent-fundamentals`) and delete the branch.

- [ ] **Step 2: After the next 16:30 ET cycle**

```bash
tail -14 logs/agent.log
sqlite3 trading_agent.db "select id, outcome, substr(error_text,1,200) from run_log order by id desc limit 1;"
sqlite3 trading_agent.db "select id, code_commit, code_dirty, system_prompt_sha, input_tokens from decision_cycles order by id desc limit 2;"
grep -c '"fundamentals"' logs/prompts/$(sqlite3 trading_agent.db "select max(id) from decision_cycles").txt
```

Expected: a `fundamentals: N names, M with a failed lookup` line and `=== done (agent=0 dashboard=0) ===`; `success`; the new `code_commit` with `code_dirty = 0`; a **changed** `system_prompt_sha`, the deliberate change; `input_tokens` about 12k above the previous cycle; and the `fundamentals` count equal to holdings plus candidates.

- [ ] **Step 3: If the run failed**

`fundamentals_for` cannot fail the cycle, so a failure here is elsewhere. Read `run_log.error_text`. To restore the previous behaviour, revert the Task 3 commit on `main` (`git revert --no-edit <task-3-commit>`), push, and diagnose on a branch.
