from django.db import models

class Product(models.Model):
    source_product_id = models.CharField(max_length=64, unique=True, db_index=True)
    name = models.CharField(max_length=255)
    url = models.URLField(max_length=500, blank=True)
    thumbnail_url = models.URLField(max_length=500, blank=True, null=True)
    brand = models.CharField(max_length=128, blank=True)
    category = models.CharField(max_length=128, blank=True)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    is_tracked = models.BooleanField(default=True, db_index=True)
    scrape_interval_minutes = models.IntegerField(default=120)
    last_scraped_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-first_seen_at"]

    @property
    def latest_price_entry(self):
        return self.price_history.first()

    def __str__(self):
        return f"{self.name} (#{self.source_product_id})"

class ScrapeLog(models.Model):
    STATUS_CHOICES = [
        ("success", "Success"),
        ("retried_then_success", "Retried Then Success"),
        ("failed", "Failed"),
    ]

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="logs")
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, db_index=True)
    attempt_count = models.IntegerField(default=1)
    error_message = models.TextField(null=True, blank=True)
    http_or_dom_detail = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"Log #{self.id} for Product {self.product_id}: {self.status}"

class PriceHistory(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="price_history")
    price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    in_stock = models.BooleanField(null=True, blank=True)
    stock_raw = models.CharField(max_length=128, blank=True)
    scraped_at = models.DateTimeField(auto_now_add=True, db_index=True)
    scrape_log = models.ForeignKey(ScrapeLog, on_delete=models.SET_NULL, null=True, blank=True, related_name="price_record")

    class Meta:
        ordering = ["-scraped_at"]

    def __str__(self):
        return f"Price ₹{self.price} on {self.scraped_at}"

class Alert(models.Model):
    ALERT_TYPES = [
        ("price_drop", "Price Drop"),
        ("back_in_stock", "Back In Stock"),
    ]

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="alerts")
    type = models.CharField(max_length=32, choices=ALERT_TYPES, default="price_drop")
    threshold = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    triggered_at = models.DateTimeField(null=True, blank=True)
    notified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Alert for {self.product.name} ({self.type})"

class CatalogCache(models.Model):
    key = models.CharField(max_length=64, unique=True, default="full_catalog")
    data = models.JSONField(default=list)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "tracker_catalogcache"

    def __str__(self):
        return f"CatalogCache({self.key}, items={len(self.data)}, updated_at={self.updated_at})"

class ScrapeJob(models.Model):
    STATUS_CHOICES = [
        ("queued", "Queued"),
        ("running", "Running"),
        ("done", "Done"),
        ("failed", "Failed"),
    ]

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="scrape_jobs")
    job_type = models.CharField(max_length=32, default="initial")
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default="queued", db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]
        db_table = "tracker_scrapejob"

    def __str__(self):
        return f"ScrapeJob #{self.id} for Product {self.product_id}: {self.status}"

