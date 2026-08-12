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
        if (i + 2 < len(lines) and re.match(r"^Vote For \d+$", lines[i + 1])
                and lines[i + 2] == "TOTAL VOTE %"):
            reordered.append("TOTAL")
            reordered.append(lines[i + 1])   # Vote For N
            reordered.append(lines[i])       # race name
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


def test_summary_marker_before_name():
    """Third observed ordering (web extractors): TOTAL VOTE % / Name / Vote For."""
    lines = FIXTURE_TEXT.read_text().splitlines()
    reordered, i = [], 0
    while i < len(lines):
        if (i + 2 < len(lines) and re.match(r"^Vote For \d+$", lines[i + 1])
                and lines[i + 2] == "TOTAL VOTE %"):
            reordered.append(lines[i + 2])   # marker first
            reordered.append(lines[i])       # race name
            reordered.append(lines[i + 1])   # Vote For N
            i += 3
        else:
            reordered.append(lines[i])
            i += 1
    pages = extract_text(build_pdf(reordered))
    data = parse.parse_summary("\n".join(pages), CONFIG_2024)
    check("marker-first race count", len(data["races"]), 62)
    r = race(data, "DEM", "United States Senator")
    check("marker-first Baldwin", r["candidates"][0]["votes"], 11264)
    print("  summary (marker-before-name ordering): passed")


def test_summary_zero_state():
    """Pre-results summary, exactly as the county published it on election
    day 2026 ('2026 Fall Primary'): every count is zero and the tabulator
    omits the 'Total Votes Cast' line from every block, including Party
    Preference. Must parse with totals of 0 -- and must still refuse a
    block that has votes but no stated total."""
    cfg = dict(CONFIG_2024, name="2026 Fall Primary")
    zero = [
        "Summary Results Report UNOFFICIAL RESULTS",
        "2026 Fall Primary",
        "August 11, 2026 Marathon County",
        "Statistics TOTAL",
        "Precincts Complete 0 of 88",
        "Ballots Cast - Total 0",
        "Ballots Cast - Blank 0",
        "Voter Turnout - Total 0.00%",
        "Party Preference Section",
        "Vote For 1",
        "TOTAL VOTE %",
        "Republican 0",
        "Democratic 0",
        "Overvotes 0",
        "Undervotes 0",
        "Precincts Reporting 0 of 88",
        "REP Governor",
        "Vote For 1",
        "TOTAL VOTE %",
        "Tom Tiffany 0",
        "Andy Manske 0",
        "Write-In Totals 0",
        "Overvotes 0",
        "Undervotes 0",
        "Precincts Reporting 0 of 88",
    ]
    pages = extract_text(build_pdf(zero))
    check("zero-state classify", parse.classify(pages[0], cfg), "summary")
    data = parse.parse_summary("\n".join(pages), cfg)
    check("zero-state status", data["election"]["status"], "live")
    check("zero-state race count", len(data["races"]), 1)
    check("zero-state total", data["races"][0]["totalVotes"], 0)
    check("zero-state precincts", data["races"][0]["precincts"], {"reported": 0, "total": 88})
    check("zero-state party pref", [p["name"] for p in data["partyPreference"]],
          ["Republican", "Democratic"])

    # fail-fast must survive: votes present but no 'Total Votes Cast' line
    bad = list(zero)
    bad[bad.index("Tom Tiffany 0")] = "Tom Tiffany 4,123 61.00%"
    try:
        parse.parse_summary("\n".join(extract_text(build_pdf(bad))), cfg)
        raise AssertionError("votes without 'Total Votes Cast' must raise")
    except ValueError as e:
        if "no 'Total Votes Cast'" not in str(e):
            raise
    print("  summary (zero-state, missing Total Votes Cast): passed incl. fail-fast")


def test_ward_pages():
    """Ward detail in its true condensed shape (verified against the real
    2024 PDF): no Total/Over/Under/Precincts lines, a keyword-less Party
    Preference block, ward name repeated as a page header, a race block
    split across a page break, and a district race name that looks like a
    candidate line."""
    ward_pdf_lines = [
        "Summary Results Report UNOFFICIAL RESULTS",
        "2024 Partisan Primary",
        "August 13, 2024 Marathon County",
        "BERGEN T WD 1",
        "Statistics TOTAL",
        "Registered Voters - Total 521",
        "Ballots Cast - Total 219",
        "Ballots Cast - Democratic 78",
        "Ballots Cast - Republican 137",
        "Voter Turnout - Total 42.03%",
        "Party Preference Section",
        "Vote For 1",
        "TOTAL VOTE %",
        "Democratic 73 38.42%",
        "Republican 115 60.53%",
        "REP United States Senator",
        "Vote For 1",
        "TOTAL VOTE %",
        "Eric Hovde 107 82.31%",
        "Charles E. Barman 12 9.23%",
        # page break mid-block: headers + repeated ward name, then the rest
        "Precinct Summary - 08/13/2024 10:08PM Page 1 of 3",
        "Summary Results Report UNOFFICIAL RESULTS",
        "2024 Partisan Primary",
        "August 13, 2024 Marathon County",
        "BERGEN T WD 1",
        "Rejani Raveendran 11 8.46%",
        "Write-In Totals 0 0.00%",
        "REP State Senator District 12",
        "Vote For 1",
        "TOTAL VOTE %",
        "Mary Felzkowski 88 100.00%",
        "Write-In Totals 0",
        "CON District Attorney",
        "Vote For 1",
        "TOTAL VOTE %",
        "Write-In Totals 0",
        "Precinct Summary - 08/13/2024 10:08PM Page 2 of 3",
        "Summary Results Report UNOFFICIAL RESULTS",
        "2024 Partisan Primary",
        "August 13, 2024 Marathon County",
        "WAUSAU C WD 1",
        "Statistics TOTAL",
        "Registered Voters - Total 902",
        "Ballots Cast - Total 388",
        "Voter Turnout - Total 43.01%",
        "Party Preference Section",
        "Vote For 1",
        "TOTAL VOTE %",
        "Democratic 240 63.16%",
        "Republican 140 36.84%",
        "REP United States Senator",
        "Vote For 1",
        "TOTAL VOTE %",
        "Eric Hovde 110 87.30%",
        "Charles E. Barman 9 7.14%",
        "Rejani Raveendran 7 5.56%",
        "Write-In Totals 0 0.00%",
        "Precinct Summary - 08/13/2024 10:08PM Page 3 of 3",
    ]
    pages = extract_text(build_pdf(ward_pdf_lines))
    check("classify ward", parse.classify(pages[0], CONFIG_2024), "ward")

    summary_lines = FIXTURE_TEXT.read_text().splitlines()
    summary_pages = extract_text(build_pdf(summary_lines))
    data = parse.parse_summary("\n".join(summary_pages), CONFIG_2024)

    parse.parse_ward_pages(pages, CONFIG_2024, data["races"])
    r = race(data, "REP", "United States Senator")
    check("ward count", len(r["wards"]), 2)
    check("ward 1 name", r["wards"][0]["ward"], "BERGEN T WD 1")
    check("split-block Hovde", r["wards"][0]["candidates"]["Eric Hovde"], 107)
    check("split-block Raveendran", r["wards"][0]["candidates"]["Rejani Raveendran"], 11)
    check("ward 1 ballots", r["wards"][0]["ballotsCast"], 219)
    check("ward 2 ballots", r["wards"][1]["ballotsCast"], 388)
    d12 = race(data, "REP", "State Senator District 12")
    check("district-name-as-candidate trap", d12["wards"][0]["candidates"]["Mary Felzkowski"], 88)
    con = race(data, "CON", "District Attorney")
    check("zero-vote ward block", (con["wards"][0]["candidates"], con["wards"][0]["writeIns"]), ({}, 0))
    print("  ward detail merge: passed (condensed format, split block, district trap)")


def test_link_discovery():
    """Discovery must survive attribute-order changes and decoy links."""
    import fetch
    html = """
    <html><body>
    <a href="/services/elections-voting">Elections &amp; Voting</a>
    <h2>Available Reports</h2>
    <a class="doc" href="/home/showpublisheddocument/18979/639213411072400000">Election Summary</a>
    <a href="https://www.marathoncounty.gov/home/showpublisheddocument/18983/639213411078830000" target="_blank"><span>Precinct Summary</span></a>
    <a target="_blank" href="/home/showpublisheddocument/18981/639213411076500000">
      Precincts Reported/Not Reported
    </a>
    <h3>Historical Reports</h3>
    <a href="/home/showpublisheddocument/13529/638591837723970000">2024 Partisan Primary</a>
    <a href="/home/showpublisheddocument/13531/638591837949400000">By Ward Detail</a>
    </body></html>
    """
    links = fetch.discover_links(html, "https://www.marathoncounty.gov/services/elections-voting/results")
    check("discovery slots", sorted(links.keys()),
          ["electionSummary", "precinctStatus", "precinctSummary"])
    check("discovery absolute url", links["electionSummary"],
          "https://www.marathoncounty.gov/home/showpublisheddocument/18979/639213411072400000")
    check("discovery nested-tag anchor", links["precinctSummary"],
          "https://www.marathoncounty.gov/home/showpublisheddocument/18983/639213411078830000")
    try:
        fetch.discover_links("<a href='/x'>Nothing relevant</a>", "https://example.com")
        raise AssertionError("FAIL: discovery accepted a page with no report links")
    except fetch.FetchError:
        pass
    print("  link discovery: passed incl. decoys, nesting, relative URLs")


def test_bytes_ingest(tmp_output=Path("/tmp/wpr_test_results.json")):
    """process_pdf must accept raw bytes (the automated fetch path)."""
    if tmp_output.exists():
        tmp_output.unlink()
    original = parse.OUTPUT_PATH
    parse.OUTPUT_PATH = tmp_output
    try:
        pdf_bytes = build_pdf(FIXTURE_TEXT.read_text().splitlines())
        kind = parse.process_pdf(pdf_bytes, CONFIG_2024)
        check("bytes ingest kind", kind, "summary")
        import json
        data = json.loads(tmp_output.read_text())
        check("bytes ingest races", len(data["races"]), 62)
    finally:
        parse.OUTPUT_PATH = original
        if tmp_output.exists():
            tmp_output.unlink()
    print("  bytes ingest (auto-fetch path): passed")


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
            print(f"  ward detail: merged into {with_wards} of {len(data['races'])} races")
            mismatches = []
            for r in data["races"]:
                if not r["wards"]:
                    continue
                ward_sum = sum(sum(w["candidates"].values()) + w["writeIns"] for w in r["wards"])
                if ward_sum != r["totalVotes"]:
                    mismatches.append(f"{r['rawName']}: wards={ward_sum} vs summary={r['totalVotes']}")
            if mismatches:
                print("  CROSS-CHECK FAILED for " + str(len(mismatches)) + " races:")
                for m in mismatches[:8]:
                    print("    " + m)
                sys.exit(1)
            rep_sen = race(data, "REP", "United States Senator")
            print(f"  cross-check: every race's ward votes sum exactly to its summary total")
            print(f"  (REP U.S. Senator: {len(rep_sen['wards'])} wards, "
                  f"{sum(sum(w['candidates'].values()) + w['writeIns'] for w in rep_sen['wards']):,} votes)")
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
        test_summary_marker_before_name()
        test_summary_zero_state()
        test_ward_pages()
        test_link_discovery()
        test_bytes_ingest()
        print("\nAll parser tests passed.")
