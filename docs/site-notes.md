# INE Store Scraper Reconnaissance & Site Notes

**Target Site:** `https://demo.inelabteamdev.com/`  
**Inspected:** September 2026  
**Status:** Live mock storefront designed specifically to benchmark scraper resilience.

---

## 1. Catalog & Search Architecture (Lightweight HTTP)

- **Endpoint:** `GET https://demo.inelabteamdev.com/api/catalog?page={page}&pageSize={pageSize}`
- **Pagination:** Fixed 50 pages, 20 items per page (1,000 total products).
- **Payload Schema:**
  ```json
  {
    "page": 1,
    "pageSize": 20,
    "pages": 50,
    "total": 1000,
    "items": [
      {
        "id": 69,
        "slug": "copperpot-sous-vide-wand-pro",
        "name": "Copperpot Sous-Vide Wand Pro",
        "brand": "Copperpot",
        "category": "Kitchen",
        "sku": "COP-10069",
        "description": "..."
      }
    ]
  }
  ```
- **Judgment:** Product search and catalog exploration can and should be done via lightweight HTTP requests against this public JSON API, with a 5-minute in-memory cache to prevent unnecessary load.
- **Product Detail API:** `GET /api/product/{id}` exists, but **omits price and live stock entirely**.

---

## 2. Product Detail & Price-Reveal Mechanism (Headless Playwright Required)

### 2.1 DOM & Telemetry Gating
On `/product/{id}`, the price is hidden in an idle container:
- **Idle selector:** `.price-block.price-idle`
- **Button:** `button[aria-label="Reveal price"]`
- **Telemetry requirement:** Gated by class `Ar({minMoves: 8, minDwellMs: 600})`.
- The button is rendered with `disabled={p !== null}` where `p` checks:
  1. `moves.length >= 8` (recorded at $\ge 40\text{ ms}$ intervals).
  2. Dwell time $\ge 600\text{ ms}$ over `.price-block`.
- **Action:** Scraper must simulate mouse movement over `.price-block` for $>600\text{ ms}$ across $\ge 8$ points before clicking.

### 2.2 Chaos Click Dropper (`Xn`)
- The click handler is wrapped in `Xn`:
  ```javascript
  function Xn(e) {
    return () => {
      if (Math.random() < 0.35) {
        if (Math.random() < 0.5) return; // 17.5% dropped completely
        window.setTimeout(e, 900);       // 17.5% delayed by 900ms
        return;
      }
      e();
    };
  }
  ```
- **Action:** If `.price-block` remains in `.price-idle` after 1.0s, the scraper detects a dropped click and issues a re-click.

### 2.3 WebAssembly Proof-of-Work & Token Exchange
- The browser fetches `/api/challenge`, which returns a base64 WASM binary, `salt`, and `difficulty`.
- The client executes `WebAssembly.compile()` / `instantiate()`, evaluates `exports.f`, and solves a SHA-256 hashcash loop (`"0".repeat(difficulty)`).
- The client POSTs the telemetry snapshot and PoW solution to `/api/session`, receives a Bearer token, and fetches `/api/products/{id}/price`.
- The encrypted payload is XOR-decrypted client-side and rendered into the DOM.
- **Judgment:** Running in a real browser (Playwright) automatically handles the WASM execution, PoW solving, and decryption natively.

---

## 3. DOM Honeypots & Decoy Selectors

When `.price-success` mounts into the DOM, multiple traps are present:

| Element | Selectors / Classes | Computed Styles | Role |
| :--- | :--- | :--- | :--- |
| **Decoy 1** | `.price-value` | `display: none;` | Fake lower price |
| **Decoy 2** | `.amount[data-price="true"]` | `display: none;` | Fake alternative price |
| **Retail MRP** | `.mr-*` (e.g. `mr-z6`) | `display: block; text-decoration: line-through;` | Original list price (strike-through) |
| **Deal Label** | `.sl-*` (e.g. `sl-z6`) | `display: block; text: "Deal price ..."` | Promotional banner |
| **Discount Badge** | `.bd-*` (e.g. `bd-z6`) | `display: block; text: "X% off"` | Percentage off |
| **Genuine Price** | `.pv-*` / tag `v` | `display: block; font-size: 38.4px; font-weight: 700;` | **Real current price** |

### Obfuscation in Genuine Price Text:
- May contain zero-width spaces (`\u200b`).
- May contain non-breaking spaces (`\u00a0`).
- May use full-width unicode numerals (`\uff10-\uff19`).
- May append trailing tax notes (`/- (incl. of all taxes)`).
- **Action:** Clean text with `NFKD` unicode normalization, strip `\u200b` and `\u00a0`, strip currency symbols and non-digits before parsing as numeric float.

---

## 4. Stock Badge Extraction

- **Selector:** `.stock-badge`
- **Variants:**
  - `.stock-badge.in-stock`: e.g. `"Selling fast — 33 left"`, `"Hurry, just 44 left"`, `"In stock · 12 left"`.
  - `.stock-badge.out-stock`: `"Out of stock"`.
- **Parsing:** `in_stock = "out of stock" not in badge_text.lower()`.

---

## 5. Retry & Failure Policy

- Up to 3 attempts with exponential backoff ($2\text{ s}, 4\text{ s}, 8\text{ s}$).
- Fresh page reload on every attempt.
- On failure: Record `ScrapeLog` with `status='failed'` and exact error; **never write wrong or empty data to `PriceHistory`**.
