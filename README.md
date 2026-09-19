# INE Product Price Tracker

An automated price tracker built specifically to monitor the chaos mock storefront (`https://demo.inelabteamdev.com/`).

The mock store deliberately employs anti-scraping countermeasures:
1. Client-rendered single-page app (empty `<div id="root">`).
2. Gated price reveal requiring human mouse-dwell telemetry (`minMoves: 8`, `minDwellMs: 600`).
3. Chaos click dropper (`Xn` drops 17.5% of clicks and delays 17.5% by 900ms).
4. WebAssembly proof-of-work challenge and encrypted quote payload.
5. DOM honeypots (hidden decoy price spans, line-through retail MRPs, promotional badges).

---

## Architecture & Judgment

- **Lightweight Catalog Search (`/scraper/catalog.py`):** Products are discovered and searched via the store's public JSON API (`/api/catalog?page=N&pageSize=20`) with in-memory caching. A headless browser is not wasted on listing pages.
- **Headless Browser Detail Scraper (`/scraper/engine.py`):** Playwright is strictly used where genuinely required: executing the hover-dwell telemetry, surviving the click dropper, letting the browser solve the WASM/PoW challenge, and parsing the genuine revealed price from the DOM while filtering out hidden decoys.
- **Backend (`/backend`):** Django + Django REST Framework with Supabase Postgres (via `DATABASE_URL`) and local SQLite fallback.
- **External Cron Trigger (`POST /api/scrape/run`):** Authenticated with `X-Scrape-Secret` to accommodate free-tier sleeping backends (Render) without relying on fragile in-process loops.
- **Frontend (`/frontend`):** React + Vite dashboard featuring live catalog search, interactive price history charts (Recharts), toggleable data tables, and honest scrape audit logs.

---

## Local Development Setup

### Prerequisites
- Python 3.10+
- Node.js 18+ and npm
- Chromium or Microsoft Edge

### 1. Backend Setup
```bash
# Navigate to backend directory
cd backend

# Install Python dependencies
pip install -r requirements.txt

# Install Playwright browser dependencies
python -m playwright install chromium

# Run migrations (defaults to local SQLite db.sqlite3 if DATABASE_URL is not set)
python manage.py migrate

# (Optional) Run test suite
python manage.py test tracker

# Start development server
python manage.py runserver 8000
```
Backend API will be live at `http://127.0.0.1:8000/`.

### 2. Frontend Setup
```bash
# Navigate to frontend directory
cd frontend

# Install npm dependencies
npm install

# Start Vite dev server (automatically proxies /api to http://127.0.0.1:8000)
npm run dev
```
Frontend UI will be live at `http://localhost:3000/`.

### 3. CLI Scraper Standalone Run
You can run the scraper directly in headed mode to inspect the browser interaction and telemetry generation:
```bash
# Headed run on product 1
python -m scraper.engine --product 1 --headed

# Headless run on product 69
python -m scraper.engine --product 69
```

---

## Scraping Schedule & Cron Configuration

Because free-tier hosting (Render) sleeps after inactivity, the scraping engine is triggered externally rather than using an in-process loop.

### How it works:
1. Every tracked product has a `scrape_interval_minutes` (default: `120` minutes / 2 hours, configurable per product).
2. An external cron service (e.g. [cron-job.org](https://cron-job.org)) sends a `POST` request to `https://<backend-url>/api/scrape/run`.
3. The request must include the header:
   ```http
   X-Scrape-Secret: <SCRAPE_SHARED_SECRET>
   ```
4. The endpoint queries products due for scraping (`now - last_scraped_at >= interval`), scrapes each via Playwright, and commits logs and price records.
5. Recommended schedule on cron-job.org: **Every 15 minutes** (or every 2 hours).

### Keep-Warm Ping:
A lightweight ping endpoint is available at `GET /api/ping`. A cron job hitting this endpoint every 10 minutes keeps the Render instance warm before scrape runs.

---

## Environment Variables

### Backend (`backend/.env` / Render environment)

| Variable | Required | Default | Description |
| :--- | :--- | :--- | :--- |
| `DJANGO_SECRET_KEY` | Yes (Prod) | `insecure-dev-key...` | Cryptographic signing secret for Django. |
| `DJANGO_DEBUG` | No | `True` | Set to `False` in production. |
| `DATABASE_URL` | No | SQLite (`db.sqlite3`) | Supabase Postgres connection URI (e.g. `postgres://user:pass@host:5432/postgres`). |
| `SCRAPE_SHARED_SECRET`| Yes | `ine-tracker-cron-secret-2026` | Secret header key required to invoke `POST /api/scrape/run`. |
| `PLAYWRIGHT_BROWSERS_PATH`| Render only | `0` | Forces Playwright to use local container browser path on Render. |

### Frontend (`frontend/.env` / Vercel environment)

| Variable | Required | Default | Description |
| :--- | :--- | :--- | :--- |
| `VITE_API_BASE_URL` | Production | `""` (proxies `/api`) | Full URL of the backend API (e.g. `https://ine-price-tracker.onrender.com`). |

---

## How the Scraper Handles Failure

The scraper module (`/scraper/engine.py`) enforces strict error handling:

1. **Human Telemetry Simulation:** Moves cursor over `.price-block` in 12 steps over $>600\text{ ms}$ to satisfy `minMoves: 8` and `minDwellMs: 600`, preventing the button from staying disabled.
2. **Chaos Click Recovery:** Detects if `Xn` dropped the click (state remaining in `price-idle` after 1.0s) and automatically re-clicks.
3. **Decoy Filtering:** Computes live styles on DOM elements. Elements with `display: none` (`.price-value`, `.amount[data-price="true"]`) and `text-decoration: line-through` (retail MRP) are rejected.
4. **Text Normalization:** Strips zero-width spaces (`\u200b`), non-breaking spaces (`\u00a0`), and converts full-width unicode numerals (`\uff10-\uff19`) to standard ASCII digits.
5. **Retry with Fresh Reload:** On any network timeout or `.price-error` state, the scraper retries up to 3 times with exponential backoff ($2\text{s}, 4\text{s}, 8\text{s}$) with a clean page reload.
6. **Honest Logging Guarantee:**
   - Every scrape attempt creates a `ScrapeLog` row with status (`success`, `retried_then_success`, or `failed`), attempt count, duration, and error message.
   - `PriceHistory` is **only written when a verified price is parsed**. A failed scrape **never writes corrupt, empty, or placeholder data**.

Detailed technical findings and deobfuscated source excerpts are documented in [`docs/site-notes.md`](docs/site-notes.md).

---

## Live Deployment Links

- **Frontend (Vercel):** *Deploy from `frontend/` with `VITE_API_BASE_URL` pointing to backend.*
- **Backend (Render):** *Deploy blueprint using `render.yaml` or Docker/Python runtime with `Procfile`.*
- **Database (Supabase):** *Set `DATABASE_URL` in backend environment.*
