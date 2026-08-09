"""
WPR Election Night Runner
=========================
Watches your downloads folder for Marathon County result PDFs, parses each new
one the moment it lands, and pushes updated results to GitHub. The widget on
the site refreshes itself within about a minute of each push.

Your only job on election night:
  1. Keep this running in a terminal.
  2. When the county posts/updates results, download the three report PDFs
     from their results page (links printed below) into your downloads folder.
  3. That's it. Filenames don't matter -- reports are identified by content.

Run:
    python scraper/runner.py

Press Ctrl+C to stop early.
"""

import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import parse

REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS_REL = "public/data/results.json"


def run(cmd: str) -> tuple[int, str, str]:
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=REPO_ROOT)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def sha256(path: Path) -> str:
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


def push_results(kind: str) -> None:
    code, out, _ = run(f"git diff --stat {RESULTS_REL}")
    _, untracked, _ = run(f"git ls-files --others --exclude-standard {RESULTS_REL}")
    if not out.strip() and not untracked.strip():
        print(f"  [{now_local()}] parsed [{kind}] -- no change in results, nothing to push")
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
        print(f"  [{now_local()}] pushed [{kind}] -- widget updates within ~1 minute")
    else:
        print(f"  ERROR: push failed: {err}")


def scoreboard() -> None:
    results = REPO_ROOT / RESULTS_REL
    if not results.exists():
        return
    with open(results) as f:
        data = json.load(f)
    e = data["election"]
    print(f"  precincts {e['precinctsReported']}/{e['precinctsTotal']}"
          f"  |  ballots {data['statistics']['ballotsCast']:,}"
          f"  |  status {e['status'].upper()}")
    marquee = [r for r in data["races"]
               if r["category"] in ("Statewide", "U.S. Congress") and r["candidates"]]
    for r in marquee[:6]:
        lead = r["candidates"][0]
        print(f"    [{r['party']}] {r['name']}: {lead['name']} leads ({lead['votes']:,})")


def main():
    config = parse.load_config()
    stop_at = datetime.fromisoformat(config["stopAtUtc"].replace("Z", "+00:00"))
    poll = int(config.get("pollSeconds", 5))
    watch_dir = preflight(config)

    print()
    print("=" * 62)
    print(f"  WPR ELECTION NIGHT RUNNER  --  {config['name']}")
    print(f"  Watching: {watch_dir}")
    print(f"  Runs until {stop_at.astimezone().strftime('%I:%M %p %b %d')} local, Ctrl+C to stop")
    print("=" * 62)
    print()
    print("  When the county posts results, download all three PDFs from:")
    print(f"    {config['reportPages']['results']}")
    print("  (Election Summary, Precinct Summary, Precincts Reported/Not Reported)")
    print("  Save them anywhere in the watched folder. Filenames don't matter.")
    print()

    seen: set[str] = set()
    last_heartbeat = 0.0

    while True:
        if datetime.now(timezone.utc) >= stop_at:
            print(f"\n  [{now_local()}] Reached stop time. Results are final on the widget. Good night!")
            break

        pdfs = sorted(watch_dir.glob("*.pdf"), key=lambda p: p.stat().st_mtime)
        for pdf in pdfs:
            try:
                digest = sha256(pdf)
            except OSError:
                continue  # still being written by the browser; next pass gets it
            if digest in seen:
                continue
            seen.add(digest)
            try:
                kind = parse.process_pdf(pdf, config)
            except ValueError as e:
                print(f"  [{now_local()}] skipped {pdf.name}: {e}")
                continue
            except Exception as e:
                print(f"  [{now_local()}] ERROR parsing {pdf.name}: {e}")
                continue
            push_results(kind)
            scoreboard()

        if time.time() - last_heartbeat > 120:
            print(f"  [{now_local()}] watching for new PDFs...")
            last_heartbeat = time.time()
        time.sleep(poll)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  Stopped. Goodbye!")
        sys.exit(0)
