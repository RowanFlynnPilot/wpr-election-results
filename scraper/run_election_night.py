"""
WPR Election Night Local Runner
================================
Run this script on election night to automatically scrape Marathon County
results and push updates to GitHub every 3 minutes.

Usage:
    python scraper/run_election_night.py

Keep this terminal window open. Press Ctrl+C to stop early.
Results will appear on the widget within 2 minutes of each update.
"""

import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

# ── CONFIG ──────────────────────────────────────────────────────
INTERVAL_SECONDS = 180          # Run every 3 minutes
STOP_AT_CT = "07:00"            # Stop at 7 AM Central Time (CT = UTC-5)
STOP_AT_UTC_HOUR = 12           # 7 AM CT = 12:00 UTC (CDT, UTC-5)
STOP_AT_UTC_DATE = (2026, 4, 8) # April 8, 2026
# ────────────────────────────────────────────────────────────────

STOP_AT = datetime(
    *STOP_AT_UTC_DATE, STOP_AT_UTC_HOUR, 0, 0, tzinfo=timezone.utc
)

REPO_ROOT = __file__.replace("\\", "/").rsplit("/scraper/", 1)[0].replace("/", "\\")


def now_ct():
    ct = timezone(timedelta(hours=-5))  # CDT
    return datetime.now(ct).strftime("%I:%M:%S %p CT")


def run(cmd, cwd=None):
    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True, cwd=cwd or REPO_ROOT
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def scrape(manual_url=None, precinct_url=None, status_url=None):
    """Run the scraper and return True if election.json changed."""
    print(f"\n{'─'*55}")
    print(f"  [{now_ct()}]  Running scraper...")
    print(f"{'─'*55}")

    cmd = 'python scraper\\parse_results.py'
    if manual_url:
        cmd += f' --url "{manual_url}"'
    if precinct_url:
        cmd += f' --precinct-url "{precinct_url}"'
    if status_url:
        cmd += f' --status-url "{status_url}"'

    code, out, err = run(cmd, cwd=REPO_ROOT)

    if out:
        for line in out.splitlines():
            print(f"  {line}")
    if err and code != 0:
        for line in err.splitlines():
            print(f"  ERR: {line}")

    if code != 0:
        return False

    # Check if election.json changed
    code2, diff_out, _ = run("git diff --stat public/data/election.json", cwd=REPO_ROOT)
    changed = bool(diff_out.strip())

    if changed:
        print(f"\n  Results updated! Committing and pushing...")
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        run(f'git add public/data/election.json', cwd=REPO_ROOT)
        code3, _, cerr = run(f'git commit -m "Update election results {ts}"', cwd=REPO_ROOT)
        if code3 == 0:
            run("git pull --rebase origin main", cwd=REPO_ROOT)
            code4, _, perr = run("git push origin main", cwd=REPO_ROOT)
            if code4 == 0:
                print(f"  Pushed to GitHub. Widget will update within 2 minutes.")
            else:
                print(f"  Push failed: {perr}")
        else:
            print(f"  Commit failed: {cerr}")
        return True
    else:
        print(f"  No changes in results yet.")
        return False


DOWNLOADS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
SUMMARY_FILE  = os.path.join(DOWNLOADS_DIR, "election-summary.pdf")
PRECINCT_FILE = os.path.join(DOWNLOADS_DIR, "precinct-summary.pdf")
STATUS_FILE   = os.path.join(DOWNLOADS_DIR, "precinct-status.pdf")


def check_downloads():
    """
    Check the downloads folder for manually saved PDFs.
    Returns (summary_path, precinct_path, status_path) — empty string if not present.
    """
    return (
        SUMMARY_FILE  if os.path.exists(SUMMARY_FILE)  else "",
        PRECINCT_FILE if os.path.exists(PRECINCT_FILE) else "",
        STATUS_FILE   if os.path.exists(STATUS_FILE)   else "",
    )


def cleanup_downloads():
    """Remove processed PDFs from the downloads folder."""
    for f in [SUMMARY_FILE, PRECINCT_FILE, STATUS_FILE]:
        if os.path.exists(f):
            os.remove(f)


def show_download_instructions():
    print()
    print("  ╔══════════════════════════════════════════════════════════════╗")
    print("  ║  ACTION NEEDED — download the PDFs from Marathon County:     ║")
    print("  ║                                                              ║")
    print("  ║  1. Open in browser:                                         ║")
    print("  ║     marathoncounty.gov/services/elections-voting/results     ║")
    print("  ║                                                              ║")
    print("  ║  2. Click 'Election Summary' → save as:                      ║")
    print(f"  ║     election-summary.pdf                                     ║")
    print("  ║                                                              ║")
    print("  ║  3. Click 'Precinct Summary' → save as:                      ║")
    print(f"  ║     precinct-summary.pdf                                     ║")
    print("  ║                                                              ║")
    print("  ║  4. Click 'Precincts Reported/Not Reported' → save as:       ║")
    print(f"  ║     precinct-status.pdf                                      ║")
    print("  ║                                                              ║")
    print(f"  ║  Save ALL files to:                                          ║")
    print(f"  ║  {DOWNLOADS_DIR[:60]:<60}║")
    print("  ║                                                              ║")
    print("  ║  Runner will detect them automatically each cycle.           ║")
    print("  ╚══════════════════════════════════════════════════════════════╝")
    print()


def main():
    print()
    print("╔══════════════════════════════════════════════════════╗")
    print("║     WPR Election Night Runner — April 7, 2026        ║")
    print("║     Running every 3 minutes until 7 AM CT            ║")
    print("║     Press Ctrl+C to stop early                       ║")
    print("╚══════════════════════════════════════════════════════╝")
    print()

    now_utc = datetime.now(timezone.utc)
    if now_utc >= STOP_AT:
        print("It's already past 7 AM CT — nothing to do.")
        sys.exit(0)

    run_count = 0

    while True:
        now_utc = datetime.now(timezone.utc)
        if now_utc >= STOP_AT:
            print(f"\n  It's 7 AM CT — stopping. Good night!")
            break

        run_count += 1
        print(f"\n  Run #{run_count}  |  {now_ct()}")

        # Try auto-discovery first
        changed = scrape()

        if not changed:
            # Check if user has manually downloaded PDFs
            summary_path, precinct_path, status_path = check_downloads()
            if summary_path:
                print(f"\n  Found downloaded PDFs — processing...")
                changed = scrape(manual_url=summary_path,
                                 precinct_url=precinct_path,
                                 status_url=status_path)
                if changed:
                    cleanup_downloads()
            else:
                show_download_instructions()

        # Calculate next run time
        now_utc = datetime.now(timezone.utc)
        next_run = now_utc + timedelta(seconds=INTERVAL_SECONDS)
        if next_run >= STOP_AT:
            print(f"\n  Next run would be after 7 AM — stopping after this run.")
            break

        # Countdown (only shown if we skipped the URL prompt)
        print()
        for remaining in range(INTERVAL_SECONDS, 0, -15):
            now_utc = datetime.now(timezone.utc)
            if now_utc >= STOP_AT:
                break
            next_str = (now_utc + timedelta(seconds=remaining)).astimezone(
                timezone(timedelta(hours=-5))
            ).strftime("%I:%M:%S %p CT")
            print(f"  Next run in {remaining}s  (at {next_str})        ", end="\r")
            time.sleep(15)

    print("\n  Done. Results are final on the widget.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  Stopped by user. Goodbye!")
        sys.exit(0)
