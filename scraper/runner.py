"""
WPR Election Night Runner
=========================
One pipeline, two producers:

  1. AUTOMATED (primary): every fetchSeconds, re-discover the three report
     links on the county results page and download any that changed. Runs on
     this machine because the county blocks datacenter IPs (GitHub Actions
     gets 403; residential traffic gets through -- proven April 2026).
  2. MANUAL (always available): any PDF saved into the watched folder is
     picked up within pollSeconds. If the county ever blocks the fetcher,
     download the PDFs in your browser and nothing else changes.

Both producers feed the same hash-dedupe -> parse -> validate -> push flow.

Run:
    python scraper/runner.py                # election night
    python scraper/runner.py --fetch-once   # connectivity self-test, then exit

Press Ctrl+C to stop early.
"""

import argparse
import hashlib
import io
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import parse
from fetch import SLOTS, CountyFetcher, FetchError

REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS_REL = "public/data/results.json"


def run(cmd: str) -> tuple[int, str, str]:
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=REPO_ROOT)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def now_local() -> str:
    return datetime.now().strftime("%I:%M:%S %p")


def preflight(config: dict) -> Path:
    watch_dir = Path(config["watchDir"]).expanduser()
    if not watch_dir.is_dir():
        raise SystemExit(f"ERROR: watchDir does not exist: {watch_dir}")
    code, out, err = run("git remote get-url origin")
    if code != 0:
        raise SystemExit(f"ERROR: not a git repo with an 'origin' remote: {err}")
    code, _, err = run("git push --dry-run origin main")
    if code != 0:
        raise SystemExit(
            f"ERROR: cannot push to origin (fix auth BEFORE election night):\n{err}"
        )
    return watch_dir


def push_results(kind: str, label: str) -> None:
    code, out, _ = run(f"git diff --stat {RESULTS_REL}")
    _, untracked, _ = run(f"git ls-files --others --exclude-standard {RESULTS_REL}")
    if not out.strip() and not untracked.strip():
        print(f"  [{now_local()}] parsed [{kind}] from {label} -- no change, nothing to push")
        return
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    run(f"git add {RESULTS_REL}")
    code, _, err = run(f'git commit -m "Results update ({kind}) {ts}"')
    if code != 0:
        print(f"  ERROR: commit failed: {err}")
        return
    run("git pull --rebase origin main")
    code, _, err = run("git push origin main")
    if code == 0:
        print(f"  [{now_local()}] pushed [{kind}] from {label} -- widget updates within ~1 minute")
    else:
        print(f"  ERROR: push failed: {err}")


def scoreboard() -> None:
    results = REPO_ROOT / RESULTS_REL
    if not results.exists():
        return
    with open(results, encoding="utf-8") as f:
        data = json.load(f)
    e = data["election"]
    rule = "  " + "-" * 62
    print(rule)
    print(f"  precincts {e['precinctsReported']}/{e['precinctsTotal']}"
          f"  |  ballots {data['statistics']['ballotsCast']:,}"
          f"  |  status {e['status'].upper()}")
    marquee = [r for r in data["races"]
               if r["category"] in ("Statewide", "U.S. Congress") and r["candidates"]]
    for r in marquee[:6]:
        lead = r["candidates"][0]
        prec = ""
        if r.get("precincts"):
            prec = f"{r['precincts']['reported']}/{r['precincts']['total']}"
        label = f"{r['party']} {r['name']}"
        line = (f"  {label:<33.33} {prec:>6}  "
                f"{lead['name']} {lead['votes']:,} ({lead['pct']:.1f}%)")
        if len(r["candidates"]) > 1:
            line += f" +{lead['votes'] - r['candidates'][1]['votes']:,}"
        print(line)
    print(rule)


def ingest(source, label: str, config: dict, seen: set) -> None:
    """Shared pipeline entry for both producers."""
    if isinstance(source, (bytes, bytearray)):
        digest = hashlib.sha256(source).hexdigest()
    else:
        try:
            digest = sha256_file(source)
        except OSError:
            return  # still being written by the browser; next pass gets it
    if digest in seen:
        return
    seen.add(digest)
    try:
        kind = parse.process_pdf(source, config)
    except ValueError as e:
        print(f"  [{now_local()}] skipped {label}: {e}")
        return
    except Exception as e:
        print(f"  [{now_local()}] ERROR parsing {label}: {e}")
        return
    push_results(kind, label)
    scoreboard()


def fetch_once(config: dict) -> None:
    """Connectivity self-test: discover links, download each slot, classify."""
    print(f"\n  Fetch self-test against {config['resultsPage']}\n")
    fetcher = CountyFetcher(config["resultsPage"])
    try:
        links = fetcher.discover()
    except FetchError as e:
        print(f"  DISCOVERY FAILED: {e}")
        print("  If this is a 403, the county is blocking this connection --")
        print("  manual browser downloads into the watched folder still work.")
        sys.exit(1)
    for slot, url in links.items():
        try:
            r = fetcher._get(url, referer=config["resultsPage"])
            data = r.content
            ok_pdf = data.startswith(b"%PDF")
            line = (f"  {SLOTS[slot]}: HTTP {r.status_code}, {len(data)//1024} KB, "
                    f"{'PDF' if ok_pdf else 'NOT A PDF'}")
            if ok_pdf:
                import pdfplumber
                with pdfplumber.open(io.BytesIO(data)) as pdf:
                    first = pdf.pages[0].extract_text() or ""
                try:
                    kind = parse.classify(first, config)
                    line += f" -> classified [{kind}]"
                except ValueError as e:
                    line += f" -> would be skipped ({e})"
            print(line)
        except FetchError as e:
            print(f"  {SLOTS[slot]}: FAILED ({e})")
    print("\n  Self-test complete. 'Would be skipped' lines are correct until the")
    print("  county repoints these links to the new election's documents.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch-once", action="store_true",
                    help="Test county connectivity and link discovery, then exit")
    args = ap.parse_args()

    config = parse.load_config()
    if args.fetch_once:
        fetch_once(config)
        return

    stop_at = datetime.fromisoformat(config["stopAtUtc"].replace("Z", "+00:00"))
    poll = int(config.get("pollSeconds", 5))
    fetch_every = int(config.get("fetchSeconds", 90))
    watch_dir = preflight(config)
    fetcher = CountyFetcher(config["resultsPage"])

    print()
    print("=" * 62)
    print(f"  WPR ELECTION NIGHT RUNNER  --  {config['name']}")
    print(f"  Auto-fetch: {config['resultsPage']}")
    print(f"              every {fetch_every}s (link discovery each cycle)")
    print(f"  Watching:   {watch_dir}  (manual saves work any time)")
    print(f"  Runs until {stop_at.astimezone().strftime('%I:%M %p %b %d')} local, Ctrl+C to stop")
    print("=" * 62)
    print()

    seen: set[str] = set()
    last_heartbeat = 0.0
    next_fetch = 0.0
    last_fetch_error = None

    while True:
        if datetime.now(timezone.utc) >= stop_at:
            print(f"\n  [{now_local()}] Reached stop time. Results are final on the widget. Good night!")
            break

        # Producer 1: watched folder (manual saves)
        for pdf in sorted(watch_dir.glob("*.pdf"), key=lambda p: p.stat().st_mtime):
            ingest(pdf, pdf.name, config, seen)

        # Producer 2: automated county fetch
        if time.time() >= next_fetch:
            next_fetch = time.time() + fetch_every
            try:
                for slot, url, data in fetcher.fetch_changed():
                    print(f"  [{now_local()}] fetched new {SLOTS[slot]} ({len(data)//1024} KB)")
                    ingest(data, f"auto:{SLOTS[slot]}", config, seen)
                if last_fetch_error is not None:
                    print(f"  [{now_local()}] county fetch recovered")
                    last_fetch_error = None
            except FetchError as e:
                msg = str(e)
                if msg != last_fetch_error:
                    print(f"  [{now_local()}] county fetch failed: {msg}")
                    print("            (will keep retrying -- manual downloads to the watched folder still work)")
                    last_fetch_error = msg

        if time.time() - last_heartbeat > 300:
            print(f"  [{now_local()}] running -- auto-fetch active, watching folder for manual saves")
            last_heartbeat = time.time()
        time.sleep(poll)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  Stopped. Goodbye!")
        sys.exit(0)
