import argparse
import asyncio
import json
import random
import re
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass
from typing import List, Optional

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

@dataclass
class ScrapeResult:
    source_product_id: str
    product_name: str
    price: Optional[float]
    currency: Optional[str]
    in_stock: Optional[bool]
    stock_raw: Optional[str]
    attempts: int
    status: str  # "success", "retried_then_success", "failed"
    error_message: Optional[str]
    logs: List[str]
    elapsed_seconds: float
    overlay_detected: bool = False

def clean_price_string(raw: str) -> Optional[float]:
    if not raw:
        return None
    # 1. Normalize unicode (handles full-width digits etc)
    s = unicodedata.normalize("NFKD", raw)
    # 2. Strip currency abbreviations (e.g. Rs. or INR) so trailing periods are not mistaken for decimals
    s = re.sub(r"\b(rs|inr)\b\.?", "", s, flags=re.IGNORECASE)
    # 3. Keep only digits, commas, and dots
    s = re.sub(r"[^\d,\.]", "", s).strip()
    if not s:
        return None
    # 4. Handle Indian numbering vs European decimals
    s = s.replace(".", "").replace(",", ".") if ("," in s and "." in s and s.rfind(",") > s.rfind(".")) else s.replace(",", "")
    try:
        val = float(s)
        return val if val > 0 else None
    except ValueError:
        return None

COOKIE_SUPPRESSION_CSS = ".cookie-overlay { display: none !important; pointer-events: none !important; }"

async def _neutralize_cookie_overlay(page, log) -> bool:
    """
    Checks for .cookie-overlay in DOM, records telemetry, and ensures
    anti-overlay CSS is applied so the overlay never intercepts pointer events.

    Why both context.add_init_script and page.add_style_tag exist:
    1. Primary defense: context.add_init_script injects '#anti-overlay-fix' before
       any document script executes on every navigation and reload.
    2. Fallback defense: if _scrape_with_page is invoked with a bare page
       lacking the context init script (e.g. in tests or direct custom contexts),
       or if SPA framework hydration wipes the document head, this fallback
       injects the style tag.
    3. De-duplication: we query document.getElementById('anti-overlay-fix') first,
       avoiding redundant <style> tags from accumulating across attempts.
    """
    detected = False
    try:
        count = await page.locator(".cookie-overlay").count()
        detected = count > 0
        if detected:
            log(f"[TELEMETRY] Cookie overlay detected in DOM ({count} element(s)) - neutralized via style injection")
    except Exception as e:
        log(f"[WARNING] Failed to query cookie overlay presence: {e}")

    try:
        has_style = await page.evaluate("() => !!document.getElementById('anti-overlay-fix')")
        if not has_style:
            await page.add_style_tag(content=COOKIE_SUPPRESSION_CSS)
            await page.evaluate("""() => {
                const s = Array.from(document.querySelectorAll('style')).pop();
                if (s && !s.id) s.id = 'anti-overlay-fix';
            }""")
    except Exception as e:
        log(f"[WARNING] Failed to inject overlay suppression styles: {e}")

    return detected

async def _scrape_with_page(
    page,
    source_product_id: str,
    max_retries: int = 3,
    base_url: str = "https://demo.inelabteamdev.com"
) -> ScrapeResult:
    logs: List[str] = []
    def log(msg: str):
        ts = time.strftime("%H:%M:%S")
        entry = f"[{ts}] [Product {source_product_id}] {msg}"
        logs.append(entry)
        print(entry, flush=True)

    t_start = time.time()
    log(f"Starting scrape for product_id={source_product_id}")

    product_name = ""
    last_error: Optional[str] = None
    target_url = f"{base_url}/product/{source_product_id}"
    overlay_detected = False

    for attempt in range(1, max_retries + 1):
        log(f"--- Attempt {attempt} of {max_retries} ---")
        try:
            log(f"Navigating to {target_url} ...")
            await page.goto(target_url, wait_until="domcontentloaded", timeout=20000)

            # Pre-emptive neutralization & detection on load/reload
            if await _neutralize_cookie_overlay(page, log):
                overlay_detected = True

            # 1. Wait for product title
            await page.wait_for_selector("h1", timeout=10000)
            product_name = (await page.locator("h1").inner_text()).strip()
            log(f"Page loaded. Product: '{product_name}'")

            # 2. Locate price-block
            price_block = page.locator(".price-block")
            await price_block.wait_for(state="visible", timeout=10000)
            box = await price_block.bounding_box()
            if not box:
                raise RuntimeError("Price block has zero bounding box")

            # 3. Simulate human telemetry (minMoves: 8, minDwellMs: 600 required by Ar)
            log("Generating cursor movements over price-block to satisfy dwell telemetry...")
            start_x = box["x"] + box["width"] * 0.2
            start_y = box["y"] + box["height"] * 0.3
            await page.mouse.move(start_x, start_y)
            
            for step in range(12):
                await asyncio.sleep(0.06)
                cur_x = start_x + (step * 8)
                cur_y = start_y + ((step % 3) * 6)
                await page.mouse.move(cur_x, cur_y)

            reveal_btn = page.locator('button[aria-label="Reveal price"]')
            is_disabled = await reveal_btn.is_disabled()
            if is_disabled:
                log("Reveal button still disabled by telemetry check, extending dwell time...")
                for step in range(8):
                    await asyncio.sleep(0.08)
                    await page.mouse.move(start_x - (step * 6), start_y + ((step % 2) * 4))

            # Defensive re-check before click (cookie banner can trigger asynchronously on timer during dwell)
            if await _neutralize_cookie_overlay(page, log):
                overlay_detected = True

            # 4. Click reveal button
            log("Clicking 'Reveal price' button...")
            await reveal_btn.click()

            # 5. Handle chaos click dropper (Xn drops 17.5% of clicks)
            try:
                await page.wait_for_function("() => !document.querySelector('.price-block')?.classList.contains('price-idle')", timeout=1500)
                log("Price block transitioned out of idle state")
            except Exception:
                log("[CHAOS DETECTED] Click was dropped by store chaos logic! Re-clicking...")
                await reveal_btn.click()

            # 6. Wait for price-success or price-error
            log("Waiting for price quote resolution (WASM + API exchange)...")
            await page.wait_for_selector(".price-success, .price-error", timeout=18000)
            final_classes = await price_block.get_attribute("class") or ""

            if "price-error" in final_classes:
                err_sub = await page.locator(".price-substatus").inner_text()
                log(f"[CHAOS ERROR] Store returned error state: {err_sub}")
                raise RuntimeError(f"Store chaos error: {err_sub}")

            if "price-success" in final_classes:
                log("Price revealed successfully in DOM!")
                
                # 7. Extract DOM details safely in page context
                dom_data = await page.evaluate('''() => {
                    const block = document.querySelector(".price-block");
                    const main = block.querySelector(".price-main") || block;
                    const candidates = [];
                    for (const el of main.children) {
                        const cs = window.getComputedStyle(el);
                        // Skip hidden elements (decoys)
                        if (cs.display === "none" || cs.visibility === "hidden" || cs.opacity === "0") continue;
                        // Skip line-through (MRP)
                        if (cs.textDecorationLine.includes("line-through")) continue;
                        const txt = (el.innerText || el.textContent || "").trim();
                        // Skip discount badges or deal label
                        if (txt.toLowerCase().includes("deal price") || txt.toLowerCase().includes("off")) continue;
                        if (/\\d/.test(txt)) {
                            candidates.push({
                                text: txt,
                                fontSize: cs.fontSize,
                                fontWeight: cs.fontWeight
                            });
                        }
                    }
                    const stockEl = block.querySelector(".stock-badge");
                    const stockText = stockEl ? (stockEl.innerText || "").trim() : "";
                    return { candidates, stockText };
                }''')

                candidates = dom_data.get("candidates", [])
                stock_raw = dom_data.get("stockText", "")
                log(f"Candidate price elements: {candidates}")
                log(f"Raw stock badge: '{stock_raw}'")

                if not candidates:
                    raise RuntimeError("No visible price candidate found after reveal")

                # The real price element has large font (font-size 2.4rem ~ 38px, font-weight 700)
                chosen_cand = candidates[0]["text"]
                price_val = clean_price_string(chosen_cand)
                
                if price_val is None:
                    raise ValueError(f"Failed to parse valid numeric price from '{chosen_cand}'")

                in_stock = "out of stock" not in stock_raw.lower() if stock_raw else None
                status = "success" if attempt == 1 else "retried_then_success"
                
                log(f"Extraction validated: Price={price_val}, InStock={in_stock} on attempt {attempt}")
                
                return ScrapeResult(
                    source_product_id=str(source_product_id),
                    product_name=product_name,
                    price=price_val,
                    currency="INR",
                    in_stock=in_stock,
                    stock_raw=stock_raw,
                    attempts=attempt,
                    status=status,
                    error_message=None,
                    logs=logs,
                    elapsed_seconds=round(time.time() - t_start, 2),
                    overlay_detected=overlay_detected
                )

        except Exception as e:
            last_error = str(e)
            log(f"Attempt {attempt} failed: {last_error}")
            base_delay = 2
            max_delay = 20
            if attempt < max_retries:
                exponential = base_delay * (2 ** (attempt - 1))  # 2, 4, 8 for attempts 1, 2, 3
                backoff = random.uniform(0, min(max_delay, exponential))
                log(f"Backing off for {backoff:.2f}s before retry (full jitter)...")
                await asyncio.sleep(backoff)

    log(f"All {max_retries} attempts exhausted without success.")
    return ScrapeResult(
        source_product_id=str(source_product_id),
        product_name=product_name,
        price=None,
        currency=None,
        in_stock=None,
        stock_raw=None,
        attempts=max_retries,
        status="failed",
        error_message=last_error or "Retries exhausted",
        logs=logs,
        elapsed_seconds=round(time.time() - t_start, 2),
        overlay_detected=overlay_detected
    )

async def scrape_product_async(
    source_product_id: str,
    headed: bool = False,
    max_retries: int = 3,
    base_url: str = "https://demo.inelabteamdev.com"
) -> ScrapeResult:
    results = await scrape_products_batch(
        [str(source_product_id)],
        concurrency=1,
        max_retries=max_retries,
        base_url=base_url,
        headless=not headed
    )
    return results[str(source_product_id)]

async def scrape_products_batch(
    source_product_ids: List[str],
    concurrency: int = 4,
    max_retries: int = 3,
    base_url: str = "https://demo.inelabteamdev.com",
    headless: bool = True
) -> dict:
    from playwright.async_api import async_playwright

    results = {}
    if not source_product_ids:
        return results

    sem = asyncio.Semaphore(concurrency)

    async with async_playwright() as p:
        browser = None
        if sys.platform == "win32":
            try:
                browser = await p.chromium.launch(channel="msedge", headless=headless)
            except Exception:
                pass
        if browser is None:
            browser = await p.chromium.launch(headless=headless)

        async def scrape_one(pid):
            pid_str = str(pid)
            async with sem:
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    viewport={"width": 1280, "height": 800}
                )
                await context.add_init_script("""
                    (() => {
                        const style = document.createElement('style');
                        style.id = 'anti-overlay-fix';
                        style.textContent = '.cookie-overlay { display: none !important; pointer-events: none !important; }';
                        if (document.head) {
                            document.head.appendChild(style);
                        } else {
                            document.addEventListener('DOMContentLoaded', () => {
                                if (document.head) document.head.appendChild(style);
                            });
                        }
                    })();
                """)
                await context.route(
                    "**/*",
                    lambda route: route.abort()
                    if route.request.resource_type in ("image", "font", "media")
                    else route.continue_()
                )
                page = await context.new_page()
                try:
                    results[pid_str] = await _scrape_with_page(page, pid_str, max_retries, base_url)
                except Exception as e:
                    results[pid_str] = ScrapeResult(
                        source_product_id=pid_str,
                        product_name="",
                        price=None,
                        currency=None,
                        in_stock=None,
                        stock_raw=None,
                        attempts=1,
                        status="failed",
                        error_message=f"Batch worker error: {e}",
                        logs=[f"Batch worker error: {e}"],
                        elapsed_seconds=0.0
                    )
                finally:
                    await context.close()

        await asyncio.gather(*(scrape_one(pid) for pid in source_product_ids))
        await browser.close()

    return results

def scrape_product(source_product_id: str, headed: bool = False, max_retries: int = 3) -> ScrapeResult:
    return asyncio.run(scrape_product_async(source_product_id, headed=headed, max_retries=max_retries))

def main():
    parser = argparse.ArgumentParser(description="INE Store Playwright Scraper")
    parser.add_argument("--product", type=str, default="1", help="Source product ID")
    parser.add_argument("--headed", action="store_true", help="Run browser in headed mode")
    parser.add_argument("--retries", type=int, default=3, help="Max retry attempts")
    args = parser.parse_args()

    result = scrape_product(source_product_id=args.product, headed=args.headed, max_retries=args.retries)
    print("\n" + "="*50)
    print("SCRAPE RESULT:")
    print("="*50)
    print(json.dumps(asdict(result), indent=2))

if __name__ == "__main__":
    main()
