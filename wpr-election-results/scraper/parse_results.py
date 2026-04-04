"""
Marathon County Election Results PDF Scraper
Wausau Pilot & Review

Fetches election results PDFs from the Marathon County Clerk's website,
parses them into structured JSON, and writes to public/data/election.json.

Usage:
    python scraper/parse_results.py
    python scraper/parse_results.py --pdf path/to/local.pdf
    python scraper/parse_results.py --url https://marathoncounty.gov/...pdf

The scraper handles two PDF formats published by Marathon County:
  1. Election Summary — countywide totals per race
  2. Precinct Summary — ward-by-ward breakdowns
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import pdfplumber
except ImportError:
    print("ERROR: pdfplumber not installed. Run: pip install pdfplumber")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("ERROR: requests not installed. Run: pip install requests")
    sys.exit(1)


# ── CONFIGURATION ────────────────────────────────────────────────
# Update these URLs when Marathon County publishes new results.
# They typically appear at:
# https://www.marathoncounty.gov/services/elections-voting/results
#
# The page shows links like "Election Summary" and "Precinct Summary"
# which point to PDF documents. Copy those URLs here.

RESULTS_PAGE_URL = "https://www.marathoncounty.gov/services/elections-voting/results"

# Direct PDF URLs — update these on election night when links appear
SUMMARY_PDF_URL = ""   # Election Summary PDF
PRECINCT_PDF_URL = ""  # Precinct Summary (By Ward Detail) PDF

# Election metadata — update per election
ELECTION_CONFIG = {
    "name": "2026 Spring Election",
    "date": "2026-04-07",
    "displayDate": "April 7, 2026",
    "county": "Marathon County",
    "state": "Wisconsin",
}

OUTPUT_PATH = Path(__file__).parent.parent / "public" / "data" / "election.json"


# ── CATEGORY DETECTION ───────────────────────────────────────────

def detect_category(race_name: str) -> str:
    """Infer race category from the race title in the PDF."""
    name_lower = race_name.lower()
    if any(k in name_lower for k in ["supreme court", "circuit court", "court of appeals", "judge"]):
        return "judicial"
    if any(k in name_lower for k in ["school board", "school district"]):
        return "school"
    if any(k in name_lower for k in ["county board", "county supervisor", "supervisor"]):
        return "county"
    if any(k in name_lower for k in ["referendum", "question", "amendment"]):
        return "referendum"
    # Municipal: alderperson, mayor, city council, village trustee, etc.
    return "municipal"


def detect_seats(race_text: str) -> int:
    """Extract 'Vote For N' from the race header."""
    m = re.search(r"Vote For\s+(\d+)", race_text, re.IGNORECASE)
    return int(m.group(1)) if m else 1


def slugify(text: str) -> str:
    """Create a URL-safe ID from a race name."""
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s-]+", "-", text)
    return text[:60]


# ── PDF PARSING ──────────────────────────────────────────────────

def fetch_pdf(url: str) -> bytes:
    """Download a PDF from a URL."""
    print(f"  Fetching: {url}")
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.content


def parse_summary_pdf(pdf_path_or_bytes) -> dict:
    """
    Parse the Election Summary PDF into structured data.

    Marathon County's summary PDF format:
    - Page header: "Summary Results Report UNOFFICIAL RESULTS"
    - Election name & date
    - Statistics block (Registered Voters, Ballots Cast, etc.)
    - Race blocks with "Vote For N", candidate names, and vote counts
    """
    if isinstance(pdf_path_or_bytes, (str, Path)):
        pdf = pdfplumber.open(pdf_path_or_bytes)
    else:
        import io
        pdf = pdfplumber.open(io.BytesIO(pdf_path_or_bytes))

    full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    pdf.close()

    # Extract statistics
    stats = {
        "registeredVoters": extract_number(full_text, r"Registered Voters\s*[-–]\s*Total\s+([\d,]+)"),
        "ballotsCast": extract_number(full_text, r"Ballots Cast\s*[-–]\s*Total\s+([\d,]+)"),
        "blanks": extract_number(full_text, r"Ballots Cast\s*[-–]\s*Blank\s+([\d,]+)"),
        "turnoutPct": extract_float(full_text, r"Voter Turnout\s*[-–]\s*Total\s+([\d.]+)%"),
    }

    # Extract precincts reported
    precincts_match = re.search(r"Precincts Complete\s+(\d+)\s+of\s+(\d+)", full_text)
    precincts_reported = int(precincts_match.group(1)) if precincts_match else 0
    precincts_total = int(precincts_match.group(2)) if precincts_match else 0

    # Determine status
    status = "final" if precincts_reported == precincts_total and precincts_total > 0 else "live"

    # Extract timestamp from PDF footer
    time_match = re.search(r"(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}[AP]M)", full_text)
    if time_match:
        try:
            last_updated = datetime.strptime(time_match.group(1), "%m/%d/%Y %I:%M%p")
            last_updated_iso = last_updated.isoformat()
        except ValueError:
            last_updated_iso = datetime.now(timezone.utc).isoformat()
    else:
        last_updated_iso = datetime.now(timezone.utc).isoformat()

    # Parse races
    races = parse_races_from_text(full_text, precincts_reported, precincts_total)

    return {
        "election": {
            **ELECTION_CONFIG,
            "status": status,
            "lastUpdated": last_updated_iso,
            "precinctsReported": precincts_reported,
            "precinctsTotal": precincts_total,
        },
        "statistics": stats,
        "races": races,
    }


def parse_races_from_text(text: str, precincts_reported: int, precincts_total: int) -> list:
    """
    Extract individual races from the summary PDF text.

    Each race block looks like:
        TOTAL
        Vote For 1
        Justice of the Supreme Court
        Chris Taylor  12345
        Maria S. Lazar  6789
        Write-In Totals  12
    """
    races = []

    # Split on "TOTAL\nVote For" pattern
    # This regex captures each race block
    blocks = re.split(r"(?=TOTAL\s*\nVote For\s+\d+)", text)

    for block in blocks:
        if "Vote For" not in block:
            continue

        lines = [l.strip() for l in block.strip().split("\n") if l.strip()]

        # Extract Vote For N
        seats = 1
        race_name_start = 0
        for i, line in enumerate(lines):
            m = re.match(r"Vote For\s+(\d+)", line)
            if m:
                seats = int(m.group(1))
                race_name_start = i + 1
                break

        if race_name_start >= len(lines):
            continue

        # Race name is the line after "Vote For N"
        race_name = lines[race_name_start]

        # Candidates are lines with a name followed by a number
        candidates = []
        write_ins = 0
        for line in lines[race_name_start + 1:]:
            # Skip page footers
            if "Election Summary" in line or "Page " in line or "Summary Results" in line:
                break

            # Check for "Write-In Totals  N"
            wm = re.match(r"Write-In Totals\s+([\d,]+)", line)
            if wm:
                write_ins = parse_int(wm.group(1))
                continue

            # Candidate line: "Name  Votes"
            cm = re.match(r"(.+?)\s{2,}([\d,]+)\s*$", line)
            if cm:
                candidates.append({
                    "name": cm.group(1).strip(),
                    "votes": parse_int(cm.group(2)),
                })

        if not candidates:
            continue

        race_id = slugify(race_name)
        category = detect_category(race_name)

        races.append({
            "id": race_id,
            "name": race_name,
            "type": "general",
            "seats": seats,
            "jurisdiction": ELECTION_CONFIG["county"],
            "category": category,
            "candidates": sorted(candidates, key=lambda c: c["votes"], reverse=True),
            "writeIns": write_ins,
            "precincts": {
                "reported": precincts_reported,
                "total": precincts_total,
            },
            "wardData": [],  # Populated from precinct PDF
        })

    return races


def parse_precinct_pdf(pdf_path_or_bytes, races: list) -> list:
    """
    Parse the Precinct Summary (By Ward Detail) PDF and merge ward data
    into the existing races list.

    Each page covers one precinct/ward with format:
        WARD_NAME
        Summary Results Report UNOFFICIAL RESULTS
        ...
        Statistics  TOTAL
        Registered Voters - Total  N
        Ballots Cast - Total  N
        ...
        Vote For N
        Race Name
        Candidate  Votes
        ...
    """
    if isinstance(pdf_path_or_bytes, (str, Path)):
        pdf = pdfplumber.open(pdf_path_or_bytes)
    else:
        import io
        pdf = pdfplumber.open(io.BytesIO(pdf_path_or_bytes))

    # Build a lookup by race name
    race_lookup = {r["name"].lower(): r for r in races}

    for page in pdf.pages:
        text = page.extract_text() or ""
        lines = [l.strip() for l in text.split("\n") if l.strip()]

        if not lines:
            continue

        # First line is the ward name
        ward_name = lines[0]

        # Skip if it's a header line
        if "Summary Results" in ward_name or "UNOFFICIAL" in ward_name:
            continue

        # Extract ward statistics
        registered = extract_number(text, r"Registered Voters\s*[-–]\s*Total\s+([\d,]+)")
        ballots_cast = extract_number(text, r"Ballots Cast\s*[-–]\s*Total\s+([\d,]+)")

        # Find race blocks in this ward page
        blocks = re.split(r"(?=Vote For\s+\d+)", text)
        for block in blocks:
            if "Vote For" not in block:
                continue

            blines = [l.strip() for l in block.split("\n") if l.strip()]

            # Find race name
            race_name = None
            for i, line in enumerate(blines):
                if re.match(r"Vote For\s+\d+", line) and i + 1 < len(blines):
                    race_name = blines[i + 1]
                    break

            if not race_name:
                continue

            # Match to our known races
            race = race_lookup.get(race_name.lower())
            if not race:
                continue

            # Parse candidate votes for this ward
            ward_candidates = {}
            for line in blines:
                if "Write-In" in line or "Vote For" in line or line == race_name:
                    continue
                if "Precinct Summary" in line or "Page " in line:
                    break

                cm = re.match(r"(.+?)\s{2,}([\d,]+)\s*$", line)
                if cm:
                    ward_candidates[cm.group(1).strip()] = parse_int(cm.group(2))

            if ward_candidates:
                race["wardData"].append({
                    "ward": ward_name,
                    "candidates": ward_candidates,
                    "ballotsCast": ballots_cast,
                    "registered": registered,
                })

    pdf.close()
    return races


# ── HELPERS ──────────────────────────────────────────────────────

def extract_number(text: str, pattern: str) -> int:
    m = re.search(pattern, text)
    return parse_int(m.group(1)) if m else 0


def extract_float(text: str, pattern: str) -> float:
    m = re.search(pattern, text)
    return float(m.group(1)) if m else 0.0


def parse_int(s: str) -> int:
    return int(s.replace(",", ""))


# ── MAIN ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Parse Marathon County election results PDFs")
    parser.add_argument("--pdf", help="Path to a local summary PDF")
    parser.add_argument("--precinct-pdf", help="Path to a local precinct/ward detail PDF")
    parser.add_argument("--url", help="URL to fetch summary PDF from")
    parser.add_argument("--precinct-url", help="URL to fetch precinct PDF from")
    parser.add_argument("--output", help="Output JSON path", default=str(OUTPUT_PATH))
    args = parser.parse_args()

    summary_source = args.pdf or args.url or SUMMARY_PDF_URL
    precinct_source = args.precinct_pdf or args.precinct_url or PRECINCT_PDF_URL

    if not summary_source:
        print("ERROR: No PDF source specified.")
        print("Either:")
        print("  1. Set SUMMARY_PDF_URL in this script")
        print("  2. Pass --pdf <path> or --url <url>")
        print()
        print("Check https://www.marathoncounty.gov/services/elections-voting/results")
        print("for the latest PDF links on election night.")
        sys.exit(1)

    print(f"=== Marathon County Election Results Scraper ===")
    print(f"Election: {ELECTION_CONFIG['name']}")
    print()

    # Fetch/load summary PDF
    print("[1/3] Loading summary PDF...")
    if summary_source.startswith("http"):
        pdf_bytes = fetch_pdf(summary_source)
        data = parse_summary_pdf(pdf_bytes)
    else:
        data = parse_summary_pdf(summary_source)

    print(f"  Found {len(data['races'])} races")
    print(f"  Precincts: {data['election']['precinctsReported']}/{data['election']['precinctsTotal']}")
    print(f"  Ballots cast: {data['statistics']['ballotsCast']}")

    # Fetch/load precinct PDF if available
    if precinct_source:
        print("\n[2/3] Loading precinct detail PDF...")
        if precinct_source.startswith("http"):
            precinct_bytes = fetch_pdf(precinct_source)
            data["races"] = parse_precinct_pdf(precinct_bytes, data["races"])
        else:
            data["races"] = parse_precinct_pdf(precinct_source, data["races"])

        ward_counts = [len(r["wardData"]) for r in data["races"]]
        print(f"  Ward data loaded for {sum(1 for w in ward_counts if w > 0)} races")
    else:
        print("\n[2/3] No precinct PDF specified, skipping ward detail...")

    # Write output
    print(f"\n[3/3] Writing JSON to {args.output}")
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"\n  Done! {output_path.stat().st_size:,} bytes written")

    # Summary
    print(f"\n=== Results Summary ===")
    print(f"Status: {data['election']['status'].upper()}")
    for race in data["races"]:
        leader = race["candidates"][0] if race["candidates"] else None
        print(f"  {race['name']}: {leader['name']} leads ({leader['votes']} votes)" if leader else f"  {race['name']}: No votes yet")


if __name__ == "__main__":
    main()
