"""
Automated Marathon County report fetcher.

TRANSPORT: curl_cffi impersonating Chrome. The county's Cloudflare blocks by
TLS fingerprint, not just IP -- plain python-requests gets 403 even from a
residential connection (discovered Aug 2026). curl_cffi performs Chrome's
actual TLS handshake, which is what gets browser traffic through. The
transport is isolated in _get(); if the county's rules change, only this
file changes.

This fetcher runs ONLY on the newsroom machine. GitHub Actions and cloud
runners are on datacenter IP ranges Cloudflare blocks outright -- proven in
April 2026. Never move fetching into a workflow.

Each cycle re-reads the county results page and discovers the three
"Available Reports" links fresh -- the county repoints those document URLs
between (and sometimes during) elections, so hardcoded URLs would go stale.
Discovered PDFs are returned as bytes; nothing is written to disk.
"""

import hashlib
import re
from urllib.parse import urljoin

from curl_cffi import requests as creq

IMPERSONATE = "chrome"

SLOTS = {
    "electionSummary": "Election Summary",
    "precinctSummary": "Precinct Summary",
    "precinctStatus": "Precincts Reported/Not Reported",
}

ANCHOR_RE = re.compile(r"<a\s+[^>]*href\s*=\s*\"([^\"]+)\"[^>]*>(.*?)</a>",
                       re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")


class FetchError(Exception):
    """Any transport-level failure. The runner logs it and retries next cycle."""


def discover_links(html: str, base_url: str) -> dict:
    """Find the three Available Reports links by their visible anchor text.
    Returns {slot: absolute_url}. Raises FetchError if any slot is missing."""
    found = {}
    for href, inner in ANCHOR_RE.findall(html):
        text = " ".join(TAG_RE.sub("", inner).split())
        for slot, label in SLOTS.items():
            if slot not in found and text == label:
                found[slot] = urljoin(base_url, href)
    missing = [SLOTS[s] for s in SLOTS if s not in found]
    if missing:
        raise FetchError(f"results page is missing report links: {', '.join(missing)}")
    return found


class CountyFetcher:
    def __init__(self, results_page: str):
        self.results_page = results_page
        self.session = creq.Session(impersonate=IMPERSONATE)
        self._last_hash: dict[str, str] = {}

    def _get(self, url: str, referer: str | None = None):
        """The single transport seam. Raises FetchError on any failure."""
        headers = {"Referer": referer} if referer else {}
        try:
            r = self.session.get(url, headers=headers, timeout=30)
            r.raise_for_status()
        except Exception as e:
            raise FetchError(str(e)) from e
        return r

    def discover(self) -> dict:
        r = self._get(self.results_page)
        return discover_links(r.text, self.results_page)

    def fetch_changed(self) -> list[tuple[str, str, bytes]]:
        """One fetch cycle: discover links, download each slot, return only
        slots whose content changed since the last cycle as
        (slot, url, pdf_bytes). Raises FetchError; the runner logs and
        retries next cycle."""
        links = self.discover()
        changed = []
        for slot, url in links.items():
            r = self._get(url, referer=self.results_page)
            data = r.content
            if not data.startswith(b"%PDF"):
                raise FetchError(f"{SLOTS[slot]} link did not return a PDF "
                                 f"(got {r.headers.get('Content-Type', 'unknown')})")
            digest = hashlib.sha256(data).hexdigest()
            if self._last_hash.get(slot) == digest:
                continue
            self._last_hash[slot] = digest
            changed.append((slot, url, data))
        return changed
