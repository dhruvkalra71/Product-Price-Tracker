# INE Product Price Tracker

An automated, resilient product price tracker built specifically to monitor the chaos mock storefront (`https://demo.inelabteamdev.com/`).

The mock store deliberately employs anti-scraping countermeasures and simulated network instability:
1. **Client-Rendered Single-Page App**: Empty `<div id="root">` rendered by React in the browser.
2. **Gated Price Reveal**: Requires human mouse-dwell telemetry (`minMoves: 8`, `minDwellMs: 600`) over `.price-block`.
3. **Chaos Click Dropper (`Xn`)**: Drops 17.5% of clicks completely and delays 17.5% of clicks by 900ms.
4. **Intermittent Server 503s & In-Page Retries**: `/api/products/{id}/price` intermittently returns HTTP 503, triggering up to 6 internal retries with backoff in the storefront's in-page script.
5. **WebAssembly Proof-of-Work Challenge**: Evaluates a SHA-256 hashcash challenge before granting quote decryption keys.
6. **DOM Honeypots & Decoys**: Fake hidden price spans (`display: none`), line-through retail MRPs, and promotional badges.

---

## Architecture & Engineering Highlights

```
                       ┌──────────────────────────────┐
                       │   React + Vite Frontend UI   │
                       │ (Live Search, Charts, Logs)  │
                       └──────────────┬───────────────┘
                                      │ REST API / Adaptive Polling
                                      ▼
                       ┌──────────────────────────────┐
                       │    Django REST API Backend   │
                       └──────────────┬───────────────┘
                                      │
              ┌───────────────────────┴───────────────────────┐
              ▼                                               ▼
┌───────────────────────────┐                   ┌───────────────────────────┐
│   PostgreSQL / SQLite     │                   │  Playwright Engine Worker │
│  - Products & PriceHistory│                   │  - Telemetry generation   │
│  - Audit ScrapeLogs       │                   │  - Chaos click recovery   │
│  - FIFO ScrapeJob Queue   │◄──────────────────┤  - WASM / Decoy filters   │
│  - Postgres Advisory Lock │                   │  - 35s in-page 503 buffer │
└───────────────────────────┘                   └───────────────────────────┘
```

- **Database-Backed Sequential FIFO Queue (`ScrapeJob`):** When multiple products are tracked in batch, requests do not contend or fail with arbitrary timeouts. Initial scrapes are enqueued in a persistent database queue (`status="queued"`), processed in strict FIFO order, and surfaced in the frontend with live queue positions (`⏳ Queued (Position #N)...`).
- **Cross-Process Concurrency Locking (`ScrapeLock`):** Serializes Playwright browser launches across all Gunicorn worker processes via PostgreSQL session advisory locks (`pg_try_advisory_lock` / `pg_advisory_unlock`), preventing memory exhaustion on resource-constrained containers.
- **Configurable Concurrency (`SCRAPE_MAX_CONCURRENT`):** Defaults to `1` for 512MB hosts. When scaled up, batch processing seamlessly utilizes `scrape_products_batch` with shared browser contexts.
- **Orphaned Product Auto-Healing:** Products tracked prior to worker deployments or during container restarts are automatically detected by `/api/products` and auto-enqueued for initial price resolution.
- **Lightweight Catalog Search (`/scraper/catalog.py`):** Products are searched via the store's public JSON API (`/api/catalog?page=N&pageSize=20`) with in-memory caching—a headless browser is never wasted on listing pages.
- **Headless Browser Detail Scraper (`/scraper/engine.py`):** Playwright is strictly used where genuinely required: simulating cursor dwell, surviving click drops/delays, waiting for store 503 in-page retries, and extracting verified prices while ignoring decoys.
- **External Cron Trigger (`POST /api/scrape/run`):** Authenticated with `X-Scrape-Secret` to accommodate free-tier sleeping backends (Render) without relying on fragile in-process loops.
- **Honest Logging Guarantee:** Every scrape attempt creates a detailed `ScrapeLog` row. `PriceHistory` is **only written when a verified price is parsed**—failed scrapes never write corrupt or empty data.

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

# Run migrations (defaults to local SQLite db.sqlite3 if DATABASE_URL is unset)
python manage.py migrate

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

### 3. Running Unit Tests
```bash
# Run backend API and queue tests (25 tests)
cd backend
python manage.py test tracker

# Run Playwright scraper integration tests (6 tests)
cd ..
python -m unittest scraper/test_engine_playwright.py

# Verify frontend production build
cd frontend
npm run build
```

### 4. CLI Scraper Standalone Run
You can run the Playwright scraper directly from the terminal to inspect telemetry generation and price reveal:
```bash
# Headed run on product 1 (launches browser window)
python -m scraper.engine --product 1 --headed

# Headless run on product 708
python -m scraper.engine --product 708
```

---

## Scraping Schedule & Cron Configuration

Because free-tier hosting (Render) sleeps after inactivity, the recurring scrape cycle is driven externally:

### 1. Recurring Price Scrapes (`POST /api/scrape/run`)
- **Schedule:** Trigger every **15 minutes** (or up to every 2 hours) via [cron-job.org](https://cron-job.org) or GitHub Actions.
- **Method & URL:** `POST https://<backend-url>/api/scrape/run`
- **Authentication Header:**
  ```http
  X-Scrape-Secret: <SCRAPE_SHARED_SECRET>
  ```
- **How it works:**
  1. The endpoint validates the secret key.
  2. Queries all tracked products where `last_scraped_at` is older than their configured `scrape_interval_minutes` (default: 120m).
  3. Checks `ScrapeLock`. If another scrape run is in progress, returns `409 Conflict` (`already_running`) to prevent overlapping runs.
  4. Returns `202 Accepted` immediately with the `due_count`, running the batch in the background.

### 2. Keep-Warm Ping (`GET /api/ping`)
- **Schedule:** Trigger every **10 minutes** via a separate job on [cron-job.org](https://cron-job.org).
- **Method & URL:** `GET https://<backend-url>/api/ping`
- **Purpose:** Keeps the Render container warm so scheduled scraping requests on `/api/scrape/run` do not experience cold-start spin-up timeouts.

---

## Environment Variables

### Backend (`backend/.env` or Render Dashboard)

| Variable | Required | Default | Description |
| :--- | :--- | :--- | :--- |
| `DJANGO_SECRET_KEY` | Yes (Prod) | `insecure-dev-key...` | Django cryptographic signing secret. |
| `DJANGO_DEBUG` | No | `False` (in prod) | Set to `False` in production. |
| `DATABASE_URL` | Recommended | SQLite (`db.sqlite3`) | Supabase Session Pooler URI on port 5432 (e.g. `postgresql://postgres.<ref>:<pass>@aws-0-<region>.pooler.supabase.com:5432/postgres`). Direct `db.<ref>.supabase.co` is IPv6-only and will fail on Render with "Network unreachable". |
| `SCRAPE_SHARED_SECRET` | Yes | `ine-tracker-cron-secret-2026` | Shared secret header required to invoke `POST /api/scrape/run`. |
| `SCRAPE_MAX_CONCURRENT` | No | `1` | Max concurrent browser sessions (default: `1` sequential for memory stability on 512MB RAM). |
| `CONN_MAX_AGE` | No | `0` | Connection max age (`0` recommended for transaction/session poolers). |
| `PLAYWRIGHT_BROWSERS_PATH`| Render only | `/ms-playwright` | Forces Playwright to locate the container's installed browser binaries. |

### Frontend (`frontend/.env` or Vercel Dashboard)

| Variable | Required | Default | Description |
| :--- | :--- | :--- | :--- |
| `VITE_API_BASE_URL` | Production | `""` (proxies `/api`) | Public backend API URL (e.g. `https://ine-product-price-tracker-backend.onrender.com`). |

---

## How the Scraper Handles Failure

The scraper module (`/scraper/engine.py`) and queue infrastructure enforce strict error handling, anti-scraping countermeasures, and resilience guarantees:

1. **Human Telemetry Simulation:** Moves the cursor over `.price-block` in 10–12 discrete steps over $>600\text{ ms}$ to satisfy the store's `minMoves: 8` and `minDwellMs: 600` gating telemetry, preventing the reveal button from staying disabled.
2. **Chaos Click Dropper & Delay Recovery (`Xn`):** The store's click handler delays 17.5% of clicks by 900ms and drops 17.5% entirely. The scraper checks state with an **1100ms threshold** so delayed clicks fire naturally, and automatically re-clicks up to 2 times if the container remains in `.price-idle`.
3. **Store 503 In-Page Retries:** When `/api/products/{id}/price` returns `HTTP 503 (Service Unavailable)`, the storefront's in-page React component executes up to 6 internal retries with backoff. Playwright waits up to **35 seconds** (`page.wait_for_selector(".price-success, .price-error", timeout=35000)`) so the store's internal retries succeed without Playwright timing out prematurely.
4. **Decoy & MRP Filtering:** Computes live styles on DOM elements in page context. Elements with `display: none` (`.price-value`, `.amount[data-price="true"]`), line-through retail MRPs (`text-decoration: line-through`), and promotional deal badges are rejected.
5. **Price Font-Signature Selection & Text Normalization:** Selects genuine price elements by font weight ($\ge 700$) and font size ($2.4\text{rem} \approx 38.4\text{px}$). Cleans unicode quirks including full-width digits (`\uff10-\uff19`), non-breaking spaces (`\u00a0`), zero-width spaces (`\u200b`), and trailing tax annotations (`/- (incl. of all taxes)`).
6. **Retry with Fresh Page Reload:** On any network error, browser crash, or `.price-error` state, the scraper retries up to 3 times with exponential backoff ($2\text{s}, 4\text{s}, 8\text{s}$) with a clean page reload.
7. **Worker Crash & Interruption Recovery:** If a server process terminates while a scrape job is marked `status="running"`, the worker resets stale jobs older than 5 minutes to `status="failed"` upon restart.
8. **Orphaned Product Auto-Healing:** Products tracked prior to deployments or container restarts that lack price history and pending jobs are automatically detected by `/api/products` and auto-enqueued for initial scraping.
9. **Adaptive Polling:** The React frontend polls every **1.5s** while any product is `queued` or `running`, and drops to **8s** when all products are idle.
10. **Honest Logging Guarantee:**
    - Every scrape attempt creates a detailed `ScrapeLog` row with status (`success`, `retried_then_success`, or `failed`), attempt count, duration, exact error message, and DOM telemetry.
    - `PriceHistory` is **only written when a verified price is parsed**. A failed scrape **never writes corrupt, empty, or placeholder data**.

Detailed technical findings and deobfuscated storefront excerpts are documented in [`docs/site-notes.md`](docs/site-notes.md).

---

## Deployment Guide

- **Frontend (Vercel):**
  - Root directory: `frontend`
  - Build command: `npm run build`
  - Output directory: `dist`
  - Environment variables: `VITE_API_BASE_URL=https://<your-backend>.onrender.com`
- **Backend (Render):**
  - Blueprint: uses [`render.yaml`](render.yaml) or Docker runtime with [`Dockerfile`](Dockerfile).
  - Automatically runs `python backend/manage.py migrate` on container start.
  - Workers configured: `gunicorn tracker_project.wsgi:application --workers 2 --threads 4`.
- **Database (Supabase):**
  - Set `DATABASE_URL` to Supabase connection pooler port 5432 in backend environment.


