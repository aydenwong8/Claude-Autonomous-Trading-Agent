"""The item text of a holding's recent 8-Ks, beside the one-line filing list.

Before this the agent saw "8-K: 1.01 Material definitive agreement; 2.03 New
material debt obligation" and nothing more. It carried IQV's 23 Sep 8-K as an
open question for three weeks against an invalidation naming a debt-funded
acquisition; the filing itself says the $2B of notes refinance existing debt.
Fixtures are the real IQV (23 Sep) and ETN (5 Oct) 8-Ks.
"""

from datetime import date, datetime, timezone
from pathlib import Path

import config
import fundamentals as f

FIXTURES = Path(__file__).parent / "tests" / "data" / "fundamentals"
TODAY = date(2026, 10, 14)
LAST_CYCLE = datetime(2026, 10, 13, 20, 30, tzinfo=timezone.utc)
IQV_DOC = "d128109d8k.htm"
ETN_DOC = "ef20083229_8k.htm"


def text_of(name):
    return f.html_to_text((FIXTURES / name).read_text(encoding="utf-8"))


def submissions(*rows):
    """(form, filed, items, accession, primary document) rows, newest first."""
    keys = ("form", "filingDate", "items", "accessionNumber", "primaryDocument")
    recent = {k: [r[i] for r in rows] for i, k in enumerate(keys)}
    recent["acceptanceDateTime"] = [f"{r[1]}T12:00:00.000Z" for r in rows]
    return {"filings": {"recent": recent}}


# --- the item body ----------------------------------------------------------------


def test_body_drops_the_cover_page_exhibit_list_and_signature():
    body = f.eight_k_body(text_of(f"edgar_8k_IQV_{IQV_DOC}"))
    assert body.startswith("Item 1.01 | Entry into a Material Definitive Agreement")
    assert "redeem in full the Issuer’s Senior 5.000% Notes due 2026" in body
    assert body.rstrip().endswith("is incorporated by reference into this Item 2.03.")
    for gone in ("Commission File Number", "Item 9.01", "SIGNATURE", "/s/"):
        assert gone not in body


def test_officer_change_body_is_kept_whole_under_the_cap():
    body = f.eight_k_body(text_of(f"edgar_8k_ETN_{ETN_DOC}"))
    assert body.startswith("Item 5.02 | Departure of Directors")
    assert "Andrea Trabacchin was appointed" in body
    assert len(body) <= config.EIGHT_K_MAX_CHARS


def test_a_document_without_item_headings_is_kept_from_the_top():
    assert f.eight_k_body("Some company\nA short notice.") == "Some company\nA short notice."


# --- which 8-Ks -------------------------------------------------------------------


def test_recent_non_results_8ks_newest_first_capped():
    subs = submissions(
        ("8-K", "2026-10-13", "2.02,9.01", "acc-results", "r.htm"),   # the release covers it
        ("10-Q", "2026-10-10", "", "acc-q", "q.htm"),
        ("8-K", "2026-10-05", "5.02", "acc-a", "a.htm"),
        ("8-K/A", "2026-10-01", "5.02", "acc-b", "b.htm"),
        ("8-K", "2026-09-30", "8.01,9.01", "acc-c", "c.htm"),
        ("8-K", "2026-09-29", "1.01", "acc-d", "d.htm"),             # over the count cap
    )
    picked = f.recent_8ks(subs, TODAY)
    assert [p["accession"] for p in picked] == ["acc-a", "acc-b", "acc-c"]
    assert len(picked) == config.EIGHT_K_TEXT_MAX_FILINGS


def test_8ks_older_than_the_window_are_left_to_the_filing_list():
    old = date.fromordinal(TODAY.toordinal() - config.EIGHT_K_TEXT_DAYS - 1).isoformat()
    assert f.recent_8ks(submissions(("8-K", old, "1.01", "acc", "x.htm")), TODAY) == []


# --- the block --------------------------------------------------------------------


class Docs:
    """Serves the recorded 8-K documents by file name; records what was fetched."""

    def __init__(self, fail=()):
        self.fetched = []
        self.fail = set(fail)

    def __call__(self, url, headers, limiter):
        name = url.rsplit("/", 1)[1]
        self.fetched.append(name)
        if name in self.fail:
            raise f.LookupFailed("HTTP 503")
        prefix = "IQV" if name == IQV_DOC else "ETN"
        return (FIXTURES / f"edgar_8k_{prefix}_{name}").read_text(encoding="utf-8")


SUBS = submissions(
    ("8-K", "2026-10-05", "5.02", "0001140361-26-038523", ETN_DOC),
    ("8-K", "2026-09-23", "1.01,2.03,9.01", "0001193125-26-399616", IQV_DOC),
)


def edgar(tmp_path, docs):
    limiter = f.RateLimiter(1000, 1.0, clock=lambda: 0.0, sleep=lambda s: None)
    return f.EdgarClient("UA", limiter, tmp_path, TODAY, get=None, get_text=docs)


def test_block_lines_match_the_filing_list(tmp_path):
    entries = f.eight_k_text_block(edgar(tmp_path, Docs()), 1, SUBS, TODAY, LAST_CYCLE)
    assert [e["filing"] for e in entries] == [
        "2026-10-05 8-K: 5.02 Officer or director change",
        "2026-09-23 8-K: 1.01 Material definitive agreement; 2.03 New material debt obligation",
    ]
    assert "Andrea Trabacchin was appointed" in entries[0]["text"]
    assert "redeem in full" in entries[1]["text"]


def test_long_bodies_are_cut_and_say_so(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EIGHT_K_MAX_CHARS", 600)
    (entry, _) = f.eight_k_text_block(edgar(tmp_path, Docs()), 1, SUBS, TODAY, LAST_CYCLE)
    assert len(entry["text"]) <= 600
    assert entry["truncated"].startswith(f"first {len(entry['text'])} of ")


def test_one_unreadable_8k_is_flagged_and_the_other_kept(tmp_path):
    entries = f.eight_k_text_block(edgar(tmp_path, Docs(fail={ETN_DOC})), 1, SUBS,
                                   TODAY, LAST_CYCLE)
    assert entries[0] == {"filing": "2026-10-05 8-K: 5.02 Officer or director change",
                          "lookup_failed": "HTTP 503"}
    assert "redeem in full" in entries[1]["text"]


def test_documents_are_cached(tmp_path):
    docs = Docs()
    for _ in range(2):
        f.eight_k_text_block(edgar(tmp_path, docs), 1, SUBS, TODAY, LAST_CYCLE)
    assert docs.fetched == [ETN_DOC, IQV_DOC]


class Holding:
    """EdgarClient stand-in at the symbol_block level."""

    def __init__(self, tmp_path, docs):
        self.inner = edgar(tmp_path, docs)

    def cik(self, symbol):
        return 1

    def submissions(self, cik):
        return SUBS

    def facts(self, cik, accession):
        return {"facts": {}}

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_only_holdings_get_8k_text(tmp_path):
    client = Holding(tmp_path, Docs())
    held = f.symbol_block("IQV", None, client, TODAY, LAST_CYCLE, {}, held=True)
    other = f.symbol_block("IQV", None, client, TODAY, LAST_CYCLE, {})
    assert len(held["recent_8k_text"]) == 2
    assert "recent_8k_text" not in other
