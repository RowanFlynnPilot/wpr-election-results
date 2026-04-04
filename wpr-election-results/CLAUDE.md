# WPR Election Results Widget

## Overview
Real-time election results widget for Wausau Pilot & Review, covering Marathon County, Wisconsin elections. Designed for WordPress iframe embed on wausaupilotandreview.com.

## Architecture
```
scraper/
  parse_results.py    → Parses Marathon County election PDFs into JSON
  requirements.txt    → Python dependencies (pdfplumber, requests)
public/
  index.html          → Self-contained widget (HTML/CSS/JS, WPR-branded)
  data/
    election.json     → Current election results (updated by scraper)
    spring-primary-2026.json → Archived: Feb 17, 2026 primary
  assets/
    logo.jpeg         → WPR circular logo
.github/workflows/
  scrape.yml          → GitHub Actions: runs scraper on cron during election night
  deploy.yml          → GitHub Actions: deploys public/ to GitHub Pages
```

## Data Flow
1. Marathon County publishes PDF results at marathoncounty.gov/services/elections-voting/results
2. `parse_results.py` fetches & parses the PDF into structured JSON
3. JSON is committed to `public/data/election.json`
4. GitHub Pages serves the JSON
5. Widget fetches JSON on a 2-minute timer on election night

## Brand
- Colors: Teal #5CABA3 (from logo typewriter), Black #1a1a1a, Off-white #f8f7f5
- Fonts: Playfair Display (headlines), Source Sans 3 (body), JetBrains Mono (data)
- Bar chart palette: Cool steel-blue (#2b4c6f → #c6d8e4)
- Tagline: "More News. Less Fluff. All Local."

## Deployment
- GitHub Pages from `public/` directory
- WordPress embed: `<iframe src="https://wausaupilotandreview.github.io/wpr-election-results/" ...>`

## Election Night Workflow
1. Polls close at 8 PM CT
2. Enable the `scrape.yml` cron (every 3 minutes from 8 PM–midnight)
3. Monitor for new PDFs on the county results page
4. Scraper auto-commits updated JSON → Pages auto-deploys
5. Widget auto-refreshes for readers

## Key Files to Edit Per Election
- `scraper/parse_results.py` → Update PDF URLs and race definitions
- `public/data/election.json` → Can also be manually edited as fallback
- `public/index.html` → DATA_URL constant points to the JSON endpoint
