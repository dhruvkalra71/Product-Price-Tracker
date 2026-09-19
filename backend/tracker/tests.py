from decimal import Decimal
from django.test import TestCase
from django.urls import reverse
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
        product = Product.objects.create(
            source_product_id="10",
            name="Test Item",
            is_tracked=True
        )
        resp = self.client.delete(reverse("product-untrack", kwargs={"pk": product.id}))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        product.refresh_from_db()
        self.assertFalse(product.is_tracked)

    def test_cron_scrape_run_unauthorized_without_secret(self):
        resp = self.client.post(reverse("scrape-run"))
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    @patch("tracker.views.scrape_products_batch", new_callable=AsyncMock)
    def test_cron_scrape_run_authorized_with_secret(self, mock_batch):
        mock_batch.return_value = {
            "5": ScrapeResult(
                source_product_id="5",
                product_name="Cron Item",
                price=999.0,
                currency="INR",
                in_stock=True,
                stock_raw="5 left",
                attempts=1,
                status="success",
                error_message=None,
                logs=["ok"],
                elapsed_seconds=2.0
            )
        }

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
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["scraped_count"], 1)
        self.assertEqual(len(resp.data["results"]), 1)
        self.assertEqual(resp.data["results"][0]["price"], 999.0)

    @patch("tracker.views.scrape_products_batch", new_callable=AsyncMock)
    def test_cron_scrape_batch_multiple_products(self, mock_batch):
        mock_batch.return_value = {
            "101": ScrapeResult(
                source_product_id="101",
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
            "102": ScrapeResult(
                source_product_id="102",
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

        Product.objects.create(
            source_product_id="101",
            name="Batch Item 1",
            is_tracked=True,
            scrape_interval_minutes=60,
            last_scraped_at=None
        )
        Product.objects.create(
            source_product_id="102",
            name="Batch Item 2",
            is_tracked=True,
            scrape_interval_minutes=60,
            last_scraped_at=None
        )

        resp = self.client.post(
            reverse("scrape-run"),
            HTTP_X_SCRAPE_SECRET="ine-tracker-cron-secret-2026"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["scraped_count"], 2)

        # Verify product 101 recorded price history
        p1 = Product.objects.get(source_product_id="101")
        self.assertEqual(p1.price_history.count(), 1)
        self.assertEqual(p1.price_history.first().price, Decimal("500.00"))
        self.assertEqual(p1.logs.count(), 1)

        # Verify product 102 recorded log but NO price history
        p2 = Product.objects.get(source_product_id="102")
        self.assertEqual(p2.price_history.count(), 0)
        self.assertEqual(p2.logs.count(), 1)
        self.assertEqual(p2.logs.first().status, "failed")
