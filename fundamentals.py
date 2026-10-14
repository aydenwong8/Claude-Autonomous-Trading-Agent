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

import http.client
import json
import os
import re
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

import config
import markettime

FINNHUB_URL = "https://finnhub.io/api/v1"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SEC_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SEC_FILING_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{name}"

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
# What a response of the wrong shape raises while being parsed.
PARSE_ERRORS = (KeyError, TypeError, AttributeError, ValueError, IndexError)


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
    return _http_get(url, headers, limiter, json.load, opener, sleep)


def http_get_text(
    url: str,
    headers: dict[str, str],
    limiter: RateLimiter,
    opener: Callable[..., Any] = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """GET a document as text, with http_get_json's retry and failure rules."""
    return _http_get(url, headers, limiter,
                     lambda r: r.read().decode("utf-8", errors="replace"), opener, sleep)


def _http_get(
    url: str,
    headers: dict[str, str],
    limiter: RateLimiter,
    decode: Callable[[Any], Any],
    opener: Callable[..., Any],
    sleep: Callable[[float], None],
) -> Any:
    reason = "no response"
    for attempt in range(2):
        limiter.wait()
        try:
            with opener(urllib.request.Request(url, headers=headers), timeout=30) as r:
                return decode(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise LookupFailed("not found (HTTP 404)") from None
            reason = f"HTTP {e.code}"
            if e.code == 429 and attempt == 0:
                sleep(_retry_after(e.headers.get("Retry-After")))
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
            # URLError is an OSError; ValueError covers malformed JSON;
            # HTTPException covers a truncated body (IncompleteRead).
            reason = type(e).__name__
    raise LookupFailed(reason)


def _retry_after(value: str | None) -> float:
    """Seconds to wait after a 429. The header may also be an HTTP-date, and
    is capped so one rate-limit reply cannot stall the cycle."""
    try:
        return min(max(float(value), 0.0), 60.0)
    except (TypeError, ValueError):
        return 2.0


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
        get_text: Callable[..., str] = http_get_text,
    ):
        self.headers = {"User-Agent": user_agent}
        self.limiter = limiter
        self.cache_dir = Path(cache_dir)
        self.today = today
        self.get = get
        self.get_text = get_text
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

    def filing_index(self, cik: int, accession: str) -> dict[str, Any]:
        # A filing's contents never change once accepted, so this is cached
        # for as long as it keeps being used, like document_text.
        path = self.cache_dir / "docs" / f"{accession}-index.json"
        cached = _read_json(path)
        if cached is not None:
            path.touch()
            return cached
        index = self._fetch(SEC_FILING_URL.format(
            cik=cik, accession=accession.replace("-", ""), name="index.json"))
        _write_json(path, index)
        return index

    def document_text(self, cik: int, accession: str, name: str) -> str:
        """A filed document as plain text. Filings never change once accepted,
        so the converted text is cached by accession and fetched only once."""
        path = self.cache_dir / "docs" / f"{accession}-{name}.txt"
        try:
            text = path.read_text()
            path.touch()  # in use: keep it out of prune()
            return text
        except OSError:
            pass
        html = self.get_text(
            SEC_FILING_URL.format(cik=cik, accession=accession.replace("-", ""), name=name),
            self.headers, self.limiter,
        )
        text = html_to_text(html)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        except OSError:
            pass
        return text

    def prune(self, now_epoch: float) -> int:
        """Delete cached companyfacts and documents unused for EDGAR_CACHE_MAX_AGE_DAYS.

        Candidates rotate daily and each file is ~3.6 MB, so without this the
        cache grows by every name the screen has ever surfaced."""
        cutoff = now_epoch - config.EDGAR_CACHE_MAX_AGE_DAYS * 86_400
        removed = 0
        for path in [*self.cache_dir.glob("CIK*.json"), *self.cache_dir.glob("docs/*")]:
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
        out["pe_ttm_5y_range"] = _span(history[0])
        out["pe_ttm_5y_median"] = history[1]
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
        out["operating_margin_5y_range_pct"] = _span(history[0])
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
            # One line per quarter: as structured JSON these were a fifth
            # of the whole block once indented into the prompt.
            out["last_4_quarters"] = [
                _quarter_line(h)
                for h in sorted(own, key=lambda h: h.get("period") or "", reverse=True)
            ]
    return out


def _quarter_line(h: dict[str, Any]) -> str:
    actual, estimate = _round(h.get("actual"), 2), _round(h.get("estimate"), 2)
    surprise = _round(h.get("surprisePercent"), 1)
    line = (f"{h['year']}Q{h['quarter']}: EPS "
            f"{'n/a' if actual is None else actual} vs "
            f"{'n/a' if estimate is None else estimate} est")
    return line if surprise is None else f"{line} ({surprise:+.1f}%)"


def _span(bounds: list[float]) -> str:
    return f"{bounds[0]} to {bounds[1]}"


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
    elif latest_report and anchor.get("accn") != latest_report["accession"]:
        # The filing is in the data but the quarter shown is not from it. For a
        # 10-K that is normal: it reports the full year with no separate fourth
        # quarter, so from February to the Q1 10-Q the newest quarter is two
        # reports old.
        why = ("it reports the full year, with no separate quarter"
               if latest_report["form"].startswith("10-K")
               else "its quarterly figures are not in the SEC's data yet")
        out["newer_filing_not_yet_in_sec_data"] = (
            f"{latest_report['form']} filed {latest_report['filed']} ({why})"
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


def _items(recent: dict[str, list[Any]], i: int) -> list[str]:
    return [c for c in ((recent.get("items") or [""] * (i + 1))[i] or "").split(",") if c]


def _filing_line(recent: dict[str, list[Any]], i: int, last_cycle_at: datetime | None) -> str:
    """"2026-07-29 8-K: 2.02 Results of operations [new since your last cycle]"."""
    line = f"{recent['filingDate'][i]} {recent['form'][i]}"
    codes = _items(recent, i)
    if len(codes) > 1:
        # 9.01 lists the exhibits; beside a real item it adds nothing.
        codes = [c for c in codes if c != "9.01"]
    if codes:
        line += ": " + "; ".join(f"{c} {EIGHT_K_ITEMS.get(c, '')}".strip() for c in codes)
    accepted = datetime.fromisoformat(_accepted_at(recent, i).replace("Z", "+00:00"))
    if last_cycle_at is None or accepted > last_cycle_at:
        line += " [new since your last cycle]"
    return line


def material_filings(
    submissions: dict[str, Any], today: date, last_cycle_at: datetime | None,
) -> list[str]:
    """One line per filing, newest first: "2026-07-29 8-K: 2.02 Results of
    operations [new since your last cycle]". Lines rather than objects, which
    cost several times the tokens once indented into the prompt."""
    recent = (submissions.get("filings") or {}).get("recent") or {}
    cutoff = (today - timedelta(days=config.FILINGS_LOOKBACK_DAYS)).isoformat()
    out = []
    for i, form in enumerate(recent.get("form") or []):
        if form not in MATERIAL_FORMS or recent["filingDate"][i] < cutoff:
            continue
        out.append(_filing_line(recent, i, last_cycle_at))
    # By date only: the SEC lists newest first, and a stable sort keeps that
    # order within a day (sorting whole lines would put "8-K" after "10-Q").
    return sorted(out, key=lambda line: line[:10], reverse=True)


class _TextExtractor(HTMLParser):
    """Filed HTML as lines: a table row on one line with its non-empty cells
    joined by " | ", other blocks a line each. Filings lay out every figure in
    tables, and one cell per line (or the empty spacer cells kept) roughly
    doubled the size of a release without adding a figure."""

    BLOCKS = frozenset({"p", "div", "br", "li", "h1", "h2", "h3", "h4", "table"})
    SKIP = frozenset({"script", "style"})

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []
        self.text: list[str] = []
        self.row: list[str] | None = None
        self.cell: list[str] | None = None
        self.skipping = 0

    def _flush(self) -> None:
        line = " ".join("".join(self.text).split())
        if line:
            self.lines.append(line)
        self.text = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self.SKIP:
            self.skipping += 1
        elif tag == "tr":
            self._flush()
            self.row = []
        elif tag in ("td", "th"):
            self.cell = []
        elif tag in self.BLOCKS and self.cell is None:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self.skipping = max(self.skipping - 1, 0)
        elif tag in ("td", "th") and self.cell is not None:
            cell = " ".join("".join(self.cell).split())
            if cell and self.row is not None:
                self.row.append(cell)
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.row:
                self.lines.append(" | ".join(self.row))
            self.row = None
        elif tag in self.BLOCKS and self.cell is None:
            self._flush()

    def handle_data(self, data: str) -> None:
        if not self.skipping:
            (self.cell if self.cell is not None else self.text).append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    parser._flush()
    return "\n".join(parser.lines)


def _cap(text: str, limit: int) -> tuple[str, str | None]:
    """At most `limit` characters, cut at a line end, and a note when cut."""
    if len(text) <= limit:
        return text, None
    head = text[:limit]
    cut = head.rfind("\n")
    if cut > limit // 2:
        head = head[:cut]
    return head, f"first {len(head)} of {len(text)} characters"


def latest_earnings_release(submissions: dict[str, Any], today: date) -> dict[str, str] | None:
    """The newest 8-K reporting results (item 2.02) within EARNINGS_RELEASE_DAYS."""
    recent = (submissions.get("filings") or {}).get("recent") or {}
    cutoff = (today - timedelta(days=config.EARNINGS_RELEASE_DAYS)).isoformat()
    for i, form in enumerate(recent.get("form") or []):
        filed = recent["filingDate"][i]
        if filed < cutoff:
            return None  # newest first: nothing older can qualify
        items = ((recent.get("items") or [""] * (i + 1))[i] or "").split(",")
        if form in ("8-K", "8-K/A") and "2.02" in items:
            return {"accession": recent["accessionNumber"][i], "filed": filed,
                    "accepted": _accepted_at(recent, i)}
    return None


_EX_99_1 = re.compile(r"ex(?:h|hibit)?[-_]?99[-_.]?0?1(?!\d)")


def release_exhibit(index: dict[str, Any]) -> str | None:
    """The press release in an earnings 8-K: exhibit 99.1. Not 99.2, which is
    usually the much longer financial supplement (JPM's is ~3.4 MB)."""
    for item in (index.get("directory") or {}).get("item") or []:
        name = item.get("name") or ""
        if name.lower().endswith((".htm", ".html")) and _EX_99_1.search(name.lower()):
            return name
    return None


def earnings_release_block(
    edgar: EdgarClient, cik: int, submissions: dict[str, Any], today: date,
    last_cycle_at: datetime | None,
) -> dict[str, Any] | None:
    """The opening of the latest earnings release, or None if there is none
    within the window. Raises LookupFailed when one exists but cannot be read."""
    release = latest_earnings_release(submissions, today)
    if release is None:
        return None
    exhibit = release_exhibit(edgar.filing_index(cik, release["accession"]))
    if exhibit is None:
        raise LookupFailed(f"8-K filed {release['filed']} has no exhibit 99.1")
    text, note = _cap(edgar.document_text(cik, release["accession"], exhibit),
                      config.EARNINGS_RELEASE_MAX_CHARS)
    source = f"8-K filed {release['filed']}, item 2.02, exhibit {exhibit}"
    accepted = datetime.fromisoformat(release["accepted"].replace("Z", "+00:00"))
    if last_cycle_at is None or accepted > last_cycle_at:
        source += " [new since your last cycle]"
    block: dict[str, Any] = {"source": source, "text": text}
    if note:
        block["truncated"] = note
    return block


_ITEM_HEADING = re.compile(r"^Item \d+\.\d{2}\b", re.MULTILINE)
_BODY_END = re.compile(r"^(?:Item 9\.01\b|SIGNATURES?\b)", re.MULTILINE)


def eight_k_body(text: str) -> str:
    """An 8-K's item sections: from the first "Item N.NN" heading to the
    exhibit list (9.01) or the signature. The cover page before it is the same
    boilerplate in every filing (~2-4k characters of addresses and checkboxes)."""
    start = _ITEM_HEADING.search(text)
    if start is None:
        return text
    body = text[start.start():]
    end = _BODY_END.search(body)
    return (body[: end.start()] if end else body).rstrip()


def recent_8ks(submissions: dict[str, Any], today: date) -> list[dict[str, Any]]:
    """8-Ks within EIGHT_K_TEXT_DAYS, newest first, at most EIGHT_K_TEXT_MAX_FILINGS.
    Results 8-Ks (item 2.02) are left out: earnings_release covers them."""
    recent = (submissions.get("filings") or {}).get("recent") or {}
    cutoff = (today - timedelta(days=config.EIGHT_K_TEXT_DAYS)).isoformat()
    picked = []
    for i, form in enumerate(recent.get("form") or []):
        if recent["filingDate"][i] < cutoff:
            break  # newest first: nothing older can qualify
        if form in ("8-K", "8-K/A") and "2.02" not in _items(recent, i):
            picked.append({"index": i, "accession": recent["accessionNumber"][i],
                           "document": recent["primaryDocument"][i]})
            if len(picked) == config.EIGHT_K_TEXT_MAX_FILINGS:
                break
    return picked


def eight_k_text_block(
    edgar: EdgarClient, cik: int, submissions: dict[str, Any], today: date,
    last_cycle_at: datetime | None,
) -> list[dict[str, Any]]:
    """One entry per recent 8-K: its filing line (as in material_filings_90d)
    and item text. A document that cannot be read is flagged in its own entry."""
    recent = submissions["filings"]["recent"]
    entries = []
    for filing in recent_8ks(submissions, today):
        entry: dict[str, Any] = {"filing": _filing_line(recent, filing["index"], last_cycle_at)}
        try:
            text = edgar.document_text(cik, filing["accession"], filing["document"])
        except LookupFailed as e:
            entry["lookup_failed"] = str(e)
        else:
            entry["text"], note = _cap(eight_k_body(text), config.EIGHT_K_MAX_CHARS)
            if note:
                entry["truncated"] = note
        entries.append(entry)
    return entries


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
    held: bool = False,
) -> dict[str, Any]:
    """One name's block. `unavailable` names sources that cannot be called at
    all this cycle (e.g. no API key), with the reason to show. `held` adds the
    filed documents themselves: the latest earnings release and recent 8-Ks'
    item text (holdings only: a few thousand tokens each)."""
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
        # Parsed into locals first, so a malformed payload flags this name's
        # Finnhub data instead of escaping and blanking every name's block.
        parsed: dict[str, Any] = {}
        noted: list[str] = []
        try:
            if metric is not None:
                parsed["valuation"] = valuation_block(metric, noted)
                parsed["growth_and_quality"] = growth_block(metric, noted)
            earnings = earnings_block(symbol, calendar, history, today, noted)
            if earnings:
                parsed["earnings"] = earnings
        except PARSE_ERRORS:
            problems = ["unexpected response shape"]
        else:
            block.update(parsed)
            missing.extend(noted)
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
        except PARSE_ERRORS:
            failed["edgar"] = "unexpected response shape"
        else:
            # Its own failure, so an unreadable exhibit costs only the release.
            if held:
                try:
                    release = earnings_release_block(edgar, cik, submissions, today, last_cycle_at)
                    if release:
                        block["earnings_release"] = release
                except LookupFailed as e:
                    failed["earnings_release"] = str(e)
                except PARSE_ERRORS:
                    failed["earnings_release"] = "unexpected response shape"
                try:
                    eight_ks = eight_k_text_block(edgar, cik, submissions, today, last_cycle_at)
                    if eight_ks:
                        block["recent_8k_text"] = eight_ks
                except PARSE_ERRORS:
                    failed["recent_8k_text"] = "unexpected response shape"

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
    held: frozenset[str] | set[str] = frozenset(),
) -> dict[str, dict[str, Any]]:
    """Blocks for every symbol, keyed by symbol. Clients are built from the
    environment unless passed in (tests pass fakes). Names in `held` also get
    their filed documents (see symbol_block)."""
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
        s: symbol_block(s, finnhub, edgar, today, last_cycle_at, unavailable,
                        held=s in held)
        for s in symbols
    }
    if edgar is not None:
        edgar.prune(time.time())
    return blocks
