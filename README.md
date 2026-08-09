# WPR Election Live

Live election-night results widget for [Wausau Pilot & Review](https://wausaupilotandreview.com),
covering Marathon County, Wisconsin.

- `election.config.json` — the only file that changes between elections
- `scraper/runner.py` — election-night loop: watches a downloads folder, parses
  county PDFs, pushes updated results
- `scraper/parse.py` — Marathon County PDF parser (summary, ward detail, precinct status)
- `public/index.html` — embeddable results widget (GitHub Pages)
- `tests/test_parse.py` — parser tests against real 2024 Partisan Primary data

See `CLAUDE.md` for architecture and the election-night runbook.
