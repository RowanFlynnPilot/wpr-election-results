# WPR Election Live

Live election results for Wausau Pilot & Review, covering Marathon County,
Wisconsin. Successor to the April 2026 spring-election widget, rebuilt for the
August 11, 2026 Partisan Primary with party-aware parsing and ward drill-down.

## Architecture — one pipeline, two producers

```
                 ┌─ AUTO: fetch.py polls the county results page (90s),
                 │        re-discovers the 3 report links, downloads changes
runner.py ───────┤                                          → bytes ──┐
                 └─ MANUAL: browser downloads into the watched folder ┤
                                                                      ▼
                    hash-dedupe → parse.py → results.json → git push →
                    GitHub Pages → widget polls 60s → WordPress iframe
```

**The April lesson, stated precisely:** Marathon County's site (Granicus
behind Cloudflare) blocks *datacenter* IPs — GitHub Actions got 403 all
night — but serves *residential* traffic; the fetch worked once it ran
locally via scheduled PowerShell. Therefore the fetcher lives inside the
locally-run runner and MUST NEVER move into GitHub Actions or any cloud
runner. The fetcher re-discovers the three "Available Reports" links from
the results page every cycle because the county repoints those document
URLs between (and during) elections.

The manual path is not a bolted-on fallback: both producers feed the
identical ingest pipeline, and the human-with-a-browser producer is the
one that cannot be blocked. If auto-fetch ever fails, the runner says so
and manual saves continue working with zero mode-switching.

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

1. Before the night: `python scraper/runner.py --fetch-once` — a connectivity
   self-test that discovers the three report links and downloads each. Until
   the county repoints the links to primary documents, "would be skipped
   (PDF is not for '2026 Partisan Primary')" is the CORRECT output.
2. Election night: `python scraper/runner.py`. That's the whole job. It
   preflights git auth (dry-run push), then auto-fetches every 90 seconds,
   parses anything new, pushes only when numbers change, prints a scoreboard.
3. If the console reports "county fetch failed", download the three PDFs in
   a browser to the watched folder — same pipeline, no mode switch. Duplicate
   downloads (`file (1).pdf` etc.) are deduped by hash.
4. Order matters once: the first Election Summary must land before ward/status
   PDFs mean anything. The runner says so if they arrive early.

## The ward report's true format (verified against the real 2024 PDF)

The By Ward Detail report is 447 pages, ~13 per ward, ward name repeated as
a page header. Its race blocks are CONDENSED: name / Vote For N / TOTAL
VOTE % / candidate lines / at most a Write-In Totals line. No Total Votes
Cast, no Overvotes/Undervotes, no Precincts Reporting -- and the per-ward
Party Preference block has no result keywords at all. Block boundaries are
therefore "the first name-like line" (party prefix, 'Question', statistics),
which is also what makes 'DEM State Senator District 12' safely a name and
never a candidate. A spring (nonpartisan) election may print unprefixed
race names that look candidate-like ('...District 7'); if a future ward
parse raises on that, this is why -- bring the real PDF to the harness.

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
- Python deps: `pdfplumber`, `requests` (runtime), `reportlab` (tests only).
- The fetcher runs ONLY on the local machine. Never GitHub Actions — see above.
- Deploys via GitHub Pages workflow on pushes touching `public/**`.
- Widget fonts: Fraunces / Public Sans / JetBrains Mono (WPR design system:
  teal #3A867C, cream #F6F2E9).
