import asyncio
from datetime import timedelta
from decimal import Decimal
import logging
import sys
import threading
from django.conf import settings
from django.db import connection, transaction
from django.db.models import Q, F, ExpressionWrapper, DurationField
from django.db.models.functions import Now
from django.shortcuts import get_object_or_404
from django.utils import timezone

logger = logging.getLogger(__name__)
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Product, PriceHistory, ScrapeLog, Alert
from .serializers import (
    ProductSerializer,
    ProductDetailSerializer,
    AlertSerializer,
)
from scraper.catalog import search_catalog, get_product_from_catalog
from scraper.engine import scrape_product, scrape_products_batch, ScrapeResult

# In-memory lock to prevent overlapping background scrape runs
scrape_lock = threading.Lock()

class PingView(APIView):
    """
    Lightweight keep-warm endpoint.
    Should be pinged every ~10 minutes via a separate cron job (e.g. on cron-job.org)
    to keep free-tier Render containers warm. Automated scrape reliability on
    /api/scrape/run depends on the instance already being warm when the scrape cron fires.
    """
    def get(self, request):
        return Response({"status": "ok", "timestamp": timezone.now().isoformat()})

class SearchView(APIView):
    def get(self, request):
        query = request.query_params.get("q", "").strip()
        catalog_results = search_catalog(query, limit=25)
        
        # Cross-reference with tracked products
        tracked_map = dict(Product.objects.filter(is_tracked=True).values_list("source_product_id", "id"))

        for item in catalog_results:
            pid = str(item.get("id"))
            item["is_tracked"] = pid in tracked_map
            item["tracker_id"] = tracked_map.get(pid)

        return Response(catalog_results)

class ProductListView(APIView):
    def get(self, request):
        products = Product.objects.filter(is_tracked=True).prefetch_related("price_history", "logs", "alerts")
        serializer = ProductSerializer(products, many=True)
        return Response(serializer.data)

class TrackProductView(APIView):
    def post(self, request):
        source_product_id = str(request.data.get("source_product_id", "")).strip()
        if not source_product_id:
            return Response({"error": "source_product_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        interval = int(request.data.get("scrape_interval_minutes", 120))
        immediate_scrape = request.data.get("scrape_now", True)

        # Retrieve product metadata from request if provided (e.g. from search), otherwise fallback to catalog
        name = request.data.get("name")
        brand = request.data.get("brand") or ""
        category = request.data.get("category") or ""
        thumbnail_url = request.data.get("thumbnail_url")

        if not name:
            catalog_info = get_product_from_catalog(source_product_id) or {}
            name = catalog_info.get("name") or f"Product #{source_product_id}"
            brand = brand or catalog_info.get("brand", "")
            category = category or catalog_info.get("category", "")
            thumbnail_url = thumbnail_url or catalog_info.get("thumbnail_url")

        url = f"https://demo.inelabteamdev.com/product/{source_product_id}"

        product, created = Product.objects.get_or_create(
            source_product_id=source_product_id,
            defaults={
                "name": name,
                "brand": brand,
                "category": category,
                "thumbnail_url": thumbnail_url,
                "url": url,
                "is_tracked": True,
                "scrape_interval_minutes": interval,
            }
        )

        if not created:
            product.is_tracked = True
            product.scrape_interval_minutes = interval
            if not product.name or product.name.startswith("Product #"):
                product.name = name
            if thumbnail_url and not product.thumbnail_url:
                product.thumbnail_url = thumbnail_url
            product.save()

        # Trigger immediate initial scrape if requested (async in background unless testing/sync)
        if immediate_scrape and not product.price_history.exists():
            is_test = getattr(settings, "TESTING", False) or "test" in sys.argv or request.data.get("sync", False)
            if is_test:
                _execute_product_scrape(product)
            else:
                thread = threading.Thread(
                    target=_async_initial_scrape,
                    args=(product.id,),
                    daemon=True
                )
                thread.start()

        return Response(ProductSerializer(product).data, status=status.HTTP_201_CREATED)

class UntrackProductView(APIView):
    def delete(self, request, pk):
        product = get_object_or_404(Product, pk=pk)
        with transaction.atomic():
            product.is_tracked = False
            product.last_scraped_at = None
            product.save(update_fields=["is_tracked", "last_scraped_at"])
            product.price_history.all().delete()
            product.logs.all().delete()
            product.alerts.all().delete()
        return Response({"status": "untracked", "id": pk})

class ProductDetailView(APIView):
    def get(self, request, pk):
        product = get_object_or_404(Product, pk=pk)
        return Response(ProductDetailSerializer(product).data)

    def patch(self, request, pk):
        product = get_object_or_404(Product, pk=pk)
        raw_interval = request.data.get("scrape_interval_minutes")
        if raw_interval is None:
            return Response({"error": "scrape_interval_minutes is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            interval = int(raw_interval)
        except (ValueError, TypeError):
            return Response({"error": "scrape_interval_minutes must be an integer"}, status=status.HTTP_400_BAD_REQUEST)

        if interval < 5:
            return Response({"error": "Minimum scrape interval is 5 minutes"}, status=status.HTTP_400_BAD_REQUEST)
        if interval > 43200:
            return Response({"error": "Maximum scrape interval is 43200 minutes (30 days)"}, status=status.HTTP_400_BAD_REQUEST)

        product.scrape_interval_minutes = interval
        product.save(update_fields=["scrape_interval_minutes"])
        return Response(ProductDetailSerializer(product).data, status=status.HTTP_200_OK)


class AlertConfigView(APIView):
    def post(self, request, pk):
        product = get_object_or_404(Product, pk=pk)
        alert_type = request.data.get("type", "price_drop")
        threshold = request.data.get("threshold")

        alert = Alert.objects.create(
            product=product,
            type=alert_type,
            threshold=Decimal(str(threshold)) if threshold is not None else None
        )
        return Response(AlertSerializer(alert).data, status=status.HTTP_201_CREATED)

class ManualScrapeView(APIView):
    def post(self, request, pk):
        product = get_object_or_404(Product, pk=pk)
        res = _execute_product_scrape(product)
        return Response(res)

class RunScrapeView(APIView):
    """
    Cron entry point. Auth-protected by X-Scrape-Secret header.
    Validates secret, acquires non-blocking scrape_lock, queries due products,
    spawns a background thread to execute the scrape asynchronously,
    and returns 202 Accepted immediately.
    """
    def post(self, request):
        # 1. Authenticate cron request
        secret_header = (request.headers.get("X-Scrape-Secret") or request.headers.get("Authorization", "")).removeprefix("Bearer ").strip()

        configured_secret = getattr(settings, "SCRAPE_SHARED_SECRET", "")
        if configured_secret and secret_header != configured_secret:
            return Response({"error": "Unauthorized cron trigger"}, status=status.HTTP_401_UNAUTHORIZED)

        # 2. Acquire in-process lock to prevent overlapping runs
        if not scrape_lock.acquire(blocking=False):
            return Response(
                {"status": "already_running", "message": "Scrape run currently in progress"},
                status=status.HTTP_409_CONFLICT
            )

        # 3. Query due products (cheap DB read)
        now = timezone.now()
        due_filter = Q(last_scraped_at__isnull=True) | Q(
            last_scraped_at__lte=Now() - ExpressionWrapper(
                F("scrape_interval_minutes") * timedelta(minutes=1),
                output_field=DurationField()
            )
        )
        due_products = list(Product.objects.filter(due_filter, is_tracked=True)[:50])

        if not due_products:
            scrape_lock.release()
            return Response({
                "status": "idle",
                "message": "No products currently due for scraping",
                "due_count": 0,
                "timestamp": now.isoformat()
            }, status=status.HTTP_200_OK)

        # 4. Spawn background worker thread and return 202 Accepted immediately
        product_ids = [p.id for p in due_products]
        thread = threading.Thread(
            target=_run_background_scrape,
            args=(product_ids,),
            daemon=True
        )
        thread.start()

        return Response({
            "status": "accepted",
            "message": "Background scrape started",
            "due_count": len(due_products),
            "timestamp": now.isoformat()
        }, status=status.HTTP_202_ACCEPTED)

def _run_background_scrape(product_ids: list[int]):
    """
    Background worker function running in a separate thread.
    Executes scraping sequentially by default (guaranteeing one Playwright
    browser session fully closes before the next begins).
    Guarantees scrape_lock is released and connection is closed on exit.
    """
    try:
        products = list(Product.objects.filter(id__in=product_ids))
        max_concurrent = int(getattr(settings, "SCRAPE_MAX_CONCURRENT", 1))

        if max_concurrent > 1:
            results_by_id = asyncio.run(
                scrape_products_batch(
                    [str(p.source_product_id) for p in products],
                    concurrency=max_concurrent
                )
            )
            for p in products:
                scrape_res = results_by_id.get(str(p.source_product_id))
                if scrape_res:
                    _finalize_scrape(p, scrape_res)
        else:
            # Sequential processing (default baseline: one browser closes before next starts)
            for p in products:
                try:
                    _execute_product_scrape(p)
                except Exception:
                    pass
    except Exception:
        pass
    finally:
        connection.close()
        scrape_lock.release()

def _finalize_scrape(product: Product, scrape_res: ScrapeResult) -> dict:
    t_finish = timezone.now()
    t_start = t_finish - timezone.timedelta(seconds=scrape_res.elapsed_seconds)

    with transaction.atomic():
        # 1. ALWAYS record a ScrapeLog row (satisfies honest logging)
        log = ScrapeLog.objects.create(
            product=product,
            started_at=t_start,
            finished_at=t_finish,
            status=scrape_res.status,
            attempt_count=scrape_res.attempts,
            error_message=scrape_res.error_message,
            http_or_dom_detail={
                "logs": scrape_res.logs,
                "elapsed_seconds": scrape_res.elapsed_seconds,
                "stock_raw": scrape_res.stock_raw,
                "overlay_detected": getattr(scrape_res, "overlay_detected", False),
            }
        )

        # 2. ONLY write to PriceHistory when there is a valid verified price
        if scrape_res.price is not None:
            PriceHistory.objects.create(
                product=product,
                price=Decimal(str(scrape_res.price)),
                in_stock=scrape_res.in_stock,
                stock_raw=scrape_res.stock_raw or "",
                scrape_log=log
            )

            # Update product name if more accurate
            if scrape_res.product_name and (not product.name or product.name.startswith("Product #")):
                product.name = scrape_res.product_name

            # Check alerts
            _check_alerts(product, Decimal(str(scrape_res.price)), scrape_res.in_stock)

        product.last_scraped_at = t_finish
        product.save()

    return {
        "product_id": product.id,
        "source_product_id": product.source_product_id,
        "name": product.name,
        "status": scrape_res.status,
        "price": scrape_res.price,
        "in_stock": scrape_res.in_stock,
        "attempts": scrape_res.attempts,
        "error_message": scrape_res.error_message,
        "log_id": log.id
    }

def _execute_product_scrape(product: Product) -> dict:
    scrape_res = scrape_product(source_product_id=product.source_product_id, headed=False)
    return _finalize_scrape(product, scrape_res)

def _async_initial_scrape(product_id: int):
    """
    Asynchronously executes initial scrape for a newly tracked product in a background thread.
    Cleans up DB connections before and after execution to prevent connection leaks.
    """
    from django.db import close_old_connections
    close_old_connections()
    try:
        product = Product.objects.get(id=product_id)
        _execute_product_scrape(product)
    except Exception as e:
        logger.exception("Initial background scrape failed for product %s: %s", product_id, e)
    finally:
        close_old_connections()

def _check_alerts(product: Product, current_price: Decimal, in_stock: bool):
    active_alerts = product.alerts.filter(notified=False)
    for alert in active_alerts:
        triggered = False
        if alert.type == "price_drop" and alert.threshold is not None:
            if current_price <= alert.threshold:
                triggered = True
        elif alert.type == "back_in_stock":
            if in_stock is True:
                triggered = True

        if triggered:
            alert.triggered_at = timezone.now()
            alert.notified = True
            alert.save(update_fields=["triggered_at", "notified"])
