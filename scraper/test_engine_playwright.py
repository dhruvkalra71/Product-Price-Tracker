import sys
import unittest
from unittest.mock import AsyncMock
from playwright.async_api import async_playwright
from scraper.engine import _neutralize_cookie_overlay, COOKIE_SUPPRESSION_CSS

class ScraperPlaywrightTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = None
        if sys.platform == 'win32':
            try:
                self.browser = await self.playwright.chromium.launch(channel='msedge', headless=True)
            except Exception:
                pass
        if self.browser is None:
            self.browser = await self.playwright.chromium.launch(headless=True)

    async def asyncTearDown(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def test_overlay_detected_and_neutralized_in_dom(self):
        page = await self.browser.new_page()
        html = '''<!DOCTYPE html>
<html>
<head><title>Test</title></head>
<body>
    <div class="cookie-overlay" style="display: block; pointer-events: auto;">
        <div class="cookie-banner"><button>Accept</button></div>
    </div>
    <button id="reveal">Reveal price</button>
</body>
</html>'''
        await page.set_content(html)
        logs = []

        detected = await _neutralize_cookie_overlay(page, logs.append)

        self.assertTrue(detected)
        self.assertTrue(any('[TELEMETRY] Cookie overlay detected' in l for l in logs))

        styles = await page.evaluate('''() => {
            const el = document.querySelector('.cookie-overlay');
            const cs = window.getComputedStyle(el);
            return {
                display: cs.display,
                pointerEvents: cs.pointerEvents
            };
        }''')
        self.assertEqual(styles['display'], 'none')
        self.assertEqual(styles['pointerEvents'], 'none')
        await page.close()

    async def test_no_overlay_returns_false_and_no_telemetry(self):
        page = await self.browser.new_page()
        await page.set_content('<html><body><h1>Clean Page</h1></body></html>')
        logs = []

        detected = await _neutralize_cookie_overlay(page, logs.append)

        self.assertFalse(detected)
        self.assertFalse(any('[TELEMETRY]' in l for l in logs))
        await page.close()

    async def test_init_script_survives_reload_and_navigation(self):
        context = await self.browser.new_context()
        await context.add_init_script('''
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
        ''')
        page = await context.new_page()

        import urllib.parse
        html = """<!DOCTYPE html>
<html>
<head><title>Test Init Script</title></head>
<body>
    <div class="cookie-overlay">Cookie Modal</div>
</body>
</html>"""
        data_url = "data:text/html," + urllib.parse.quote(html)
        await page.goto(data_url)

        display_before = await page.evaluate("() => window.getComputedStyle(document.querySelector('.cookie-overlay')).display")
        self.assertEqual(display_before, 'none')

        await page.reload()
        display_after_reload = await page.evaluate("() => window.getComputedStyle(document.querySelector('.cookie-overlay')).display")
        self.assertEqual(display_after_reload, 'none')

        await context.close()

    async def test_add_style_tag_failure_does_not_lose_detection(self):
        page = await self.browser.new_page()
        await page.set_content('<html><body><div class="cookie-overlay">Banner</div></body></html>')
        logs = []

        original_add_style = page.add_style_tag
        page.add_style_tag = AsyncMock(side_effect=RuntimeError('Simulated style tag failure'))

        try:
            detected = await _neutralize_cookie_overlay(page, logs.append)
            self.assertTrue(detected, 'Detection flag was lost when style injection failed!')
            self.assertTrue(any('[WARNING] Failed to inject overlay suppression styles' in l for l in logs))
            self.assertTrue(any('[TELEMETRY] Cookie overlay detected in DOM' in l for l in logs))
        finally:
            page.add_style_tag = original_add_style
            await page.close()

    async def test_no_duplicate_style_tags_accumulated(self):
        page = await self.browser.new_page()
        await page.set_content('<html><head></head><body><div class="cookie-overlay">Banner</div></body></html>')
        logs = []

        await _neutralize_cookie_overlay(page, logs.append)
        await _neutralize_cookie_overlay(page, logs.append)

        style_count = await page.evaluate("() => document.querySelectorAll('#anti-overlay-fix').length")
        self.assertEqual(style_count, 1)
    async def test_genuine_price_selected_over_decoy_candidates(self):
        from scraper.engine import select_price_candidate
        # Candidates in non-price-first order
        candidates = [
            {"text": "Deal: ₹999", "fontSize": "14px", "fontWeight": "400"},
            {"text": "₹12,499", "fontSize": "38.4px", "fontWeight": "700"},
            {"text": "MRP: ₹15,000", "fontSize": "18px", "fontWeight": "400"},
        ]
        logs = []
        chosen = select_price_candidate(candidates, log_fn=logs.append)
        self.assertEqual(chosen["text"], "₹12,499")
        self.assertTrue(any("Selected candidate matching genuine price signature" in l for l in logs))

if __name__ == '__main__':
    unittest.main()
