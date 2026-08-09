# WPR Election Live

Live election results for Wausau Pilot & Review, covering Marathon County,
Wisconsin. Successor to the April 2026 spring-election widget, rebuilt for the
August 11, 2026 Partisan Primary with party-aware parsing and ward drill-down.

## Architecture — one correct path

```
County posts PDFs → Rowan downloads them (browser) → runner.py watches the
downloads folder → parse.py → public/data/results.json → git push →
GitHub Pages → widget (public/index.html) polls every 60s → WordPress iframe
```

There is intentionally **no automated fetch**. Marathon County's site
(Granicus/CivicPlus behind Cloudflare) 403s non-browser requests. The April
tool stacked three ingestion fallbacks; the only one that worked on election
night was manually saved PDFs, so that is now the only path. A human with a
browser is the fetch layer, and everything downstream is instant.

## Key design decisions

- **One config file.** `election.config.json` is the only file that changes
  between elections: name, dates, watch folder, stop time. The `name` field
  must match the election title printed in the county's PDFs exactly — it's
  also the guard that rejects stray PDFs in the downloads folder.
- **Content-based classification.** Report type (summary / ward detail /
  precinct status) is sniffed from page-1 text. Filenames are irrelevant.
- **Party-aware races.** Partisan primary race names carry party prefixes
  (`DEM`, `REP`, `CON`, `LIB`, `WGN`); referendum questions are unprefixed
  (`Question 1: ...`, stored as party `REF`). The `Party Preference Section`
  is captured separately as `partyPreference`.
- **Per-race precinct reporting.** Partisan reports give each race its own
  `Precincts Reporting X of Y` (district races report 7 of 7 while countywide
  races report 88 of 88). Never apply the global number to individual races.
- **Extraction-order tolerance.** PDF text extractors emit the race name
  either before or after the `Vote For N` line. The parser detects the mode
  once per PDF from evidence and refuses mixed signals. Both modes are under
  test.
- **Fail fast, loudly.** Candidate votes + write-ins must equal `Total Votes
  Cast` for every race or the parse raises. Unknown lines inside race blocks
  raise. The runner prints skips/errors and keeps watching; it never publishes
  a bad parse.
- **Vanilla single-file widget.** No build step: election-night pushes are
  data-only. The widget shows a leader ✓ only when that race's own precincts
  are complete, and labels everything unofficial. It posts its height to the
  parent frame (`{type:"wpr-embed-height"}`) for iframe auto-sizing.

## Election-night runbook

1. `python scraper/runner.py` (it preflights git auth with a dry-run push and
   refuses to start if pushing would fail — fix auth Monday, not Tuesday).
2. After polls close (~8 p.m.), open the county results page it prints,
   download the three PDFs (Election Summary, Precinct Summary, Precincts
   Reported/Not Reported) to the watched folder. Repeat whenever the county
   updates them — every 15–30 min, typically.
3. The runner parses on arrival, pushes only when numbers change, and prints a
   scoreboard. Duplicate downloads (`file (1).pdf` etc.) are deduped by hash.
4. Order matters once: the first Election Summary must land before ward/status
   PDFs mean anything. The runner says so if they arrive early.

## Validation before election night

The parser is tested against the county's real 2024 Partisan Primary summary
(embedded fixture, `python tests/test_parse.py`). The ward-detail format is
built from the April parser's proven structure but has NOT been checked
against a real partisan ward PDF. Before Tuesday: download the 2024 Partisan
Primary "By Ward Detail" PDF from the county results page (historical section)
and run:

    python tests/test_parse.py --real path\to\that.pdf

If it parses and the numbers match the PDF, we're set. If it raises, the error
message names the exact line that didn't fit — bring it back to Claude.

## Per-election checklist (next election)

1. Edit `election.config.json`: name, date, displayDate, stopAtUtc.
2. Reset `public/data/results.json` to the pre-election skeleton (status
   "pre", empty races).
3. Commit and push. Done — nothing else references election specifics.

## Stack notes

- Windows / PowerShell 5.1; runner output is ASCII-only on purpose.
- Python deps: `pdfplumber` (runtime), `reportlab` (tests only).
- Deploys via GitHub Pages workflow on pushes touching `public/**`.
- Widget fonts: Fraunces / Public Sans / JetBrains Mono (WPR design system:
  teal #3A867C, cream #F6F2E9).
