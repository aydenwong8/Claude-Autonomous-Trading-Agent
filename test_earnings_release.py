"""The earnings release (8-K item 2.02, exhibit 99.1) attached to holdings.

Before this the agent saw EPS against the estimate and a few headlines, never
the release itself: on 2026-10-13 it could not tell whether JNJ's MedTech was
stalling (a written invalidation) or what drove JPM's miss, though both
releases state it on their first page. No network: JNJ's real 13 Oct exhibit
and filing index are recorded in tests/data/fundamentals/.
"""

import json
from datetime import date, datetime, timezone
from pathlib import Path

import config
import fundamentals as f

FIXTURES = Path(__file__).parent / "tests" / "data" / "fundamentals"
TODAY = date(2026, 10, 14)
LAST_CYCLE = datetime(2026, 10, 13, 20, 30, tzinfo=timezone.utc)
JNJ_CIK = 200406
JNJ_RELEASE = "0000200406-26-000189"


def submissions(*rows):
    """A submissions payload from (form, filed, items, accession) rows, newest first."""
    keys = ("form", "filingDate", "items", "accessionNumber")
    recent = {k: [r[i] for r in rows] for i, k in enumerate(keys)}
    recent["acceptanceDateTime"] = [f"{r[1]}T12:00:00.000Z" for r in rows]
    return {"filings": {"recent": recent}}


JNJ_SUBMISSIONS = submissions(
    ("8-K", "2026-10-13", "2.02,9.01", JNJ_RELEASE),
    ("10-Q", "2026-07-30", "", "0000200406-26-000150"),
)


class FakeSec:
    """JSON and HTML getters over the JNJ fixtures; records every URL."""

    def __init__(self, fail_text=False):
        self.urls = []
        self.fail_text = fail_text

    def json(self, url, headers, limiter):
        self.urls.append(url)
        if url.endswith("index.json"):
            return json.loads((FIXTURES / f"edgar_index_JNJ_{JNJ_RELEASE}.json").read_text())
        raise AssertionError(f"unexpected JSON fetch {url}")

    def text(self, url, headers, limiter):
        self.urls.append(url)
        if self.fail_text:
            raise f.LookupFailed("HTTP 503")
        assert url.endswith("a2026q3exhibit991.htm"), url
        return (FIXTURES / "edgar_release_JNJ_ex991.htm").read_text(encoding="utf-8")


def edgar(tmp_path, sec):
    limiter = f.RateLimiter(1000, 1.0, clock=lambda: 0.0, sleep=lambda s: None)
    return f.EdgarClient("UA", limiter, tmp_path, TODAY, get=sec.json, get_text=sec.text)


# --- html_to_text --------------------------------------------------------------


def test_a_table_row_is_one_line_without_empty_cells():
    html = ("<table><tr><td>MedTech</td><td>&nbsp;</td><td>8,642</td>"
            "<td></td><td>2.5</td></tr><tr><td>Worldwide</td><td>$25,399</td></tr></table>")
    assert f.html_to_text(html) == "MedTech | 8,642 | 2.5\nWorldwide | $25,399"


def test_paragraphs_split_and_scripts_are_dropped():
    html = ("<style>p{color:red}</style><p>Sales   grew\n 5.9%.</p>"
            "<div>Guidance raised.</div><script>x=1</script>")
    assert f.html_to_text(html) == "Sales grew 5.9%.\nGuidance raised."


# --- finding the release ---------------------------------------------------------


def test_newest_results_8k_within_the_window():
    subs = submissions(
        ("8-K", "2026-10-13", "7.01,9.01", "acc-fd"),      # Reg FD only: not results
        ("8-K", "2026-10-13", "2.02,9.01", "acc-results"),
        ("8-K", "2026-07-14", "2.02,9.01", "acc-july"),
    )
    assert f.latest_earnings_release(subs, TODAY)["accession"] == "acc-results"


def test_a_release_older_than_the_window_is_not_shown():
    filed = TODAY.toordinal() - config.EARNINGS_RELEASE_DAYS - 1
    subs = submissions(("8-K", date.fromordinal(filed).isoformat(), "2.02", "acc-old"))
    assert f.latest_earnings_release(subs, TODAY) is None


def test_exhibit_991_is_chosen_over_the_supplement():
    index = {"directory": {"item": [{"name": n} for n in (
        "jpm-20261013.htm", "a3q26erfex992supplement.htm",
        "a3q26erfexhibit991narrative.htm", "R1.htm")]}}
    assert f.release_exhibit(index) == "a3q26erfexhibit991narrative.htm"
    index = {"directory": {"item": [{"name": n} for n in (
        "jnj-20261013.htm", "a2026q3exhibit991.htm", "a2026q3exhibit992.htm")]}}
    assert f.release_exhibit(index) == "a2026q3exhibit991.htm"
    assert f.release_exhibit({"directory": {"item": [{"name": "x-8k.htm"}]}}) is None


# --- the block -------------------------------------------------------------------


def test_jnj_block_carries_the_segment_table_within_the_cap(tmp_path):
    block = f.earnings_release_block(edgar(tmp_path, FakeSec()), JNJ_CIK, JNJ_SUBMISSIONS,
                                     TODAY, LAST_CYCLE)
    assert block["source"] == (
        "8-K filed 2026-10-13, item 2.02, exhibit a2026q3exhibit991.htm"
    )
    assert "MedTech | 8,642 | 8,430 | 2.5" in block["text"]
    assert len(block["text"]) <= config.EARNINGS_RELEASE_MAX_CHARS
    assert block["truncated"].startswith(f"first {len(block['text'])} of ")


def test_new_since_last_cycle_is_marked(tmp_path):
    before = datetime(2026, 10, 12, 20, 30, tzinfo=timezone.utc)
    block = f.earnings_release_block(edgar(tmp_path, FakeSec()), JNJ_CIK, JNJ_SUBMISSIONS,
                                     TODAY, before)
    assert block["source"].endswith(" [new since your last cycle]")


def test_document_is_fetched_once_then_cached(tmp_path):
    sec = FakeSec()
    for _ in range(2):
        f.earnings_release_block(edgar(tmp_path, sec), JNJ_CIK, JNJ_SUBMISSIONS,
                                 TODAY, LAST_CYCLE)
    assert len(sec.urls) == 2  # index + exhibit, first time only


def test_no_release_in_the_window_fetches_nothing(tmp_path):
    sec = FakeSec()
    subs = submissions(("10-Q", "2026-07-30", "", "acc-q"))
    assert f.earnings_release_block(edgar(tmp_path, sec), JNJ_CIK, subs,
                                    TODAY, LAST_CYCLE) is None
    assert sec.urls == []


# --- wiring into symbol_block ------------------------------------------------------


class JnjOnly:
    """EdgarClient stand-in at the symbol_block level: JNJ with a fresh release."""

    def __init__(self, tmp_path, sec):
        self.inner = edgar(tmp_path, sec)
        self.facts_calls = 0

    def cik(self, symbol):
        return JNJ_CIK

    def submissions(self, cik):
        return JNJ_SUBMISSIONS

    def facts(self, cik, accession):
        return {"facts": {}}

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_release_attaches_only_when_asked_for(tmp_path):
    client = JnjOnly(tmp_path, FakeSec())
    held = f.symbol_block("JNJ", None, client, TODAY, LAST_CYCLE, {}, with_release=True)
    candidate = f.symbol_block("JNJ", None, client, TODAY, LAST_CYCLE, {})
    assert "MedTech" in held["earnings_release"]["text"]
    assert "earnings_release" not in candidate


def test_a_failed_release_fetch_keeps_the_filings(tmp_path):
    client = JnjOnly(tmp_path, FakeSec(fail_text=True))
    block = f.symbol_block("JNJ", None, client, TODAY, LAST_CYCLE, {}, with_release=True)
    assert block["lookup_failed"] == {"earnings_release": "HTTP 503"}
    assert block["material_filings_90d"][0].startswith("2026-10-13 8-K: 2.02")
    assert "earnings_release" not in block
