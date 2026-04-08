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


def scrape(manual_url=None):
    """Run the scraper and return True if election.json changed."""
    print(f"\n{'─'*55}")
    print(f"  [{now_ct()}]  Running scraper...")
    print(f"{'─'*55}")

    cmd = 'python scraper\\parse_results.py'
    if manual_url:
        cmd += f' --url "{manual_url}"'

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


def ask_for_url():
    """
    Prompt the user to paste a PDF URL. Returns the URL string or empty string.
    Times out after INTERVAL_SECONDS so the loop keeps running hands-free.
    """
    import threading, queue
    q = queue.Queue()

    print()
    print("  ┌─────────────────────────────────────────────────────┐")
    print("  │  Open this page in your browser:                    │")
    print("  │  marathoncounty.gov/services/elections-voting/results│")
    print("  │                                                     │")
    print("  │  When a PDF is posted, right-click it → Copy link  │")
    print("  │  then paste the URL below and press Enter.          │")
    print("  │  (Just press Enter to skip and retry automatically) │")
    print("  └─────────────────────────────────────────────────────┘")
    print()

    def _input(q):
        try:
            val = input("  → PDF URL (or Enter to skip): ").strip()
            q.put(val)
        except Exception:
            q.put("")

    t = threading.Thread(target=_input, args=(q,), daemon=True)
    t.start()
    t.join(timeout=INTERVAL_SECONDS)

    if not q.empty():
        return q.get()
    print("\n  (No URL entered — retrying auto-discovery next cycle)")
    return ""


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

        # Try auto-discovery first; if it fails, prompt for manual URL
        changed = scrape()
        if not changed:
            manual_url = ask_for_url()
            if manual_url:
                print(f"\n  Trying with provided URL...")
                scrape(manual_url=manual_url)

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
