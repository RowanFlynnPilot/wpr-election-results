"""
Parser test harness.

Builds a fixture PDF from the real 2024 Partisan Primary summary text
(tests/fixture_2024_summary.txt), runs the full pdfplumber -> parser pipeline,
and asserts against the county's actual published numbers. Also tests the
alternate 'name-after-Vote-For' extraction ordering and a synthetic ward page.

Run:
    python -m pip install reportlab   (test-only dependency)
    python tests/test_parse.py

To validate against the REAL 2024 PDFs (recommended before election night),
download them from the county results page and run:
    python tests/test_parse.py --real path/to/2024-partisan-primary.pdf
"""

import argparse
import io
import re
import sys
from pathlib import Path

import pdfplumber
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scraper"))
import parse  # noqa: E402

FIXTURE_TEXT = Path(__file__).parent / "fixture_2024_summary.txt"

CONFIG_2024 = {
    "name": "2024 Partisan Primary",
    "date": "2024-08-13",
    "displayDate": "August 13, 2024",
    "county": "Marathon County",
    "state": "Wisconsin",
}

PASS = 0


def check(label, actual, expected):
    global PASS
    assert actual == expected, f"FAIL {label}: got {actual!r}, expected {expected!r}"
    PASS += 1


def build_pdf(lines: list[str]) -> bytes:
    """Render lines to a PDF, breaking pages at the report's own page footers."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setFont("Helvetica", 9)
    y = 760
    for line in lines:
        if y < 40:
            c.showPage()
            c.setFont("Helvetica", 9)
            y = 760
        c.drawString(36, y, line)
        y -= 12
        if re.search(r"Page \d+ of \d+$", line):
            c.showPage()
            c.setFont("Helvetica", 9)
            y = 760
    c.save()
    return buf.getvalue()


def extract_text(pdf_bytes: bytes) -> list[str]:
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return [p.extract_text() or "" for p in pdf.pages]


def race(data, party, name):
    for r in data["races"]:
        if r["party"] == party and r["name"] == name:
            return r
    raise AssertionError(f"race not found: {party} {name}")


def test_summary_name_first():
    lines = FIXTURE_TEXT.read_text().splitlines()
    pages = extract_text(build_pdf(lines))
    check("classify", parse.classify(pages[0], CONFIG_2024), "summary")
    data = parse.parse_summary("\n".join(pages), CONFIG_2024)

    e, s = data["election"], data["statistics"]
    check("precincts reported", e["precinctsReported"], 88)
    check("precincts total", e["precinctsTotal"], 88)
    check("status", e["status"], "final")
    check("lastUpdated", e["lastUpdated"], "2024-08-13T22:07:00")
    check("ballots cast", s["ballotsCast"], 29342)
    check("ballots DEM", s["ballotsByParty"]["Democratic"], 11825)
    check("ballots REP", s["ballotsByParty"]["Republican"], 17136)
    check("ballots crossover", s["ballotsByParty"]["CrossOver"], 138)
    check("blank", s["blank"], 9)
    check("turnout", s["turnoutPct"], 37.30)

    pp = {c["name"]: c["votes"] for c in data["partyPreference"]}
    check("party pref DEM", pp["Democratic"], 10960)
    check("party pref REP", pp["Republican"], 15093)

    check("race count", len(data["races"]), 62)
    by_party = {}
    for r in data["races"]:
        by_party[r["party"]] = by_party.get(r["party"], 0) + 1
    check("races per party", by_party,
          {"DEM": 12, "REP": 12, "CON": 12, "LIB": 12, "WGN": 12, "REF": 2})

    r = race(data, "DEM", "United States Senator")
    check("Baldwin votes", r["candidates"][0]["votes"], 11264)
    check("Baldwin pct", r["candidates"][0]["pct"], 99.88)
    check("DEM senate writeins", r["writeIns"], 14)
    check("DEM senate total", r["totalVotes"], 11278)
    check("DEM senate undervotes", r["undervotes"], 547)
    check("DEM senate category", r["category"], "U.S. Congress")

    r = race(data, "REP", "United States Senator")
    check("REP senate candidates", [c["name"] for c in r["candidates"]],
          ["Eric Hovde", "Charles E. Barman", "Rejani Raveendran"])
    check("Hovde votes", r["candidates"][0]["votes"], 14178)
    check("REP senate overvotes", r["overvotes"], 8)

    r = race(data, "REP", "Representative to the Assembly District 86")
    check("A86 candidate count", len(r["candidates"]), 3)
    check("A86 leader", r["candidates"][0]["name"], "John Spiros")
    check("A86 precincts", r["precincts"], {"reported": 19, "total": 19})
    check("A86 category", r["category"], "State Legislature")

    r = race(data, "REP", "County Clerk")
    check("county clerk category", r["category"], "County")

    r = race(data, "CON", "State Senator District 12")
    check("zero-vote race total", r["totalVotes"], 0)
    check("zero-vote race writeins", r["writeIns"], 0)

    r = race(data, "DEM", "District Atttorney")  # county's own typo, preserved
    check("DEM DA writein-only", (len(r["candidates"]), r["writeIns"]), (0, 218))

    r = race(data, "REF", "Question 1: Delegation of appropriation power")
    check("Q1 yes", r["candidates"][0]["votes"], 14155)
    check("Q1 category", r["category"], "Referendum")

    print(f"  summary (name-first ordering): {PASS} checks passed")


def test_summary_name_after():
    """Same data, re-ordered the way April's pdfplumber extraction emitted it:
    TOTAL / Vote For N / Race Name / candidates..."""
    lines = FIXTURE_TEXT.read_text().splitlines()
    reordered, i = [], 0
    while i < len(lines):
        if lines[i] == "TOTAL VOTE %" and i + 2 < len(lines):
            reordered.append("TOTAL")
            reordered.append(lines[i + 2])   # Vote For N
            reordered.append(lines[i + 1])   # race name
            i += 3
        else:
            reordered.append(lines[i])
            i += 1
    pages = extract_text(build_pdf(reordered))
    data = parse.parse_summary("\n".join(pages), CONFIG_2024)
    check("name-after race count", len(data["races"]), 62)
    r = race(data, "DEM", "State Senator District 12")  # trailing-digit name case
    check("name-after district race", r["candidates"][0]["votes"], 535)
    r = race(data, "REP", "United States Senator")
    check("name-after Hovde", r["candidates"][0]["votes"], 14178)
    print("  summary (name-after ordering): passed incl. 'District 12' name case")


def test_ward_pages():
    """Synthetic two-ward Precinct Summary in the same block format."""
    ward_pdf_lines = [
        "Precinct Summary - By Ward Detail UNOFFICIAL RESULTS",
        "2024 Partisan Primary",
        "August 13, 2024 Marathon County",
        "City of Wausau Ward 1",
        "Ballots Cast - Total 412",
        "TOTAL VOTE %",
        "REP United States Senator",
        "Vote For 1",
        "Eric Hovde 201 85.17%",
        "Charles E. Barman 20 8.47%",
        "Rejani Raveendran 15 6.36%",
        "Write-In Totals 0 0.00%",
        "Total Votes Cast 236 100.00%",
        "Overvotes 0",
        "Undervotes 12",
        "Precincts Reporting 1 of 1",
        "Election Summary - 08/13/2024 10:07 PM Page 1 of 2",
        "Precinct Summary - By Ward Detail UNOFFICIAL RESULTS",
        "2024 Partisan Primary",
        "August 13, 2024 Marathon County",
        "Town of Rib Mountain Wards 1-2",
        "Ballots Cast - Total 388",
        "TOTAL VOTE %",
        "REP United States Senator",
        "Vote For 1",
        "Eric Hovde 240 87.59%",
        "Charles E. Barman 19 6.93%",
        "Rejani Raveendran 15 5.47%",
        "Write-In Totals 0 0.00%",
        "Total Votes Cast 274 100.00%",
        "Overvotes 0",
        "Undervotes 9",
        "Precincts Reporting 1 of 1",
        "Election Summary - 08/13/2024 10:07 PM Page 2 of 2",
    ]
    pages = extract_text(build_pdf(ward_pdf_lines))
    check("classify ward", parse.classify(pages[0], CONFIG_2024), "ward")

    summary_lines = FIXTURE_TEXT.read_text().splitlines()
    summary_pages = extract_text(build_pdf(summary_lines))
    data = parse.parse_summary("\n".join(summary_pages), CONFIG_2024)

    parse.parse_ward_pages(pages, CONFIG_2024, data["races"])
    r = race(data, "REP", "United States Senator")
    check("ward count", len(r["wards"]), 2)
    check("ward 1 name", r["wards"][0]["ward"], "City of Wausau Ward 1")
    check("ward 1 Hovde", r["wards"][0]["candidates"]["Eric Hovde"], 201)
    check("ward 2 ballots", r["wards"][1]["ballotsCast"], 388)
    print("  ward detail merge: passed")


def test_real(paths: list[str]):
    """Validate against real downloaded 2024 PDFs, entirely in memory.
    Pass the summary PDF, or the summary AND ward PDFs together (any order)."""
    parsed = []
    for path in paths:
        pages = extract_text(Path(path).read_bytes())
        kind = parse.classify(pages[0], CONFIG_2024)
        print(f"  {Path(path).name}: classified as [{kind}]")
        parsed.append((kind, pages))

    summaries = [p for k, p in parsed if k == "summary"]
    if not summaries:
        print("  include the 2024 Election Summary PDF -- ward validation needs it")
        return
    data = parse.parse_summary("\n".join(summaries[0]), CONFIG_2024)
    print(f"  summary: {len(data['races'])} races, "
          f"{data['statistics']['ballotsCast']:,} ballots cast, "
          f"precincts {data['election']['precinctsReported']}/{data['election']['precinctsTotal']}")
    for r in data["races"][:5]:
        lead = r["candidates"][0]["name"] if r["candidates"] else "(write-ins only)"
        print(f"    [{r['party']}] {r['name']}: {lead}")

    for kind, pages in parsed:
        if kind == "ward":
            parse.parse_ward_pages(pages, CONFIG_2024, data["races"])
            with_wards = sum(1 for r in data["races"] if r["wards"])
            n_wards = len(data["races"][0]["wards"]) if with_wards else 0
            print(f"  ward detail: merged into {with_wards} races "
                  f"(~{n_wards} wards on the first race)")
    print("  real-PDF validation complete -- eyeball the numbers above against the PDFs")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", nargs="+", help="Real 2024 Partisan Primary PDF(s): summary, optionally + ward detail")
    args = ap.parse_args()
    if args.real:
        test_real(args.real)
    else:
        test_summary_name_first()
        test_summary_name_after()
        test_ward_pages()
        print("\nAll parser tests passed.")
