# WPR Election Results Widget

Real-time Marathon County election results for [Wausau Pilot & Review](https://wausaupilotandreview.com), a nonprofit local news outlet covering central Wisconsin.

## Current Election: 2026 Spring Election — April 7, 2026

**Races covered:**
- Wisconsin Supreme Court (Chris Taylor vs. Maria S. Lazar)
- Marathon County Circuit Court, Branch 3 (Douglas Bauman vs. Michael D. Hughes)
- Marathon County Board of Supervisors (6 contested districts)
- Wausau Alderperson, District 6 (Kristin Slonski vs. Keene Winters)
- Mosinee Alderperson, Ward 4 (Todd Priest vs. Jocelyn Kuklinski Walters)
- D.C. Everest Area School Board (4 candidates for 2 seats)
- Antigo School Board (6 candidates for 3 seats)
- Marshfield School Board (6 candidates for 3 seats)
- Two statewide constitutional referendum questions

## How It Works

```
Marathon County PDFs → Python scraper → JSON → GitHub Pages → Widget auto-refreshes
```

1. Marathon County publishes election results as PDFs at [marathoncounty.gov](https://www.marathoncounty.gov/services/elections-voting/results)
2. Our scraper (`scraper/parse_results.py`) fetches and parses the PDFs into structured JSON
3. A GitHub Action runs the scraper every 3 minutes on election night
4. The widget (`public/index.html`) fetches the JSON and renders results with animated bars
5. Embedded on WPR via iframe in WordPress

## Quick Start

### Preview locally
```bash
cd public
python3 -m http.server 8000
# Open http://localhost:8000
```

### Run the scraper manually
```bash
pip install -r scraper/requirements.txt
python scraper/parse_results.py
```

### Election night
1. Uncomment the cron schedule in `.github/workflows/scrape.yml`
2. Push to `main` — the scraper will run every 3 minutes from 8 PM–midnight CT
3. Results auto-deploy to GitHub Pages

## Embedding in WordPress

```html
<iframe
  src="https://wausaupilotandreview.github.io/wpr-election-results/"
  width="100%"
  height="1200"
  style="border: none; max-width: 740px; margin: 0 auto; display: block;"
  title="Marathon County Election Results — Wausau Pilot & Review"
></iframe>
```

## Data Format

The widget consumes a single `election.json` file. See `public/data/election.json` for the full schema. Key structure:

```json
{
  "election": { "name": "...", "status": "live|final", "precinctsReported": 0, "precinctsTotal": 65 },
  "statistics": { "registeredVoters": 0, "ballotsCast": 0, "turnoutPct": 0 },
  "races": [
    {
      "id": "wi-supreme-court",
      "name": "Wisconsin Supreme Court",
      "category": "judicial",
      "seats": 1,
      "candidates": [{ "name": "Chris Taylor", "votes": 0 }],
      "precincts": { "reported": 0, "total": 65 },
      "wardData": []
    }
  ]
}
```

## Past Elections

Archived results are stored in `public/data/`:
- `spring-primary-2026.json` — February 17, 2026

## Credits

Built by [Wausau Pilot & Review](https://wausaupilotandreview.com) · Data source: [Marathon County Clerk's Office](https://www.marathoncounty.gov/services/elections-voting/results)
