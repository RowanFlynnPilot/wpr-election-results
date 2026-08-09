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

VOTE_FOR_RE = re.compile(r"^Vote For\s+(\d+)$", re.IGNORECASE)
WRITEIN_RE = re.compile(r"^Write-In Totals\s+([\d,]+)(?:\s+[\d.]+%)?$")
TOTAL_RE = re.compile(r"^Total Votes Cast\s+([\d,]+)(?:\s+[\d.]+%)?$")
OVER_RE = re.compile(r"^Overvotes\s+([\d,]+)$")
UNDER_RE = re.compile(r"^Undervotes\s+([\d,]+)$")
PRECINCTS_RE = re.compile(r"^Precincts Reporting\s+(\d+)\s+of\s+(\d+)")
COLHEAD_RE = re.compile(r"^(?:TOTAL|VOTE\s*%|TOTAL\s+VOTE\s+%)$")
STAT_LINE_RE = re.compile(
    r"^(?:Statistics\b|Precincts Complete\b|Ballots Cast\b|Voter Turnout\b|"
    r"Registered Voters\b|Times Counted\b)")
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
                     ("under", UNDER_RE), ("precincts", PRECINCTS_RE), ("votefor", VOTE_FOR_RE)):
        m = rx.match(line)
        if m:
            return kind, m
    return None, None


def is_name_like(line: str) -> bool:
    """A line that starts a race name even if it superficially matches the
    candidate pattern (e.g. 'DEM State Senator District 12'). Grounded in the
    real reports: partisan race names carry a party prefix, referenda start
    with 'Question', and 'Party Preference Section' is its own pseudo-race."""
    m = re.match(r"^([A-Z]{3})\s+\S", line)
    if m and m.group(1) in PARTY_CODES:
        return True
    low = line.lower()
    return low.startswith("question") or low.startswith("party preference")


def parse_race_blocks(lines: list[str], source: str) -> list[dict]:
    """
    Parse race blocks from a cleaned line stream. Grounded in the real 2024
    PDFs (summary and 447-page ward detail, inspected 2026-08-08):

      - 'Vote For N' begins a race's structure
      - the body is the run of result lines that follows: candidate lines,
        Write-In Totals, and (summary report only) Total Votes Cast,
        Overvotes, Undervotes, Precincts Reporting. The ward report is
        condensed -- candidates plus at most a Write-In line, and the Party
        Preference block has no result keywords at all.
      - the body ends at the first name-like or statistics line
      - 'TOTAL' / 'VOTE %' column headers are layout noise wherever they fall
      - the race name sits either immediately before 'Vote For' or
        immediately after it; placement is constant within one PDF
    """
    lines = [l for l in lines if not COLHEAD_RE.match(l)]
    vf_idxs = [i for i, l in enumerate(lines) if VOTE_FOR_RE.match(l)]
    if not vf_idxs:
        return []

    def body_end(start_i: int, limit: int) -> int:
        """First index at/after start_i that is not a body line."""
        j = start_i
        while j < limit:
            line = lines[j]
            if is_name_like(line) or STAT_LINE_RE.match(line):
                break
            kind, _ = _keyword_match(line)
            if kind == "votefor":
                break
            if kind is None and not CANDIDATE_RE.match(line):
                break
            j += 1
        return j

    # Name placement is determined by block 0: only in name-first mode does a
    # name line sit between the preceding statistics/keyword line and the
    # first 'Vote For'. Every other block is then checked for consistency.
    first_pre = []
    j = vf_idxs[0] - 1
    while j >= 0:
        line = lines[j]
        if STAT_LINE_RE.match(line) or _keyword_match(line)[0] is not None:
            break
        first_pre.append(line)
        j -= 1
    first_pre.reverse()
    mode = "name-first" if first_pre else "name-after"

    races = []
    pending_name = first_pre  # name-first: block k's name lines, collected ahead
    for k, vi in enumerate(vf_idxs):
        limit = vf_idxs[k + 1] if k + 1 < len(vf_idxs) else len(lines)
        seats = int(VOTE_FOR_RE.match(lines[vi]).group(1))
        if mode == "name-first":
            raw_name = " ".join(pending_name).strip()
            be = body_end(vi + 1, limit)
            body = lines[vi + 1:be]
            pending_name = [l for l in lines[be:limit] if not STAT_LINE_RE.match(l)]
            if k + 1 < len(vf_idxs) and not pending_name:
                raise ValueError(
                    f"{source}: no name found between blocks {k} and {k + 1} "
                    f"(after '{raw_name}') -- inconsistent format, refusing to guess")
        else:
            if vi + 1 >= limit:
                raise ValueError(f"{source}: race block too short to contain a name after '{lines[vi]}'")
            raw_name = lines[vi + 1]
            be = body_end(vi + 2, limit)
            body = lines[vi + 2:be]
            leftover = [l for l in lines[be:limit] if not STAT_LINE_RE.match(l)]
            if leftover:
                raise ValueError(
                    f"{source}: unexpected lines after race '{raw_name}' body: "
                    f"{leftover[:3]} -- inconsistent format, refusing to guess")
        if not raw_name or _keyword_match(raw_name)[0] is not None:
            raise ValueError(f"{source}: could not extract race name near '{lines[vi]}'")

        race = {
            "rawName": raw_name,
            "seats": seats,
            "candidates": [],
            "writeIns": 0,
            "totalVotes": None,
            "overvotes": 0,
            "undervotes": 0,
            "precincts": None,
        }
        seen_total = False
        for line in body:
            kind, m = _keyword_match(line)
            if kind == "writein":
                race["writeIns"] = parse_int(m.group(1))
            elif kind == "total":
                race["totalVotes"] = parse_int(m.group(1))
                seen_total = True
            elif kind == "over":
                race["overvotes"] = parse_int(m.group(1))
            elif kind == "under":
                race["undervotes"] = parse_int(m.group(1))
            elif kind == "precincts":
                race["precincts"] = {"reported": int(m.group(1)), "total": int(m.group(2))}
            elif kind is not None:
                raise ValueError(f"{source}: unexpected '{line}' inside race '{raw_name}'")
            else:
                cm = CANDIDATE_RE.match(line)
                if not cm:
                    raise ValueError(f"{source}: unparseable line in race '{raw_name}': '{line}'")
                if seen_total:
                    raise ValueError(
                        f"{source}: candidate-like line after 'Total Votes Cast' in "
                        f"'{raw_name}': '{line}' -- format change, refusing to guess")
                race["candidates"].append({
                    "name": cm.group("name").strip(),
                    "votes": parse_int(cm.group("votes")),
                    "pct": float(cm.group("pct")) if cm.group("pct") else 0.0,
                })

        # Where the report states a total, the parts must sum to it exactly
        if seen_total:
            cand_sum = sum(c["votes"] for c in race["candidates"]) + race["writeIns"]
            if cand_sum != race["totalVotes"]:
                raise ValueError(
                    f"{source}: vote sum mismatch in '{raw_name}': "
                    f"candidates+writeIns={cand_sum} but Total Votes Cast={race['totalVotes']}")
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
        if b["precincts"] is None or b["totalVotes"] is None:
            raise ValueError(
                f"summary: race '{b['rawName']}' is missing 'Precincts Reporting' or "
                f"'Total Votes Cast' -- wrong report type or format change")
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
            "precincts": b["precincts"],
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
    """Merge ward-level results from the By Ward Detail PDF into races (in place).

    Wards are NOT one-per-page: on a partisan ballot each ward carries dozens
    of races and spans multiple pages. All pages are therefore joined into one
    stream and split into ward sections anchored on each ward's Statistics
    block -- the line immediately preceding a stats run is the ward name.
    Repeated ward-name page headers inside a section are dropped, which also
    heals race blocks split across page boundaries."""
    lookup = {r["rawName"].lower(): r for r in races}
    for r in races:
        r["wards"] = []

    all_lines: list[str] = []
    for page_text in pages:
        cleaned, _ = clean_lines(page_text, config)
        all_lines.extend(cleaned)

    stat_starts = [i for i, l in enumerate(all_lines)
                   if STAT_LINE_RE.match(l) and (i == 0 or not STAT_LINE_RE.match(all_lines[i - 1]))]
    if not stat_starts:
        raise ValueError("ward PDF has no ward statistics blocks -- wrong report type or format change")

    sections = []
    for idx, s in enumerate(stat_starts):
        if s == 0:
            raise ValueError("ward PDF: statistics block with no preceding ward name line")
        name_line = all_lines[s - 1]
        if _keyword_match(name_line)[0] is not None or VOTE_FOR_RE.match(name_line):
            raise ValueError(f"ward PDF: expected a ward name before statistics, got '{name_line}'")
        end_i = stat_starts[idx + 1] - 1 if idx + 1 < len(stat_starts) else len(all_lines)
        sections.append((name_line, all_lines[s:end_i]))

    matched_any = False
    unmatched: dict[str, int] = {}
    for ward_name, seg in sections:
        seg = [l for l in seg if l != ward_name]  # drop repeated page headers
        page_full = "\n".join(seg)
        bm = re.search(r"Ballots Cast\s*[-\u2013]\s*Total\s+([\d,]+)", page_full)
        ballots_cast = parse_int(bm.group(1)) if bm else 0

        blocks = parse_race_blocks(seg, f"ward '{ward_name}'")
        for b in blocks:
            if b["rawName"].lower().startswith("party preference"):
                continue  # ward-level party preference is not a summary race
            race = lookup.get(b["rawName"].lower())
            if race is None:
                unmatched[b["rawName"]] = unmatched.get(b["rawName"], 0) + 1
                continue
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
    if unmatched:
        names = ", ".join(sorted(unmatched)[:5])
        raise ValueError(
            f"ward PDF contains race names absent from the summary ({names}) -- "
            f"likely a name-extraction error, refusing to publish partial ward data")


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
