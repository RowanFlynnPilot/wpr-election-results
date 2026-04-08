"""
Marathon County Election Results Scraper
Wausau Pilot & Review

Auto-discovers election result PDF links from the Marathon County Clerk's
website, parses them into structured JSON, and writes to public/data/election.json.

Usage:
    python scraper/parse_results.py
    python scraper/parse_results.py --pdf path/to/local.pdf
    python scraper/parse_results.py --url https://marathoncounty.gov/...pdf

On election night, simply run `python scraper/parse_results.py` with no arguments.
The scraper will fetch the results page, find the PDF links automatically, and
parse them. No manual URL updates needed.

Fallback: if auto-discovery fails, set SUMMARY_PDF_URL / PRECINCT_PDF_URL below.
"""

import argparse
import io
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import pdfplumber
except ImportError:
    print("ERROR: pdfplumber not installed. Run: pip install -r scraper/requirements.txt")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("ERROR: requests not installed. Run: pip install -r scraper/requirements.txt")
    sys.exit(1)

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: beautifulsoup4 not installed. Run: pip install -r scraper/requirements.txt")
    sys.exit(1)


# ── CONFIGURATION ────────────────────────────────────────────────
# The scraper will auto-discover PDF links from this page.
# Only set the fallback URLs below if auto-discovery fails on election night.

RESULTS_PAGE_URL = "https://www.marathoncounty.gov/services/elections-voting/results"
MARATHON_COUNTY_BASE = "https://www.marathoncounty.gov"

# Fallback URLs — leave empty for auto-discovery, or paste URLs here if needed
SUMMARY_PDF_URL = ""    # e.g. "https://www.marathoncounty.gov/home/showpublisheddocument/15125"
PRECINCT_PDF_URL = ""   # e.g. "https://www.marathoncounty.gov/home/showpublisheddocument/15126"
STATUS_PDF_URL = ""     # e.g. "https://www.marathoncounty.gov/home/showpublisheddocument/15127"

# Election metadata — update per election
ELECTION_CONFIG = {
    "name": "2026 Spring Election",
    "date": "2026-04-07",
    "displayDate": "April 7, 2026",
    "county": "Marathon County",
    "state": "Wisconsin",
}

OUTPUT_PATH = Path(__file__).parent.parent / "public" / "data" / "election.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "no-cache",
}


# ── AUTO-DISCOVERY ───────────────────────────────────────────────

def discover_pdf_urls() -> tuple[str, str, str]:
    """
    Fetch the Marathon County results page and extract the Election Summary,
    Precinct Summary, and Precincts Reported/Not Reported PDF links.

    Returns (summary_url, precinct_url, status_url). Any may be empty string if not found.
    """
    print(f"  Fetching results page: {RESULTS_PAGE_URL}")
    session = requests.Session()
    # Retry up to 3 times with increasing delays
    last_err = None
    for attempt in range(3):
        try:
            if attempt > 0:
                time.sleep(attempt * 5)
                print(f"  Retry {attempt}/2...")
            resp = session.get(RESULTS_PAGE_URL, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            break
        except requests.RequestException as e:
            last_err = e
            print(f"  WARNING: Attempt {attempt + 1} failed: {e}")
    else:
        print(f"  WARNING: Could not fetch results page after 3 attempts: {last_err}")
        return "", "", ""

    soup = BeautifulSoup(resp.text, "html.parser")

    summary_url = ""
    precinct_url = ""
    status_url = ""

    # Find all links on the page
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True).lower()

        # Resolve relative URLs
        if href.startswith("/"):
            href = MARATHON_COUNTY_BASE + href
        elif not href.startswith("http"):
            continue

        # Match by link text
        if "election summary" in text and not summary_url:
            summary_url = href
            print(f"  Found Election Summary: {href}")
        elif "precinct summary" in text and not precinct_url:
            precinct_url = href
            print(f"  Found Precinct Summary: {href}")
        elif any(k in text for k in ("precincts reported", "precinct status", "reported/not reported")) and not status_url:
            status_url = href
            print(f"  Found Precinct Status: {href}")

        # Also match by URL pattern as fallback (showpublisheddocument)
        if "showpublisheddocument" in href:
            if not summary_url and "summary" in text:
                summary_url = href
            elif not precinct_url and "precinct" in text and "status" not in text and "reported" not in text:
                precinct_url = href
            elif not status_url and ("reported" in text or "status" in text):
                status_url = href

    if not summary_url:
        print("  WARNING: Could not find Election Summary link on results page.")
    if not precinct_url:
        print("  WARNING: Could not find Precinct Summary link on results page.")
    if not status_url:
        print("  WARNING: Could not find Precinct Status link on results page (optional).")

    return summary_url, precinct_url, status_url


# ── PDF FETCHING ─────────────────────────────────────────────────

def fetch_pdf(url: str) -> bytes:
    """Download a PDF from a URL."""
    print(f"  Fetching: {url}")
    resp = requests.get(url, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    return resp.content


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

def open_pdf(pdf_path_or_bytes):
    """Open a pdfplumber PDF from a path or bytes."""
    if isinstance(pdf_path_or_bytes, (str, Path)):
        return pdfplumber.open(pdf_path_or_bytes)
    return pdfplumber.open(io.BytesIO(pdf_path_or_bytes))


def parse_summary_pdf(pdf_source) -> dict:
    """
    Parse the Election Summary PDF into structured data.

    Marathon County's summary PDF format:
    - Page header: "Summary Results Report UNOFFICIAL RESULTS"
    - Election name & date
    - Statistics block (Registered Voters, Ballots Cast, etc.)
    - Race blocks with "Vote For N", candidate names, and vote counts
    """
    pdf = open_pdf(pdf_source)
    full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    pdf.close()

    stats = {
        "registeredVoters": extract_number(full_text, r"Registered Voters\s*[-–]\s*Total\s+([\d,]+)"),
        "ballotsCast": extract_number(full_text, r"Ballots Cast\s*[-–]\s*Total\s+([\d,]+)"),
        "blanks": extract_number(full_text, r"Ballots Cast\s*[-–]\s*Blank\s+([\d,]+)"),
        "turnoutPct": extract_float(full_text, r"Voter Turnout\s*[-–]\s*Total\s+([\d.]+)%"),
    }

    precincts_match = re.search(r"Precincts Complete\s+(\d+)\s+of\s+(\d+)", full_text)
    precincts_reported = int(precincts_match.group(1)) if precincts_match else 0
    precincts_total = int(precincts_match.group(2)) if precincts_match else 0

    status = "final" if precincts_reported == precincts_total and precincts_total > 0 else "live"

    time_match = re.search(r"(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}[AP]M)", full_text)
    if time_match:
        try:
            last_updated = datetime.strptime(time_match.group(1), "%m/%d/%Y %I:%M%p")
            last_updated_iso = last_updated.isoformat()
        except ValueError:
            last_updated_iso = datetime.now(timezone.utc).isoformat()
    else:
        last_updated_iso = datetime.now(timezone.utc).isoformat()

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
    blocks = re.split(r"(?=TOTAL\s*\nVote For\s+\d+)", text)

    for block in blocks:
        if "Vote For" not in block:
            continue

        lines = [l.strip() for l in block.strip().split("\n") if l.strip()]

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

        race_name = lines[race_name_start]

        candidates = []
        write_ins = 0
        for line in lines[race_name_start + 1:]:
            if "Election Summary" in line or "Page " in line or "Summary Results" in line:
                break

            wm = re.match(r"Write-In Totals\s+([\d,]+)", line)
            if wm:
                write_ins = parse_int(wm.group(1))
                continue

            cm = re.match(r"(.+?)\s{2,}([\d,]+)\s*$", line)
            if cm:
                candidates.append({
                    "name": cm.group(1).strip(),
                    "votes": parse_int(cm.group(2)),
                })

        if not candidates:
            continue

        races.append({
            "id": slugify(race_name),
            "name": race_name,
            "type": "general",
            "seats": seats,
            "jurisdiction": ELECTION_CONFIG["county"],
            "category": detect_category(race_name),
            "candidates": sorted(candidates, key=lambda c: c["votes"], reverse=True),
            "writeIns": write_ins,
            "precincts": {
                "reported": precincts_reported,
                "total": precincts_total,
            },
            "wardData": [],
        })

    return races


def parse_precinct_pdf(pdf_source, races: list) -> list:
    """
    Parse the Precinct Summary (By Ward Detail) PDF and merge ward data
    into the existing races list.
    """
    pdf = open_pdf(pdf_source)
    race_lookup = {r["name"].lower(): r for r in races}

    for page in pdf.pages:
        text = page.extract_text() or ""
        lines = [l.strip() for l in text.split("\n") if l.strip()]

        if not lines:
            continue

        ward_name = lines[0]
        if "Summary Results" in ward_name or "UNOFFICIAL" in ward_name:
            continue

        registered = extract_number(text, r"Registered Voters\s*[-–]\s*Total\s+([\d,]+)")
        ballots_cast = extract_number(text, r"Ballots Cast\s*[-–]\s*Total\s+([\d,]+)")

        blocks = re.split(r"(?=Vote For\s+\d+)", text)
        for block in blocks:
            if "Vote For" not in block:
                continue

            blines = [l.strip() for l in block.split("\n") if l.strip()]

            race_name = None
            for i, line in enumerate(blines):
                if re.match(r"Vote For\s+\d+", line) and i + 1 < len(blines):
                    race_name = blines[i + 1]
                    break

            if not race_name:
                continue

            race = race_lookup.get(race_name.lower())
            if not race:
                continue

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


def parse_precinct_status_pdf(pdf_source) -> list:
    """
    Parse the "Precincts Reported / Not Reported" PDF into a list of precinct
    status objects.

    Expected row format (tab-separated columns):
        Precinct ID    Precinct Name    ...    Reported  (or Not Reported)

    Returns a list of {"id": str, "name": str, "status": "reported"|"notReported"}.
    Returns [] on any parse error so the scraper never fails on this.
    """
    try:
        pdf = open_pdf(pdf_source)
        results = []
        seen = set()

        for page in pdf.pages:
            text = page.extract_text() or ""
            for line in text.split("\n"):
                line = line.strip()
                if not line:
                    continue

                # Pattern 1: lines ending with "Reported" or "Not Reported"
                # e.g. "0001  City of Wausau Ward 1  ...  Reported"
                m = re.search(r"^(\d{3,4})\s{2,}(.+?)\s{2,}(Not Reported|Reported)\s*$", line)
                if m:
                    name = m.group(2).strip()
                    raw_status = m.group(3)
                    key = name.lower()
                    if key not in seen:
                        seen.add(key)
                        results.append({
                            "id": slugify(name),
                            "name": name,
                            "status": "reported" if raw_status == "Reported" else "notReported",
                        })
                    continue

                # Pattern 2: lines with just a name followed by status (no leading ID)
                m2 = re.search(r"^(.+?)\s{2,}(Not Reported|Reported)\s*$", line)
                if m2:
                    name = m2.group(1).strip()
                    raw_status = m2.group(2)
                    # Skip header-like lines
                    if any(h in name.lower() for h in ("precinct", "ward name", "municipality", "page")):
                        continue
                    key = name.lower()
                    if key not in seen:
                        seen.add(key)
                        results.append({
                            "id": slugify(name),
                            "name": name,
                            "status": "reported" if raw_status == "Reported" else "notReported",
                        })

        pdf.close()

        if results:
            reported_n = sum(1 for p in results if p["status"] == "reported")
            print(f"  Precinct status: {reported_n} of {len(results)} reported")
        else:
            print("  WARNING: No precinct status rows found in PDF.")

        return results

    except Exception as e:
        print(f"  WARNING: Could not parse precinct status PDF: {e}")
        return []


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
    parser = argparse.ArgumentParser(description="Parse Marathon County election results")
    parser.add_argument("--pdf", help="Path to a local summary PDF (skips auto-discovery)")
    parser.add_argument("--precinct-pdf", help="Path to a local precinct/ward detail PDF")
    parser.add_argument("--status-pdf", help="Path to local Precincts Reported/Not Reported PDF")
    parser.add_argument("--url", help="Direct URL to summary PDF (skips auto-discovery)")
    parser.add_argument("--precinct-url", help="Direct URL to precinct PDF")
    parser.add_argument("--status-url", help="Direct URL to Precincts Reported/Not Reported PDF")
    parser.add_argument("--output", help="Output JSON path", default=str(OUTPUT_PATH))
    args = parser.parse_args()

    print(f"=== Marathon County Election Results Scraper ===")
    print(f"Election: {ELECTION_CONFIG['name']}")
    print()

    # Determine PDF sources — priority: CLI arg > hardcoded fallback > auto-discover
    summary_source = args.pdf or args.url or SUMMARY_PDF_URL
    precinct_source = args.precinct_pdf or args.precinct_url or PRECINCT_PDF_URL
    status_source = args.status_pdf or args.status_url or STATUS_PDF_URL

    if not summary_source:
        print("[1/3] Auto-discovering PDF links from results page...")
        discovered_summary, discovered_precinct, discovered_status = discover_pdf_urls()
        summary_source = discovered_summary
        if not precinct_source:
            precinct_source = discovered_precinct
        if not status_source:
            status_source = discovered_status

    if not summary_source:
        print()
        print("ERROR: Could not find Election Summary PDF.")
        print("Options:")
        print("  1. Check the results page manually:", RESULTS_PAGE_URL)
        print("  2. Set SUMMARY_PDF_URL in this script")
        print("  3. Pass --url <pdf_url> or --pdf <path>")
        sys.exit(1)

    # Parse summary PDF
    print("\n[2/3] Parsing Election Summary PDF...")
    if summary_source.startswith("http"):
        data = parse_summary_pdf(fetch_pdf(summary_source))
    else:
        data = parse_summary_pdf(summary_source)

    print(f"  Found {len(data['races'])} races")
    print(f"  Precincts: {data['election']['precinctsReported']}/{data['election']['precinctsTotal']}")
    print(f"  Ballots cast: {data['statistics']['ballotsCast']}")
    print(f"  Status: {data['election']['status'].upper()}")

    # Parse precinct PDF if available
    if precinct_source:
        print("\n[3/3] Parsing Precinct Summary PDF...")
        if precinct_source.startswith("http"):
            data["races"] = parse_precinct_pdf(fetch_pdf(precinct_source), data["races"])
        else:
            data["races"] = parse_precinct_pdf(precinct_source, data["races"])

        ward_counts = [len(r["wardData"]) for r in data["races"]]
        print(f"  Ward data loaded for {sum(1 for w in ward_counts if w > 0)} races")
    else:
        print("\n[3/3] No Precinct Summary PDF found, skipping ward detail...")

    # Parse precinct status PDF if available
    if status_source:
        print("\n[+] Parsing Precincts Reported/Not Reported PDF...")
        try:
            if status_source.startswith("http"):
                data["precinctList"] = parse_precinct_status_pdf(fetch_pdf(status_source))
            else:
                data["precinctList"] = parse_precinct_status_pdf(status_source)
        except Exception as e:
            print(f"  WARNING: Status PDF failed, skipping: {e}")
            data["precinctList"] = []
    else:
        print("\n[+] No Precinct Status PDF found, skipping precinct list...")
        data["precinctList"] = []

    # Write output
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"\n  Written to {output_path} ({output_path.stat().st_size:,} bytes)")

    # Results summary
    print(f"\n=== Results Summary ===")
    for race in data["races"]:
        leader = race["candidates"][0] if race["candidates"] else None
        if leader:
            print(f"  {race['name']}: {leader['name']} leads ({leader['votes']:,} votes)")
        else:
            print(f"  {race['name']}: no votes yet")


if __name__ == "__main__":
    main()
