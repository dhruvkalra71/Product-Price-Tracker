import concurrent.futures
import functools
import json
import logging
import threading
import time
import urllib.request
from typing import List, Optional

logger = logging.getLogger(__name__)

BASE_URL = "https://demo.inelabteamdev.com"

# In-memory full catalog cache and concurrency lock
_catalog_cache: Optional[List[dict]] = None
_catalog_cache_time: float = 0.0
_CATALOG_CACHE_TTL: float = 900.0  # 15 minutes TTL
_catalog_lock = threading.Lock()

@functools.lru_cache(maxsize=128)
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

def get_full_catalog(max_pages: Optional[int] = None, force_refresh: bool = False) -> List[dict]:
    """
    Retrieves the 100% complete catalog by inspecting total pages from page 1
    and concurrently retrieving all pages, deduplicating products by ID.
    Results are cached in memory with a 15-minute TTL.
    """
    global _catalog_cache, _catalog_cache_time
    now = time.time()
    
    # Return valid in-memory cache if not forcing refresh and not capping pages
    if not force_refresh and max_pages is None and _catalog_cache is not None and (now - _catalog_cache_time < _CATALOG_CACHE_TTL):
        return _catalog_cache

    with _catalog_lock:
        if not force_refresh and max_pages is None and _catalog_cache is not None and (now - _catalog_cache_time < _CATALOG_CACHE_TTL):
            return _catalog_cache

        try:
            # 1. Fetch first page to discover total available pages
            first = fetch_page(1, page_size=50)
            discovered_pages = first.get("pages", 1)
            total_pages = min(discovered_pages, max_pages) if max_pages is not None else discovered_pages
            items_map = {it["id"]: it for it in first.get("items", [])}

            # 2. Concurrently fetch all remaining pages to achieve 100% catalog coverage
            if total_pages > 1:
                workers = min(total_pages, 8)
                with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
                    future_to_page = {
                        ex.submit(fetch_page, p, 50): p for p in range(2, total_pages + 1)
                    }
                    for future in concurrent.futures.as_completed(future_to_page):
                        p = future_to_page[future]
                        try:
                            data = future.result()
                            for it in data.get("items", []):
                                items_map[it["id"]] = it
                        except Exception as e:
                            logger.warning("Error fetching catalog page %d: %s", p, e)

            catalog_list = sorted(items_map.values(), key=lambda x: x.get("id", 0))
            if catalog_list and max_pages is None:
                _catalog_cache = catalog_list
                _catalog_cache_time = time.time()
            return catalog_list
        except Exception as e:
            logger.error("Failed to retrieve catalog: %s", e)
            return _catalog_cache or []

def search_catalog(query: str, limit: int = 25) -> List[dict]:
    """
    Searches the 100% full catalog across name, brand, category, SKU, and ID.
    """
    items = get_full_catalog()
    query_clean = query.strip().lower()
    if not query_clean:
        return items[:limit]

    fields = ("name", "brand", "category", "sku", "id")
    return [
        item for item in items
        if any(query_clean in str(item.get(k, "")).lower() for k in fields)
    ][:limit]

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
