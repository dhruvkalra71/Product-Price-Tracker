import concurrent.futures
import functools
import json
import urllib.request
from typing import List, Optional

BASE_URL = "https://demo.inelabteamdev.com"

@functools.lru_cache(maxsize=64)
def fetch_page(page: int = 1, page_size: int = 20) -> dict:
    url = f"{BASE_URL}/api/catalog?page={page}&pageSize={page_size}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "INE-Tracker/1.0 (Python urllib)"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Catalog API returned HTTP {resp.status}")
        return json.loads(resp.read().decode("utf-8"))

def get_full_catalog(max_pages: int = 5) -> List[dict]:
    try:
        first = fetch_page(1, page_size=50)
        pages = min(first.get("pages", 1), max_pages)
        items = {it["id"]: it for it in first.get("items", [])}
        if pages > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(pages, 5)) as ex:
                for data in ex.map(lambda p: fetch_page(p, page_size=50), range(2, pages + 1)):
                    items.update({it["id"]: it for it in data.get("items", [])})
        return list(items.values())
    except Exception:
        return []

def search_catalog(query: str, limit: int = 20) -> List[dict]:
    query_clean = query.strip().lower()
    if not query_clean:
        return get_full_catalog(max_pages=2)[:limit]

    items = get_full_catalog(max_pages=10)
    fields = ("name", "brand", "category", "sku", "id")
    return [
        item for item in items
        if any(query_clean in str(item.get(k, "")).lower() for k in fields)
    ][:limit]

@functools.lru_cache(maxsize=128)
def get_product_from_catalog(source_product_id: str) -> Optional[dict]:
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

    # Fallback to catalog scan
    return next((item for item in get_full_catalog(max_pages=10) if item.get("id") == pid), None)
