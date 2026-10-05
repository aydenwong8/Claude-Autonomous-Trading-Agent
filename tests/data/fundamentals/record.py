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
