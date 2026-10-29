# Summit County foreclosure leads

Its own repo (separate from the Franklin / Cuyahoga scrapers). Source: Summit County Clerk "new civil filings"
feed, https://newcivilfilings.summitoh.net/ (JavaScript-rendered, so Playwright; no login or CAPTCHA).

Twice daily, GitHub Actions reads the feed, keeps rows whose category is Foreclosure, builds leads, and publishes
`dashboard/` to GitHub Pages (`dashboard/summit.json`, `dashboard/summit_ghl_export.csv`, `dashboard/index.html`).

- The feed has no case number or address. `case_number` is `SUM-` + a hash of the case name and time added.
- Owner = defendants. Property/mailing address come from `data/summit_parcels.csv` (Summit County Fiscal Officer export)
  by unique owner-name match (flag "Address from name match"). Leads without an address show on the dashboard but are
  left out of the GHL CSV.
- Rows still "Pending Review" at the Clerk are flagged; the Clerk can reject them later.

## Status
Parser written from the page text only. Run `src/discover_summit.py` once locally and send `data/debug_summit/` so it can
be confirmed and the "Get Docs" step (complaint PDF -> address, amount, rate, loan type) can be built.

## Local
    pip install -r requirements.txt
    python -m playwright install chromium
    python -m pytest -q tests
    cd src && python run_pipeline.py --headed
