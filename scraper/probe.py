"""
Transport probe -- run once on the newsroom machine, paste the output back.

Tests which fetch engines the county's Cloudflare accepts from this
connection. The winner gets pinned as fetch.py's single transport; this
probe is a diagnostic, not a runtime component.

Setup (playwright is optional but answers everything in one run):
    python -m pip install curl_cffi
    python -m pip install playwright
    python -m playwright install chromium

Run:
    python scraper\\probe.py
"""

import json
import sys
from pathlib import Path

CONFIG = json.load(open(Path(__file__).resolve().parents[1] / "election.config.json"))
PAGE = CONFIG["resultsPage"]
MARKER = "Available Reports"          # real page content, absent from challenge pages
PDF_MARKER = "showpublisheddocument"  # at least one report link present

results = []


def record(name, ok, detail):
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def page_ok(text):
    return MARKER in text and PDF_MARKER in text


def probe_plain_requests():
    try:
        import requests
    except ImportError:
        record("plain requests", False, "not installed (fine -- expected to fail anyway)")
        return
    try:
        r = requests.get(PAGE, timeout=30, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0"})
        record("plain requests (baseline)", r.status_code == 200 and page_ok(r.text),
               f"HTTP {r.status_code}, real content: {page_ok(r.text)}")
    except Exception as e:
        record("plain requests (baseline)", False, str(e)[:100])


def probe_curl_cffi():
    try:
        from curl_cffi import requests as creq
    except ImportError:
        record("curl_cffi", False, "NOT INSTALLED -- python -m pip install curl_cffi")
        return
    try:
        s = creq.Session(impersonate="chrome")
        r = s.get(PAGE, timeout=30)
        if r.status_code != 200 or not page_ok(r.text):
            record("curl_cffi chrome-impersonation", False,
                   f"HTTP {r.status_code}, real content: {page_ok(r.text)}")
            return
        # Page passed -- now prove a PDF comes through the same session
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from fetch import discover_links
        links = discover_links(r.text, PAGE)
        pr = s.get(links["electionSummary"], headers={"Referer": PAGE}, timeout=30)
        is_pdf = pr.content.startswith(b"%PDF")
        record("curl_cffi chrome-impersonation", pr.status_code == 200 and is_pdf,
               f"page HTTP 200 + PDF fetch HTTP {pr.status_code}, "
               f"{len(pr.content)//1024} KB, PDF magic: {is_pdf}")
    except Exception as e:
        record("curl_cffi chrome-impersonation", False, str(e)[:120])


def probe_playwright(headless):
    label = f"playwright chromium ({'headless' if headless else 'headed'})"
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        record(label, False,
               "NOT INSTALLED -- python -m pip install playwright && python -m playwright install chromium")
        return
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            page = browser.new_page()
            resp = page.goto(PAGE, timeout=45000, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)  # let any JS challenge settle
            content = page.content()
            status = resp.status if resp else 0
            browser.close()
            record(label, status == 200 and page_ok(content),
                   f"HTTP {status}, real content: {page_ok(content)}")
    except Exception as e:
        record(label, False, str(e)[:120])


if __name__ == "__main__":
    print(f"\n  Probing {PAGE}\n")
    probe_plain_requests()
    probe_curl_cffi()
    probe_playwright(headless=True)
    probe_playwright(headless=False)
    print()
    winners = [n for n, ok, _ in results if ok and "baseline" not in n]
    if any(ok and "curl_cffi" in n for n, ok, _ in results):
        print("  VERDICT: curl_cffi works -- fetch.py is already pinned to it.")
        print("  Next: python scraper\\runner.py --fetch-once")
    elif winners:
        print(f"  VERDICT: only {winners[0]} gets through -- paste this output back")
        print("  and the transport gets swapped to it.")
    else:
        print("  VERDICT: nothing automated gets through from this connection.")
        print("  Paste this output back. Manual browser downloads remain fully working.")
