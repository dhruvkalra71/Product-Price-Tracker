from rest_framework import serializers
from .models import Product, PriceHistory, ScrapeLog, Alert, ScrapeJob

class PriceHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = PriceHistory
        fields = ["id", "price", "in_stock", "stock_raw", "scraped_at", "scrape_log"]

class ScrapeLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = ScrapeLog
        fields = [
            "id", "product", "started_at", "finished_at",
            "status", "attempt_count", "error_message", "http_or_dom_detail"
        ]

class AlertSerializer(serializers.ModelSerializer):
    class Meta:
        model = Alert
        fields = ["id", "product", "type", "threshold", "triggered_at", "notified", "created_at"]

class ProductSerializer(serializers.ModelSerializer):
    latest_price = serializers.SerializerMethodField()
    latest_in_stock = serializers.SerializerMethodField()
    latest_stock_raw = serializers.SerializerMethodField()
    latest_status = serializers.SerializerMethodField()
    latest_error = serializers.SerializerMethodField()
    scrape_queue_status = serializers.SerializerMethodField()
    queue_position = serializers.SerializerMethodField()
    alerts = AlertSerializer(many=True, read_only=True)

    class Meta:
        model = Product
        fields = [
            "id", "source_product_id", "name", "url", "thumbnail_url",
            "brand", "category", "first_seen_at", "is_tracked",
            "scrape_interval_minutes", "last_scraped_at",
            "latest_price", "latest_in_stock", "latest_stock_raw", "latest_status",
            "latest_error", "alerts",
            "scrape_queue_status", "queue_position",
        ]

    def _get_active_job(self, obj):
        if not hasattr(obj, "_cached_active_job"):
            obj._cached_active_job = obj.scrape_jobs.filter(status__in=["queued", "running"]).order_by("created_at").first()
        return obj._cached_active_job

    def get_latest_price(self, obj):
        e = obj.latest_price_entry
        return float(e.price) if e and e.price is not None else None

    def get_latest_in_stock(self, obj):
        return (e := obj.latest_price_entry) and e.in_stock

    def get_latest_stock_raw(self, obj):
        return (e := obj.latest_price_entry) and e.stock_raw

    def get_scrape_queue_status(self, obj):
        job = self._get_active_job(obj)
        return job.status if job else None

    def get_queue_position(self, obj):
        job = self._get_active_job(obj)
        if not job or job.status != "queued":
            return None
        return ScrapeJob.objects.filter(status="queued", created_at__lte=job.created_at).count()

    def get_latest_status(self, obj):
        job = self._get_active_job(obj)
        if job and not obj.latest_price_entry:
            return job.status
        return (l := obj.logs.first()) and l.status or "pending"

    def get_latest_error(self, obj):
        job = self._get_active_job(obj)
        if job and job.status in ["queued", "running"] and not obj.latest_price_entry:
            return None
        return (l := obj.logs.first()) and l.error_message or None

class ProductDetailSerializer(ProductSerializer):
    price_history = PriceHistorySerializer(many=True, read_only=True)
    logs = ScrapeLogSerializer(many=True, read_only=True)

    class Meta(ProductSerializer.Meta):
        fields = ProductSerializer.Meta.fields + ["price_history", "logs"]
