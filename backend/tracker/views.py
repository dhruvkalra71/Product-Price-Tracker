import asyncio
from datetime import timedelta
from decimal import Decimal
import logging
import sys
import threading
from typing import Optional
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

from .models import Product, PriceHistory, ScrapeLog, Alert, ScrapeJob
from .serializers import (
    ProductSerializer,
    ProductDetailSerializer,
    AlertSerializer,
)
from scraper.catalog import search_catalog, get_product_from_catalog
from scraper.engine import scrape_product, scrape_products_batch, ScrapeResult

SCRAPE_ADVISORY_LOCK_KEY = 847291038471

class ScrapeLock:
    """
    Cross-process distributed lock using Postgres session advisory locks
    (pg_try_advisory_lock / pg_advisory_unlock) to coordinate scrapes across
    multiple gunicorn worker processes, with an in-memory threading.Lock as a
    fast in-process check and SQLite dev fallback.
    """
    def __init__(self):
        self._thread_lock = threading.Lock()

    def acquire(self, blocking: bool = False) -> bool:
        # 1. Fast in-process thread lock check
        if not self._thread_lock.acquire(blocking=blocking):
            return False

        # 2. Cross-process Postgres advisory lock if running on PostgreSQL
        if connection.vendor == "postgresql":
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_try_advisory_lock(%s);", [SCRAPE_ADVISORY_LOCK_KEY])
                    row = cursor.fetchone()
                    acquired = bool(row and row[0])
                if not acquired:
                    self._thread_lock.release()
                    return False
            except Exception as e:
                logger.warning("Error acquiring Postgres advisory lock: %s", e)
                self._thread_lock.release()
                return False

        return True

    def release(self):
        try:
            if connection.vendor == "postgresql":
                try:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT pg_advisory_unlock(%s);", [SCRAPE_ADVISORY_LOCK_KEY])
                except Exception as e:
                    logger.warning("Error releasing Postgres advisory lock: %s", e)
        finally:
            if self._thread_lock.locked():
                self._thread_lock.release()

    def locked(self) -> bool:
        return self._thread_lock.locked()

scrape_lock = ScrapeLock()

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

        # Trigger immediate initial scrape if requested (queued in FIFO ScrapeJob queue)
        if immediate_scrape and not product.price_history.exists():
            job = enqueue_scrape_job(product, job_type="initial")
            is_test = getattr(settings, "TESTING", False) or "test" in sys.argv
            if is_test:
                sync_val = request.data.get("sync", True)
                if isinstance(sync_val, str):
                    sync_val = sync_val.lower() not in ("false", "0")
                if sync_val:
                    _process_single_job(job)
            else:
                _trigger_queue_worker()

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
            product.scrape_jobs.all().delete()
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
        if not scrape_lock.acquire(blocking=False):
            return Response(
                {"error": "A scrape is already in progress. Please try again shortly."},
                status=status.HTTP_409_CONFLICT
            )
        try:
            res = _execute_product_scrape(product)
            return Response(res)
        finally:
            scrape_lock.release()

class RunScrapeView(APIView):
    """
    Cron entry point. Auth-protected by X-Scrape-Secret header.
    Validates secret, acquires non-blocking cross-process scrape_lock, queries due products,
    spawns a background thread to execute the scrape asynchronously,
    and returns 202 Accepted immediately.
    """
    def post(self, request):
        # 1. Authenticate cron request (fail-closed if secret is unset)
        secret_header = (request.headers.get("X-Scrape-Secret") or request.headers.get("Authorization", "")).removeprefix("Bearer ").strip()

        configured_secret = getattr(settings, "SCRAPE_SHARED_SECRET", "")
        if not configured_secret or secret_header != configured_secret:
            return Response({"error": "Unauthorized cron trigger"}, status=status.HTTP_401_UNAUTHORIZED)

        # 2. Acquire cross-process lock to prevent overlapping runs
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

        # Inspect previous entry before creating new PriceHistory
        previous_entry = product.price_history.first()
        previous_in_stock = previous_entry.in_stock if previous_entry else None

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
            _check_alerts(product, Decimal(str(scrape_res.price)), scrape_res.in_stock, previous_in_stock)
            product.last_scraped_at = t_finish
        else:
            # If initial scrape failed (no price history ever recorded), do NOT set last_scraped_at
            # so scheduled cron triggers and immediate retries pick it up without waiting for interval
            if not product.price_history.exists():
                product.last_scraped_at = None
            else:
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

_worker_thread = None
_worker_lock = threading.Lock()

def enqueue_scrape_job(product: Product, job_type: str = "initial") -> ScrapeJob:
    """
    Enqueues a scrape job in the persistent DB-backed FIFO queue.
    If a job is already queued or running for this product, reuses it.
    """
    with transaction.atomic():
        existing = ScrapeJob.objects.filter(product=product, status__in=["queued", "running"]).first()
        if existing:
            return existing
        return ScrapeJob.objects.create(
            product=product,
            job_type=job_type,
            status="queued"
        )

def _trigger_queue_worker():
    """
    Ensures a single background worker thread is running to process queued ScrapeJobs FIFO.
    """
    global _worker_thread
    with _worker_lock:
        if _worker_thread is None or not _worker_thread.is_alive():
            _worker_thread = threading.Thread(target=_run_queue_worker, daemon=True)
            _worker_thread.start()

def _run_queue_worker():
    """
    FIFO queue worker loop.
    Pulls queued jobs up to SCRAPE_MAX_CONCURRENT, runs them sequentially (or concurrently if configured),
    and terminates when the queue is drained.
    """
    import time
    from django.db import close_old_connections
    close_old_connections()

    # Reset any stale running jobs (e.g. from prior process crash or restart > 5 mins ago)
    stale_cutoff = timezone.now() - timedelta(minutes=5)
    ScrapeJob.objects.filter(status="running", started_at__lt=stale_cutoff).update(
        status="failed",
        error_message="Scrape job timed out or worker process restarted",
        finished_at=timezone.now()
    )

    while True:
        close_old_connections()
        max_concurrent = max(1, int(getattr(settings, "SCRAPE_MAX_CONCURRENT", 1)))
        claimed_jobs = []

        with transaction.atomic():
            qs = ScrapeJob.objects.filter(status="queued").order_by("created_at")
            if connection.vendor == "postgresql":
                qs = qs.select_for_update(skip_locked=True)

            batch = list(qs[:max_concurrent])
            if not batch:
                break  # Queue is completely drained!

            now = timezone.now()
            for job in batch:
                job.status = "running"
                job.started_at = now
                job.save(update_fields=["status", "started_at"])
                claimed_jobs.append(job)

        if max_concurrent > 1 and len(claimed_jobs) > 1:
            _process_jobs_batch(claimed_jobs, concurrency=max_concurrent)
        else:
            for job in claimed_jobs:
                _process_single_job(job)

    close_old_connections()

def _process_single_job(job: ScrapeJob):
    import time
    from django.db import close_old_connections
    product = job.product
    if not product.is_tracked:
        job.status = "done"
        job.finished_at = timezone.now()
        job.error_message = "Product was untracked before scrape"
        job.save(update_fields=["status", "finished_at", "error_message"])
        return

    # Acquire cross-process scrape_lock so only 1 browser launches across all gunicorn workers
    while not scrape_lock.acquire(blocking=False):
        time.sleep(0.5)
        close_old_connections()

    try:
        res = _execute_product_scrape(product)
        job.status = "done" if res.get("status") in ["success", "retried_then_success"] else "failed"
        job.error_message = res.get("error_message")
    except Exception as e:
        logger.exception("Initial scrape job failed for product %s: %s", product.id, e)
        job.status = "failed"
        job.error_message = str(e)
        now = timezone.now()
        try:
            with transaction.atomic():
                ScrapeLog.objects.create(
                    product=product,
                    started_at=now,
                    finished_at=now,
                    status="failed",
                    attempt_count=1,
                    error_message=str(e),
                    http_or_dom_detail={"error": str(e), "context": "_process_single_job crash"}
                )
                product.last_scraped_at = now if product.price_history.exists() else None
                product.save(update_fields=["last_scraped_at"])
        except Exception as log_err:
            logger.exception("Failed to record error ScrapeLog for product %s: %s", product.id, log_err)
    finally:
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "finished_at", "error_message"])
        scrape_lock.release()
        close_old_connections()

def _process_jobs_batch(jobs: list[ScrapeJob], concurrency: int):
    import time
    from django.db import close_old_connections
    valid_jobs = [j for j in jobs if j.product.is_tracked]
    if not valid_jobs:
        now = timezone.now()
        for j in jobs:
            j.status = "done"
            j.finished_at = now
            j.save(update_fields=["status", "finished_at"])
        return

    while not scrape_lock.acquire(blocking=False):
        time.sleep(0.5)
        close_old_connections()

    try:
        source_ids = [str(j.product.source_product_id) for j in valid_jobs]
        results_by_id = asyncio.run(
            scrape_products_batch(source_ids, concurrency=concurrency)
        )
        now = timezone.now()
        for job in valid_jobs:
            scrape_res = results_by_id.get(str(job.product.source_product_id))
            if scrape_res:
                _finalize_scrape(job.product, scrape_res)
                job.status = "done" if scrape_res.status in ["success", "retried_then_success"] else "failed"
                job.error_message = scrape_res.error_message
            else:
                job.status = "failed"
                job.error_message = "No result returned from batch scrape"
            job.finished_at = now
            job.save(update_fields=["status", "finished_at", "error_message"])
    except Exception as e:
        logger.exception("Batch scrape failed: %s", e)
        now = timezone.now()
        for job in valid_jobs:
            job.status = "failed"
            job.error_message = str(e)
            job.finished_at = now
            job.save(update_fields=["status", "finished_at", "error_message"])
    finally:
        scrape_lock.release()
        close_old_connections()

def _async_initial_scrape(product_id: int):
    """
    Backward-compatibility helper: enqueues the product into the ScrapeJob FIFO queue
    and triggers the queue worker (or processes synchronously during tests).
    """
    try:
        product = Product.objects.get(id=product_id)
        job = enqueue_scrape_job(product, job_type="initial")
        is_test = getattr(settings, "TESTING", False) or "test" in sys.argv
        if is_test:
            _process_single_job(job)
        else:
            _trigger_queue_worker()
    except Exception as e:
        logger.exception("Failed to enqueue initial scrape for product %s: %s", product_id, e)

def _check_alerts(product: Product, current_price: Decimal, in_stock: bool, previous_in_stock: Optional[bool] = None):
    for alert in product.alerts.filter(notified=False):
        triggered = (
            (alert.type == "price_drop" and alert.threshold is not None and current_price <= alert.threshold)
            or (alert.type == "back_in_stock" and previous_in_stock is False and in_stock is True)
        )
        if triggered:
            alert.triggered_at = timezone.now()
            alert.notified = True
            alert.save(update_fields=["triggered_at", "notified"])
