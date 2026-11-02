# WynnFlip — zero-cost scheduled scanner (prototype)

**No Discord and no paid hosting.** The GitHub Action scans the WynnVentory API twice per hour, saves a bounded history into your **private** GitHub repository, and produces a static HTML dashboard as an Actions artifact.

## Setup
1. Create a private GitHub repository and upload **the contents of this folder** to its root (including hidden `.github` folder).
2. In repo **Settings → Secrets and variables → Actions**, create repository secret `WYNNVENTORY_API_KEY` with your private read-only key. Do not paste the key in source code.
3. Repo **Settings → Actions → General**: allow Actions; under Workflow permissions select **Read and write permissions** so the scheduled workflow can commit market snapshots. Some repos default to read-only. Save.
4. Go to **Actions → WynnFlip market scan → Run workflow**. Read the logs. If successful, the generated dashboard can be downloaded under the run's **Artifacts** as `wynnflip-dashboard`. Extract and open `index.html` (some browsers may block local JSON fetch; use the local server below).
5. Next scheduled runs are configured at :17 and :47 each hour UTC. Schedules can be delayed or skipped by GitHub. The first scan is a baseline; later scans identify newly observed listings.

## Open locally (optional)
Run `python -m http.server 8000 --directory site` and visit http://localhost:8000. The static site loads `site/market.json`.

## Public web hosting later
GitHub Pages on GitHub Free requires a **public** repo. Keep your scanner repo **private** and do not publish the API key. To provide friends a website, the `site/` output can be deployed separately to a free static hosting service (e.g. Cloudflare Pages), but that website is public unless you add access controls. Never expose private data in `market.json`.

## Limitations
- No Discord, accounts, private shared login, cloud live backend, or price-rise predictions yet.
- Data is crowdsourced and can be delayed. All profits are hypothetical gross spreads based on asking prices; listing availability is not confirmed.
- `MAX_PAGES=20` caps each run at 4,000 listings; increase carefully after verifying rate limits and action runtimes. Status explicitly shows partial coverage.
- The `seen.json` record tracks listings across the previous scan only. If an API snapshot drops and later rediscovers a listing, it may be counted as new again.
- Rolling data is committed to the private repository; history is capped at 25,000 variant snapshots, so this is a starter data store rather than a production database.
- GitHub Free private repos include monthly Actions limits; avoid enabling billing or upgrading. Check usage in GitHub Settings → Billing.
- The static dashboard contains only the latest newly detected deals; it is not a live trading terminal.

## API
Uses WynnVentory `/api/v2/market/listings` and `/api/v2/lootpools/current/items`. API scopes need `read:market` and optionally `read:lootpool`. API documentation: https://github.com/Wynnventory/WynnVentory_Web/blob/main/docs/API_V2.md
