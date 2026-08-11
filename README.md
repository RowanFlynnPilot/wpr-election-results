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

## Embedding in WordPress

Paste this into a **Custom HTML** block (requires an editor/admin account —
WordPress strips `<script>` for lower roles). The widget reports its own
height, so the iframe resizes itself as results come in; no scrollbars.

```html
<iframe id="wpr-election-embed"
  src="https://rowanflynnpilot.github.io/wpr-election-results/"
  title="Marathon County election results — Wausau Pilot &amp; Review"
  style="width:100%;border:0;display:block;" height="900"
  scrolling="no" loading="lazy"></iframe>
<script>
window.addEventListener("message", function (e) {
  if (e.origin === "https://rowanflynnpilot.github.io" &&
      e.data && e.data.type === "wpr-embed-height") {
    document.getElementById("wpr-election-embed").style.height = e.data.height + "px";
  }
});
</script>
```

The same block can be reused on any page or post — the widget always shows
whatever `public/data/results.json` currently holds, so pre-election it
shows the waiting card and on election night it fills with live results
automatically. For a shareable demonstration with sample data, see
`/demo.html` on the Pages site.
