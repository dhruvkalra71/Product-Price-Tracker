from decimal import Decimal
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status
from unittest.mock import patch, AsyncMock

from .models import Product, PriceHistory, ScrapeLog
from scraper.engine import ScrapeResult

class TrackerAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        from django.conf import settings
        settings.SCRAPE_SHARED_SECRET = "ine-tracker-cron-secret-2026"

    def test_ping_endpoint(self):
        resp = self.client.get(reverse("ping"))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["status"], "ok")

    @patch("tracker.views.search_catalog")
    def test_search_endpoint(self, mock_search):
        mock_search.return_value = [
            {"id": 1, "name": "Nordkraft Headphones Pro", "brand": "Nordkraft"}
        ]
        resp = self.client.get(reverse("search"), {"q": "headphones"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data), 1)
        self.assertEqual(resp.data[0]["name"], "Nordkraft Headphones Pro")
        self.assertFalse(resp.data[0]["is_tracked"])

    @patch("tracker.views.scrape_product")
    def test_track_product_creates_history_on_success(self, mock_scrape):
        mock_scrape.return_value = ScrapeResult(
            source_product_id="1",
            product_name="Nordkraft Headphones Pro",
            price=11468.0,
            currency="INR",
            in_stock=True,
            stock_raw="In stock",
            attempts=1,
            status="success",
            error_message=None,
            logs=["Log line"],
            elapsed_seconds=5.0
        )

        resp = self.client.post(reverse("product-track"), {
            "source_product_id": "1",
            "scrape_now": True
        })

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        product = Product.objects.get(source_product_id="1")
        self.assertTrue(product.is_tracked)
        
        # Verify log recorded
        self.assertEqual(product.logs.count(), 1)
        self.assertEqual(product.logs.first().status, "success")
        
        # Verify price history recorded
        self.assertEqual(product.price_history.count(), 1)
        self.assertEqual(product.price_history.first().price, Decimal("11468.00"))

    @patch("tracker.views.scrape_product")
    def test_failed_scrape_records_log_but_no_price_history(self, mock_scrape):
        mock_scrape.return_value = ScrapeResult(
            source_product_id="2",
            product_name="Failed Product",
            price=None,
            currency=None,
            in_stock=None,
            stock_raw=None,
            attempts=3,
            status="failed",
            error_message="Store returned error state: Server 503",
            logs=["Attempt 1 failed", "Attempt 2 failed", "Attempt 3 failed"],
            elapsed_seconds=15.0
        )

        resp = self.client.post(reverse("product-track"), {
            "source_product_id": "2",
            "scrape_now": True
        })

        product = Product.objects.get(source_product_id="2")
        # ScrapeLog MUST exist with failed status
        self.assertEqual(product.logs.count(), 1)
        log = product.logs.first()
        self.assertEqual(log.status, "failed")
        self.assertIn("503", log.error_message)

        # PriceHistory MUST NOT exist (satisfies honest logging & no garbage data)
        self.assertEqual(product.price_history.count(), 0)

    def test_untrack_product(self):
        from .models import Alert
        product = Product.objects.create(
            source_product_id="10",
            name="Test Item",
            is_tracked=True,
            last_scraped_at=timezone.now()
        )
        log = ScrapeLog.objects.create(product=product, status="success", attempt_count=1)
        PriceHistory.objects.create(product=product, price=Decimal("199.99"), in_stock=True, scrape_log=log)
        Alert.objects.create(product=product, type="price_drop", threshold=Decimal("150.00"))

        resp = self.client.delete(reverse("product-untrack", kwargs={"pk": product.id}))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        product.refresh_from_db()
        self.assertFalse(product.is_tracked)
        self.assertIsNone(product.last_scraped_at)
        self.assertEqual(product.price_history.count(), 0)
        self.assertEqual(product.logs.count(), 0)
        self.assertEqual(product.alerts.count(), 0)

    def test_cron_scrape_run_unauthorized_without_secret(self):
        resp = self.client.post(reverse("scrape-run"))
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    @patch("tracker.views._run_background_scrape")
    def test_cron_scrape_run_authorized_with_secret(self, mock_worker):
        Product.objects.create(
            source_product_id="5",
            name="Cron Item",
            is_tracked=True,
            scrape_interval_minutes=60,
            last_scraped_at=None  # due immediately
        )

        resp = self.client.post(
            reverse("scrape-run"),
            HTTP_X_SCRAPE_SECRET="ine-tracker-cron-secret-2026"
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(resp.data["status"], "accepted")
        self.assertEqual(resp.data["due_count"], 1)

    def test_cron_scrape_run_already_running_returns_409(self):
        from tracker.views import scrape_lock
        scrape_lock.acquire()
        try:
            resp = self.client.post(
                reverse("scrape-run"),
                HTTP_X_SCRAPE_SECRET="ine-tracker-cron-secret-2026"
            )
            self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)
            self.assertEqual(resp.data["status"], "already_running")
        finally:
            scrape_lock.release()

    @patch("tracker.views.scrape_product")
    def test_background_worker_sequential_execution(self, mock_scrape):
        from tracker.views import _run_background_scrape, scrape_lock

        mock_scrape.return_value = ScrapeResult(
            source_product_id="201",
            product_name="Seq Item",
            price=750.0,
            currency="INR",
            in_stock=True,
            stock_raw="In stock",
            attempts=1,
            status="success",
            error_message=None,
            logs=["ok"],
            elapsed_seconds=1.0
        )

        p = Product.objects.create(
            source_product_id="201",
            name="Seq Item",
            is_tracked=True,
            scrape_interval_minutes=60,
            last_scraped_at=None
        )

        scrape_lock.acquire()
        _run_background_scrape([p.id])

        # Verify lock was released in finally
        self.assertFalse(scrape_lock.locked())

        # Verify database was written
        p.refresh_from_db()
        self.assertEqual(p.price_history.count(), 1)
        self.assertEqual(p.price_history.first().price, Decimal("750.00"))
        self.assertEqual(p.logs.count(), 1)

    @patch("tracker.views.scrape_products_batch", new_callable=AsyncMock)
    def test_background_worker_concurrent_execution(self, mock_batch):
        from tracker.views import _run_background_scrape, scrape_lock

        mock_batch.return_value = {
            "301": ScrapeResult(
                source_product_id="301",
                product_name="Batch Item 1",
                price=500.0,
                currency="INR",
                in_stock=True,
                stock_raw="In stock",
                attempts=1,
                status="success",
                error_message=None,
                logs=["ok"],
                elapsed_seconds=1.5
            ),
            "302": ScrapeResult(
                source_product_id="302",
                product_name="Batch Item 2",
                price=None,
                currency=None,
                in_stock=None,
                stock_raw=None,
                attempts=3,
                status="failed",
                error_message="Store error",
                logs=["fail"],
                elapsed_seconds=3.0
            ),
        }

        p1 = Product.objects.create(source_product_id="301", name="Batch Item 1", is_tracked=True)
        p2 = Product.objects.create(source_product_id="302", name="Batch Item 2", is_tracked=True)

        scrape_lock.acquire()
        with self.settings(SCRAPE_MAX_CONCURRENT=2):
            _run_background_scrape([p1.id, p2.id])

        # Verify lock released
        self.assertFalse(scrape_lock.locked())

        # Verify p1 recorded price history
        p1.refresh_from_db()
        self.assertEqual(p1.price_history.count(), 1)
        self.assertEqual(p1.price_history.first().price, Decimal("500.00"))

        # Verify p2 recorded log but NO price history
        p2.refresh_from_db()
        self.assertEqual(p2.price_history.count(), 0)
        self.assertEqual(p2.logs.count(), 1)
        self.assertEqual(p2.logs.first().status, "failed")

    @patch("tracker.views.scrape_product")
    def test_scrape_log_records_overlay_detected_telemetry(self, mock_scrape):
        mock_scrape.return_value = ScrapeResult(
            source_product_id="401",
            product_name="Overlay Product",
            price=1500.0,
            currency="INR",
            in_stock=True,
            stock_raw="In stock",
            attempts=1,
            status="success",
            error_message=None,
            logs=["[TELEMETRY] Cookie overlay detected in DOM (1 element(s))"],
            elapsed_seconds=3.2,
            overlay_detected=True
        )

        resp = self.client.post(reverse("product-track"), {
            "source_product_id": "401",
            "scrape_now": True
        })
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        product = Product.objects.get(source_product_id="401")
        log = product.logs.first()
        self.assertTrue(log.http_or_dom_detail.get("overlay_detected"))

    def test_product_list_includes_alerts(self):
        from .models import Alert
        p = Product.objects.create(source_product_id="501", name="Alert Item", is_tracked=True)
        Alert.objects.create(product=p, type="price_drop", threshold=Decimal("999.00"))
        Alert.objects.create(product=p, type="back_in_stock")

        resp = self.client.get(reverse("product-list"))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        item = next(x for x in resp.data if x["source_product_id"] == "501")
        self.assertIn("alerts", item)
        self.assertEqual(len(item["alerts"]), 2)
        types = {a["type"] for a in item["alerts"]}
        self.assertEqual(types, {"price_drop", "back_in_stock"})

    @patch("tracker.views.scrape_product")
    def test_alerts_trigger_on_scrape(self, mock_scrape):
        from .models import Alert
        p = Product.objects.create(source_product_id="601", name="Trigger Item", is_tracked=True)
        drop_alert = Alert.objects.create(product=p, type="price_drop", threshold=Decimal("1000.00"), notified=False)
        stock_alert = Alert.objects.create(product=p, type="back_in_stock", notified=False)

        mock_scrape.return_value = ScrapeResult(
            source_product_id="601",
            product_name="Trigger Item",
            price=950.0,
            currency="INR",
            in_stock=True,
            stock_raw="In stock",
            attempts=1,
            status="success",
            error_message=None,
            logs=["ok"],
            elapsed_seconds=1.0
        )

        # Scrape 1: First-ever scrape with in_stock=True.
        # Price drop alert should fire (950 <= 1000).
        # Back in stock alert should NOT fire on the very first scrape (no prior history showing it was out of stock).
        resp = self.client.post(reverse("scrape-single", kwargs={"pk": p.id}))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        drop_alert.refresh_from_db()
        stock_alert.refresh_from_db()
        self.assertTrue(drop_alert.notified)
        self.assertIsNotNone(drop_alert.triggered_at)
        self.assertFalse(stock_alert.notified)
        self.assertIsNone(stock_alert.triggered_at)

        # Scrape 2: Item goes out of stock (in_stock=False)
        mock_scrape.return_value = ScrapeResult(
            source_product_id="601",
            product_name="Trigger Item",
            price=950.0,
            currency="INR",
            in_stock=False,
            stock_raw="Out of stock",
            attempts=1,
            status="success",
            error_message=None,
            logs=["ok"],
            elapsed_seconds=1.0
        )
        resp = self.client.post(reverse("scrape-single", kwargs={"pk": p.id}))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        stock_alert.refresh_from_db()
        self.assertFalse(stock_alert.notified)

        # Scrape 3: Item transitions back into stock (in_stock=True after being False) -> Back-in-stock alert fires!
        mock_scrape.return_value = ScrapeResult(
            source_product_id="601",
            product_name="Trigger Item",
            price=950.0,
            currency="INR",
            in_stock=True,
            stock_raw="In stock",
            attempts=1,
            status="success",
            error_message=None,
            logs=["ok"],
            elapsed_seconds=1.0
        )
        resp = self.client.post(reverse("scrape-single", kwargs={"pk": p.id}))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        stock_alert.refresh_from_db()
        self.assertTrue(stock_alert.notified)
        self.assertIsNotNone(stock_alert.triggered_at)

    def test_update_product_scrape_interval_success(self):
        p = Product.objects.create(source_product_id="701", name="Interval Item", is_tracked=True, scrape_interval_minutes=120)
        resp = self.client.patch(reverse("product-detail", kwargs={"pk": p.id}), {
            "scrape_interval_minutes": 30
        })
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["scrape_interval_minutes"], 30)

        p.refresh_from_db()
        self.assertEqual(p.scrape_interval_minutes, 30)

    def test_update_product_scrape_interval_validation(self):
        p = Product.objects.create(source_product_id="702", name="Validation Item", is_tracked=True, scrape_interval_minutes=120)

        # Missing field
        resp = self.client.patch(reverse("product-detail", kwargs={"pk": p.id}), {})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

        # Value too small (< 5 mins)
        resp = self.client.patch(reverse("product-detail", kwargs={"pk": p.id}), {"scrape_interval_minutes": 2})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Minimum", resp.data["error"])

        # Value not integer
        resp = self.client.patch(reverse("product-detail", kwargs={"pk": p.id}), {"scrape_interval_minutes": "invalid"})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    @patch("tracker.views._execute_product_scrape")
    def test_async_initial_scrape_exception_creates_failed_scrapelog(self, mock_exec):
        from tracker.views import _async_initial_scrape
        p = Product.objects.create(
            source_product_id="801",
            name="Crashing Product",
            is_tracked=True
        )
        mock_exec.side_effect = RuntimeError("Playwright browser missing or OOM")

        _async_initial_scrape(p.id)

        # ScrapeLog must be created with status='failed'
        self.assertEqual(p.logs.count(), 1)
        log = p.logs.first()
        self.assertEqual(log.status, "failed")
        self.assertIn("Playwright browser missing or OOM", log.error_message)

        # Price history must remain empty
        self.assertEqual(p.price_history.count(), 0)

        # Serializer should expose latest_status as 'failed' and latest_error
        resp = self.client.get(reverse("product-list"))
        data = next(x for x in resp.data if x["id"] == p.id)
        self.assertEqual(data["latest_status"], "failed")
        self.assertIn("Playwright browser missing or OOM", data["latest_error"])

    @patch("scraper.catalog.fetch_page")
    def test_catalog_fetch_page_retry(self, mock_fetch):
        from scraper.catalog import fetch_page_with_retry
        mock_fetch.side_effect = [RuntimeError("Transient network failure"), {"page": 1, "pages": 1, "items": [{"id": 99}]}]
        data = fetch_page_with_retry(1)
        self.assertEqual(data["page"], 1)
        self.assertEqual(mock_fetch.call_count, 2)

    @patch("tracker.views.scrape_product")
    def test_concurrent_scrapes_prevented_by_lock(self, mock_scrape):
        from tracker.views import scrape_lock
        p1 = Product.objects.create(source_product_id="901", name="Product 1", is_tracked=True)
        p2 = Product.objects.create(source_product_id="902", name="Product 2", is_tracked=True)

        mock_scrape.return_value = ScrapeResult(
            source_product_id="901",
            product_name="Product 1",
            price=100.0,
            currency="INR",
            in_stock=True,
            stock_raw="In stock",
            attempts=1,
            status="success",
            error_message=None,
            logs=["ok"],
            elapsed_seconds=1.0
        )

        # 1. Acquire lock to simulate an in-progress scrape
        self.assertTrue(scrape_lock.acquire(blocking=False))
        try:
            # 2. Second manual scrape for Product 2 attempts while lock is held -> must return 409 Conflict
            resp = self.client.post(reverse("scrape-single", kwargs={"pk": p2.id}))
            self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)
            self.assertIn("already in progress", resp.data["error"])
        finally:
            scrape_lock.release()

        # 3. After releasing lock, scrape for Product 2 succeeds
        resp = self.client.post(reverse("scrape-single", kwargs={"pk": p2.id}))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    @patch("scraper.catalog.fetch_page")
    def test_catalog_force_refresh_triggers_new_fetches(self, mock_fetch):
        from scraper.catalog import get_full_catalog
        mock_fetch.return_value = {"page": 1, "pages": 1, "items": [{"id": 101, "name": "Item 101"}]}

        # First call populates cache
        res1 = get_full_catalog(force_refresh=True)
        initial_call_count = mock_fetch.call_count
        self.assertGreaterEqual(initial_call_count, 1)

        # Second call without force_refresh uses cache (no new fetch calls)
        res2 = get_full_catalog(force_refresh=False)
        self.assertEqual(mock_fetch.call_count, initial_call_count)

        # Third call with force_refresh=True MUST trigger new fetch_page calls
        res3 = get_full_catalog(force_refresh=True)
        self.assertGreater(mock_fetch.call_count, initial_call_count)

    def test_genuine_price_selection_by_font_signature(self):
        from scraper.engine import select_price_candidate
        candidates = [
            {"text": "₹15,999", "fontSize": "16px", "fontWeight": "400"},      # MRP / strikethrough decoy
            {"text": "₹11,468", "fontSize": "38.4px", "fontWeight": "700"},    # Genuine price (closest to 38.4px, bold)
            {"text": "₹12,999", "fontSize": "20px", "fontWeight": "700"},      # Sub-heading price
        ]
        chosen = select_price_candidate(candidates)
        self.assertEqual(chosen["text"], "₹11,468")

    def test_price_selection_fallback_with_warning_when_no_signature_match(self):
        from scraper.engine import select_price_candidate
        candidates = [
            {"text": "₹1,299", "fontSize": "14px", "fontWeight": "400"},
            {"text": "₹999", "fontSize": "12px", "fontWeight": "400"},
        ]
        logs = []
        chosen = select_price_candidate(candidates, log_fn=logs.append)
        # Falls back to index 0
        self.assertEqual(chosen["text"], "₹1,299")
        # Logs warning
        self.assertTrue(any("WARNING" in msg for msg in logs))

    def test_run_scrape_fails_closed_when_shared_secret_unset(self):
        from django.test import override_settings
        with override_settings(SCRAPE_SHARED_SECRET=""):
            # When secret is unset in settings, any cron call must fail closed with 401
            resp = self.client.post(reverse("scrape-run"), HTTP_X_SCRAPE_SECRET="any-secret")
            self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)
