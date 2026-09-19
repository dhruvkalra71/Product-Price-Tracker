from django.urls import path
from .views import (
    PingView,
    SearchView,
    ProductListView,
    TrackProductView,
    UntrackProductView,
    ProductDetailView,
    ProductHistoryView,
    ProductLogsView,
    AlertConfigView,
    ManualScrapeView,
    RunScrapeView,
)

urlpatterns = [
    path("ping", PingView.as_view(), name="ping"),
    path("search", SearchView.as_view(), name="search"),
    path("products", ProductListView.as_view(), name="product-list"),
    path("products/track", TrackProductView.as_view(), name="product-track"),
    path("products/<int:pk>", ProductDetailView.as_view(), name="product-detail"),
    path("products/<int:pk>/track", UntrackProductView.as_view(), name="product-untrack"),
    path("products/<int:pk>/history", ProductHistoryView.as_view(), name="product-history"),
    path("products/<int:pk>/logs", ProductLogsView.as_view(), name="product-logs"),
    path("products/<int:pk>/alerts", AlertConfigView.as_view(), name="product-alerts"),
    path("scrape/single/<int:pk>", ManualScrapeView.as_view(), name="scrape-single"),
    path("scrape/run", RunScrapeView.as_view(), name="scrape-run"),
]
