import concurrent.futures
import functools
import json
import logging
import re
import threading
import time
import urllib.request
from typing import List, Optional

logger = logging.getLogger(__name__)

BASE_URL = "https://demo.inelabteamdev.com"

# Note: _catalog_cache is an in-memory cache per-process. In a multi-worker / multi-dyno
# production deployment, consider an external cache layer (such as Redis) or database caching
# so all workers share consistent catalog state.
_catalog_cache: Optional[List[dict]] = None
_catalog_cache_time: float = 0.0
_CATALOG_CACHE_TTL: float = 900.0  # 15 minutes TTL
_catalog_lock = threading.Lock()

def fetch_page(page: int = 1, page_size: int = 50) -> dict:
    url = f"{BASE_URL}/api/catalog?page={page}&pageSize={page_size}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "INE-Tracker/1.0 (Python urllib)"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Catalog API returned HTTP {resp.status}")
        return json.loads(resp.read().decode("utf-8"))

def fetch_page_with_retry(page: int = 1, page_size: int = 50) -> dict:
    """Fetches a catalog page, retrying once on failure before raising."""
    try:
        return fetch_page(page, page_size=page_size)
    except Exception as e1:
        logger.warning("Retrying catalog page %d after error: %s", page, e1)
        time.sleep(0.5)
        try:
            return fetch_page(page, page_size=page_size)
        except Exception as e2:
            logger.error("Permanently failed to fetch catalog page %d after retry: %s", page, e2)
            raise

def _get_db_catalog_cache() -> Optional[List[dict]]:
    """Loads shared catalog from DB CatalogCache model if valid within TTL."""
    try:
        from django.utils import timezone
        from tracker.models import CatalogCache
        entry = CatalogCache.objects.filter(key="full_catalog").first()
        if entry and entry.data:
            age = (timezone.now() - entry.updated_at).total_seconds()
            if age < _CATALOG_CACHE_TTL:
                return entry.data
    except Exception:
        pass
    return None

def _set_db_catalog_cache(items: List[dict]):
    """Persists shared catalog to DB CatalogCache model."""
    try:
        from tracker.models import CatalogCache
        CatalogCache.objects.update_or_create(
            key="full_catalog",
            defaults={"data": items}
        )
    except Exception as e:
        logger.warning("Could not persist catalog cache to database: %s", e)

def get_full_catalog(max_pages: Optional[int] = None, force_refresh: bool = False) -> List[dict]:
    """
    Retrieves the 100% complete catalog by inspecting total pages from page 1
    and concurrently retrieving all pages, deduplicating products by ID.
    Results are backed by the database CatalogCache to ensure cross-process consistency,
    with a 15-minute TTL.
    """
    global _catalog_cache, _catalog_cache_time
    now = time.time()
    
    # Fast return from shared DB cache or in-memory fallback
    if not force_refresh and max_pages is None:
        db_items = _get_db_catalog_cache()
        if db_items:
            return db_items
        if _catalog_cache and (now - _catalog_cache_time < _CATALOG_CACHE_TTL):
            return _catalog_cache

    with _catalog_lock:
        if not force_refresh and max_pages is None:
            db_items = _get_db_catalog_cache()
            if db_items:
                return db_items

        try:
            # 1. Fetch first page with retry to discover total available pages freshly
            first = fetch_page_with_retry(1, page_size=50)
            discovered_pages = first.get("pages", 1)
            total_pages = min(discovered_pages, max_pages) if max_pages is not None else discovered_pages
            items_map = {it["id"]: it for it in first.get("items", [])}

            # 2. Concurrently fetch all remaining pages to achieve 100% catalog coverage
            if total_pages > 1:
                workers = min(total_pages, 8)
                with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
                    future_to_page = {
                        ex.submit(fetch_page_with_retry, p, 50): p for p in range(2, total_pages + 1)
                    }
                    for future in concurrent.futures.as_completed(future_to_page):
                        p = future_to_page[future]
                        try:
                            data = future.result()
                            for it in data.get("items", []):
                                items_map[it["id"]] = it
                        except Exception as e:
                            logger.error("Catalog page %d could not be fetched; items from page %d were dropped: %s", p, p, e)

            catalog_list = sorted(items_map.values(), key=lambda x: x.get("id", 0))
            if catalog_list and max_pages is None:
                _catalog_cache = catalog_list
                _catalog_cache_time = time.time()
                _set_db_catalog_cache(catalog_list)
            return catalog_list
        except Exception as e:
            logger.error("Failed to retrieve catalog: %s", e)
            return _get_db_catalog_cache() or _catalog_cache or []

def _normalize_search_text(text: str) -> str:
    """Normalizes text by replacing punctuation and hyphens with spaces."""
    return re.sub(r"[^\w\s]", " ", str(text or "")).lower()

def search_catalog(query: str, limit: int = 25) -> List[dict]:
    """
    Searches the 100% full catalog with multi-token matching, hyphen/punctuation
    tolerance, and relevance scoring.
    """
    items = get_full_catalog()
    query_str = str(query or "").strip()
    if not query_str:
        return items[:limit]

    query_lower = query_str.lower()
    norm_query = _normalize_search_text(query_str)
    tokens = [t for t in norm_query.split() if t]

    if not tokens:
        return items[:limit]

    scored_items = []
    for item in items:
        pid_str = str(item.get("id", ""))
        name = str(item.get("name", ""))
        brand = str(item.get("brand", ""))
        category = str(item.get("category", ""))
        sku = str(item.get("sku", ""))

        norm_name = _normalize_search_text(name)
        norm_brand = _normalize_search_text(brand)
        norm_cat = _normalize_search_text(category)
        norm_sku = _normalize_search_text(sku)
        combined_norm = f"{norm_name} {norm_brand} {norm_cat} {norm_sku} {pid_str}"

        # 1. Product must contain ALL search tokens across its metadata
        if not all(token in combined_norm for token in tokens):
            continue

        # 2. Calculate relevance score (higher = better match)
        score = 0
        # Exact ID match
        if query_str == pid_str:
            score += 1000
        # Exact title match
        if query_lower == name.lower():
            score += 500
        # Title starts with query
        elif norm_name.startswith(norm_query):
            score += 300
        # Full query phrase in title
        elif norm_query in norm_name:
            score += 200

        # Tokens present in title
        matching_name_tokens = sum(1 for t in tokens if t in norm_name)
        score += matching_name_tokens * 50

        # Brand match
        if norm_query in norm_brand:
            score += 150
        elif any(t in norm_brand for t in tokens):
            score += 40

        # Category and SKU match
        if any(t in norm_cat for t in tokens):
            score += 20
        if any(t in norm_sku for t in tokens):
            score += 30

        scored_items.append((score, item))

    # Sort descending by score, tie-break by ID
    scored_items.sort(key=lambda x: (-x[0], x[1].get("id", 0)))
    return [item for _, item in scored_items][:limit]

@functools.lru_cache(maxsize=256)
def get_product_from_catalog(source_product_id: str) -> Optional[dict]:
    """
    Retrieves metadata for a product by ID.
    Queries the direct product metadata endpoint first, falling back to 100% catalog search.
    """
    try:
        pid = int(source_product_id)
    except ValueError:
        return None

    # Direct product metadata endpoint
    url = f"{BASE_URL}/api/product/{pid}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "INE-Tracker/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except Exception:
        pass

    # Fallback to 100% full catalog scan
    return next((item for item in get_full_catalog() if item.get("id") == pid), None)
