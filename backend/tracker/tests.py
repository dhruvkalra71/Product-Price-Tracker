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

        resp = self.client.post(reverse("scrape-single", kwargs={"pk": p.id}))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        drop_alert.refresh_from_db()
        stock_alert.refresh_from_db()
        self.assertTrue(drop_alert.notified)
        self.assertIsNotNone(drop_alert.triggered_at)
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
