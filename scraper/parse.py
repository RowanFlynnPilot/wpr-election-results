"""
Marathon County Election Results Parser
Wausau Pilot & Review

Parses the three Marathon County Clerk election-night PDF reports into
public/data/results.json:

  1. Election Summary  ("Summary Results Report")  -> races, stats, turnout
  2. Precinct Summary  (By Ward Detail)            -> ward-level drill-down
  3. Precincts Reported/Not Reported               -> precinct status list

The report type is detected from the PDF's own content -- filenames do not
matter. A PDF that does not belong to the configured election (see
election.config.json) is rejected loudly.

Usage:
    python scraper/parse.py --pdf path/to/report.pdf
    python scraper/parse.py --pdf a.pdf --pdf b.pdf --pdf c.pdf

Validation is strict: a race whose candidate votes don't sum to its reported
total is a parse error, and parse errors raise. No silent guesses.
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pdfplumber

import io
import logging
logging.getLogger("pdfminer").setLevel(logging.ERROR)

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "election.config.json"
OUTPUT_PATH = REPO_ROOT / "public" / "data" / "results.json"

PARTY_CODES = {
    "DEM": "Democratic",
    "REP": "Republican",
    "CON": "Constitution",
    "LIB": "Libertarian",
    "WGN": "Wisconsin Green",
    "GRN": "Green",
    "IND": "Independent",
    "NPA": "Nonpartisan",
}

MARKER_RE = re.compile(r"^TOTAL(\s+VOTE\s+%)?$")
VOTE_FOR_RE = re.compile(r"^Vote For\s+(\d+)$", re.IGNORECASE)
WRITEIN_RE = re.compile(r"^Write-In Totals\s+([\d,]+)(?:\s+[\d.]+%)?$")
TOTAL_RE = re.compile(r"^Total Votes Cast\s+([\d,]+)(?:\s+[\d.]+%)?$")
OVER_RE = re.compile(r"^Overvotes\s+([\d,]+)$")
UNDER_RE = re.compile(r"^Undervotes\s+([\d,]+)$")
PRECINCTS_RE = re.compile(r"^Precincts Reporting\s+(\d+)\s+of\s+(\d+)")
CANDIDATE_RE = re.compile(r"^(?P<name>.+?)\s+(?P<votes>[\d,]+)(?:\s+(?P<pct>\d+\.\d+)%)?$")
TIMESTAMP_RE = re.compile(r"(\d{2}/\d{2}/\d{4})\s+(\d{1,2}:\d{2})\s*([AP]M)")


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def parse_int(s: str) -> int:
    return int(s.replace(",", ""))


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s-]+", "-", text)
    return text[:70]


def detect_category(name: str) -> str:
    n = name.lower()
    if n.startswith("question") or "referendum" in n:
        return "Referendum"
    if any(k in n for k in ("governor", "attorney general", "secretary of state", "state treasurer")):
        return "Statewide"
    if any(k in n for k in ("united states senator", "representative in congress", "president")):
        return "U.S. Congress"
    if any(k in n for k in ("state senator", "assembly")):
        return "State Legislature"
    if any(k in n for k in ("district attorney", "sheriff", "clerk of circuit court",
                            "clerk of courts", "county", "register of deeds", "coroner",
                            "surveyor", "treasurer")):
        return "County"
    return "Local"


def split_party(raw_name: str) -> tuple[str | None, str]:
    """'REP District Attorney' -> ('REP', 'District Attorney')."""
    m = re.match(r"^([A-Z]{3})\s+(.+)$", raw_name)
    if m and m.group(1) in PARTY_CODES:
        return m.group(1), m.group(2)
    if raw_name.lower().startswith("question") or "referendum" in raw_name.lower():
        return "REF", raw_name
    return None, raw_name


def clean_lines(text: str, config: dict) -> tuple[list[str], str | None]:
    """Strip report headers/footers. Returns (lines, last_updated_iso)."""
    last_updated = None
    tm = TIMESTAMP_RE.search(text)
    if tm:
        try:
            dt = datetime.strptime(f"{tm.group(1)} {tm.group(2)} {tm.group(3)}", "%m/%d/%Y %I:%M %p")
            last_updated = dt.isoformat()
        except ValueError:
            pass

    date_county_re = re.compile(rf"^[A-Z][a-z]+ \d{{1,2}}, \d{{4}}\s+{re.escape(config['county'])}$")
    header_res = [
        re.compile(r"^Summary Results Report"),
        re.compile(r"^UNOFFICIAL"),
        re.compile(r"^Precinct Summary\b.*"),
        re.compile(r"^Election Summary\s*-\s*.+"),
        re.compile(r"^Page \d+ of \d+$"),
        re.compile(rf"^{re.escape(config['name'])}$"),
        date_county_re,
    ]

    lines = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if any(r.match(line) for r in header_res):
            continue
        lines.append(line)
    return lines, last_updated


def _keyword_match(line: str):
    for kind, rx in (("writein", WRITEIN_RE), ("total", TOTAL_RE), ("over", OVER_RE),
                     ("under", UNDER_RE), ("precincts", PRECINCTS_RE), ("votefor", VOTE_FOR_RE),
                     ("marker", MARKER_RE)):
        m = rx.match(line)
        if m:
            return kind, m
    return None, None


def parse_race_blocks(lines: list[str], source: str) -> list[dict]:
    """
    Split cleaned lines into race blocks and parse each.

    Different PDF text extractors emit the race name either before or after the
    'Vote For N' line. The mode is detected per block from evidence: if the line
    immediately after 'Vote For' is a structural/candidate line, the name must
    precede it, and vice versa.
    """
    # Segment on TOTAL / TOTAL VOTE % markers
    segments, current = [], []
    for line in lines:
        if MARKER_RE.match(line):
            if current:
                segments.append(current)
            current = []
        else:
            current.append(line)
    if current:
        segments.append(current)

    # Race segments only (preamble/statistics segments have no 'Vote For')
    race_segs = []
    for seg in segments:
        vf_idx = next((i for i, l in enumerate(seg) if VOTE_FOR_RE.match(l)), None)
        if vf_idx is not None:
            race_segs.append((seg, vf_idx))
    if not race_segs:
        return []

    # Mode detection: PDF text extractors emit the race name either before the
    # 'Vote For N' line or immediately after it. Every block in one PDF follows
    # the same convention, so detect it once from evidence and refuse mixed
    # signals rather than guessing per block.
    before_count = sum(1 for _, i in race_segs if i > 0)
    if before_count == len(race_segs):
        mode = "name-first"
    elif before_count == 0:
        mode = "name-after"
    else:
        raise ValueError(
            f"{source}: inconsistent race-name placement "
            f"({before_count} of {len(race_segs)} blocks name-first) -- format change, refusing to guess"
        )

    races = []
    for seg, vf_idx in race_segs:
        seats = int(VOTE_FOR_RE.match(seg[vf_idx]).group(1))
        if mode == "name-first":
            name_lines = seg[:vf_idx]
            body = seg[vf_idx + 1:]
        else:
            if len(seg) < 2:
                raise ValueError(f"{source}: race block too short to contain a name: {seg}")
            name_lines = [seg[1]]
            body = seg[2:]
        raw_name = " ".join(name_lines).strip()
        if not raw_name or _keyword_match(raw_name)[0] is not None:
            raise ValueError(f"{source}: could not extract race name from block: {seg[:4]}")

        race = {
            "rawName": raw_name,
            "seats": seats,
            "candidates": [],
            "writeIns": 0,
            "totalVotes": 0,
            "overvotes": 0,
            "undervotes": 0,
            "precincts": None,
        }
        for line in body:
            kind, m = _keyword_match(line)
            if kind == "writein":
                race["writeIns"] = parse_int(m.group(1))
            elif kind == "total":
                race["totalVotes"] = parse_int(m.group(1))
            elif kind == "over":
                race["overvotes"] = parse_int(m.group(1))
            elif kind == "under":
                race["undervotes"] = parse_int(m.group(1))
            elif kind == "precincts":
                race["precincts"] = {"reported": int(m.group(1)), "total": int(m.group(2))}
                break
            elif kind in ("votefor", "marker"):
                raise ValueError(f"{source}: unexpected '{line}' inside race '{raw_name}'")
            else:
                cm = CANDIDATE_RE.match(line)
                if not cm:
                    raise ValueError(f"{source}: unparseable line in race '{raw_name}': '{line}'")
                race["candidates"].append({
                    "name": cm.group("name").strip(),
                    "votes": parse_int(cm.group("votes")),
                    "pct": float(cm.group("pct")) if cm.group("pct") else 0.0,
                })

        # Strict validation: candidates + write-ins must equal the reported total
        cand_sum = sum(c["votes"] for c in race["candidates"]) + race["writeIns"]
        if cand_sum != race["totalVotes"]:
            raise ValueError(
                f"{source}: vote sum mismatch in '{raw_name}': "
                f"candidates+writeIns={cand_sum} but Total Votes Cast={race['totalVotes']}"
            )
        races.append(race)
    return races


def parse_summary(text: str, config: dict) -> dict:
    lines, last_updated = clean_lines(text, config)
    full = "\n".join(lines)

    pm = re.search(r"Precincts Complete\s+(\d+)\s+of\s+(\d+)", full)
    precincts_reported = int(pm.group(1)) if pm else 0
    precincts_total = int(pm.group(2)) if pm else 0

    ballots_by_party = {}
    ballots_total = 0
    blank = 0
    for m in re.finditer(r"^Ballots Cast\s*[-–]\s*(.+?)\s+([\d,]+)$", full, re.MULTILINE):
        label, value = m.group(1).strip(), parse_int(m.group(2))
        if label == "Total":
            ballots_total = value
        elif label == "Blank":
            blank = value
        else:
            ballots_by_party[label] = value

    tm = re.search(r"Voter Turnout\s*[-–]\s*Total\s+([\d.]+)%", full)
    turnout = float(tm.group(1)) if tm else 0.0

    blocks = parse_race_blocks(lines, "summary")
    if not blocks:
        raise ValueError("summary: no race blocks found -- wrong PDF or format change")

    party_preference = None
    races = []
    for b in blocks:
        if b["rawName"].lower().startswith("party preference"):
            party_preference = b["candidates"]
            continue
        party, display_name = split_party(b["rawName"])
        races.append({
            "id": slugify(f"{party or 'np'}-{display_name}"),
            "party": party,
            "partyLabel": PARTY_CODES.get(party, "Referendum" if party == "REF" else "Nonpartisan"),
            "name": display_name,
            "rawName": b["rawName"],
            "category": detect_category(display_name),
            "seats": b["seats"],
            "candidates": sorted(b["candidates"], key=lambda c: c["votes"], reverse=True),
            "writeIns": b["writeIns"],
            "totalVotes": b["totalVotes"],
            "overvotes": b["overvotes"],
            "undervotes": b["undervotes"],
            "precincts": b["precincts"] or {"reported": precincts_reported, "total": precincts_total},
            "wards": [],
        })

    status = "final" if precincts_total > 0 and precincts_reported == precincts_total else "live"

    return {
        "election": {
            "name": config["name"],
            "date": config["date"],
            "displayDate": config["displayDate"],
            "county": config["county"],
            "state": config["state"],
            "status": status,
            "lastUpdated": last_updated or datetime.now(timezone.utc).isoformat(),
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "precinctsReported": precincts_reported,
            "precinctsTotal": precincts_total,
        },
        "statistics": {
            "ballotsCast": ballots_total,
            "ballotsByParty": ballots_by_party,
            "blank": blank,
            "turnoutPct": turnout,
        },
        "partyPreference": party_preference,
        "races": races,
        "precinctList": [],
    }


def parse_ward_pages(pages: list[str], config: dict, races: list[dict]) -> None:
    """Merge ward-level results from the Precinct Summary PDF into races (in place)."""
    lookup = {r["rawName"].lower(): r for r in races}
    for r in races:
        r["wards"] = []

    matched_any = False
    for page_text in pages:
        lines, _ = clean_lines(page_text, config)
        if not lines or MARKER_RE.match(lines[0]) or VOTE_FOR_RE.match(lines[0]):
            continue
        ward_name = lines[0]
        page_full = "\n".join(lines)
        bm = re.search(r"Ballots Cast\s*[-–]\s*Total\s+([\d,]+)", page_full)
        ballots_cast = parse_int(bm.group(1)) if bm else 0

        try:
            blocks = parse_race_blocks(lines[1:], f"ward '{ward_name}'")
        except ValueError as e:
            raise ValueError(f"ward page parse failed: {e}") from e

        for b in blocks:
            race = lookup.get(b["rawName"].lower())
            if race is None:
                continue  # races not on this ward's ballot, or ward-only contests
            race["wards"].append({
                "ward": ward_name,
                "ballotsCast": ballots_cast,
                "candidates": {c["name"]: c["votes"] for c in b["candidates"]},
                "writeIns": b["writeIns"],
            })
            matched_any = True

    if not matched_any:
        raise ValueError(
            "ward PDF parsed but no race names matched the summary -- "
            "drop the Election Summary PDF first, then this one"
        )


def parse_status(text: str, config: dict) -> list[dict]:
    lines, _ = clean_lines(text, config)
    results, seen = [], set()
    row_re = re.compile(r"^(?:(\d{3,4})\s+)?(.+?)\s+(Not Reported|Reported)$")
    for line in lines:
        m = row_re.match(line)
        if not m:
            continue
        name = m.group(2).strip()
        if any(h in name.lower() for h in ("precinct", "ward name", "municipality", "page")):
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        results.append({
            "id": slugify(name),
            "name": name,
            "status": "reported" if m.group(3) == "Reported" else "notReported",
        })
    return results


# ── PDF classification and orchestration ─────────────────────────

def classify(first_page_text: str, config: dict) -> str:
    """Return 'summary' | 'ward' | 'status'. Raises if the PDF doesn't belong here."""
    t = first_page_text
    if config["name"] not in t:
        raise ValueError(f"PDF is not for '{config['name']}' -- skipping")
    if "Precinct Summary" in t or "By Ward" in t:
        return "ward"
    if "Not Reported" in t and "Reported" in t and "Summary Results Report" not in t:
        return "status"
    if "Summary Results Report" in t:
        return "summary"
    raise ValueError("PDF matches the election but no known report type")


def load_existing() -> dict | None:
    if OUTPUT_PATH.exists():
        with open(OUTPUT_PATH) as f:
            data = json.load(f)
        if data.get("races"):
            return data
    return None


def process_pdf(source: str | Path | bytes, config: dict) -> str:
    """Parse one PDF (a file path or raw bytes) and merge it into results.json.
    Returns the report kind."""
    opener = io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source
    with pdfplumber.open(opener) as pdf:
        pages = [p.extract_text() or "" for p in pdf.pages]
    if not pages or not pages[0].strip():
        raise ValueError("PDF has no extractable text")

    kind = classify(pages[0], config)
    existing = load_existing()

    if kind == "summary":
        data = parse_summary("\n".join(pages), config)
        if existing:  # carry ward data forward across summary refreshes
            prior_wards = {r["id"]: r["wards"] for r in existing["races"] if r.get("wards")}
            for r in data["races"]:
                if r["id"] in prior_wards:
                    r["wards"] = prior_wards[r["id"]]
            data["precinctList"] = existing.get("precinctList", [])
    elif kind == "ward":
        if not existing:
            raise ValueError("ward PDF arrived before any Election Summary -- drop the summary first")
        parse_ward_pages(pages, config, existing["races"])
        existing["election"]["generatedAt"] = datetime.now(timezone.utc).isoformat()
        data = existing
    else:  # status
        if not existing:
            raise ValueError("status PDF arrived before any Election Summary -- drop the summary first")
        plist = parse_status("\n".join(pages), config)
        if not plist:
            raise ValueError("status PDF parsed but no precinct rows found")
        existing["precinctList"] = plist
        existing["election"]["generatedAt"] = datetime.now(timezone.utc).isoformat()
        data = existing

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return kind


def main():
    ap = argparse.ArgumentParser(description="Parse Marathon County election PDFs")
    ap.add_argument("--pdf", action="append", required=True, help="PDF path (repeatable)")
    args = ap.parse_args()

    config = load_config()
    for path in args.pdf:
        kind = process_pdf(path, config)
        print(f"  parsed [{kind}] {path}")
    with open(OUTPUT_PATH) as f:
        data = json.load(f)
    print(f"  {len(data['races'])} races -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
