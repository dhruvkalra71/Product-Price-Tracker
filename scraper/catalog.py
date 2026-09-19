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
    all_items = {}
    try:
        first_page = fetch_page(1, page_size=50)
        total_pages = min(first_page.get("pages", 1), max_pages)
        for item in first_page.get("items", []):
            all_items[item["id"]] = item

        for p in range(2, total_pages + 1):
            for item in fetch_page(p, page_size=50).get("items", []):
                all_items[item["id"]] = item
    except Exception:
        pass
    return list(all_items.values())

def search_catalog(query: str, limit: int = 20) -> List[dict]:
    query_clean = query.strip().lower()
    if not query_clean:
        return get_full_catalog(max_pages=2)[:limit]

    items = get_full_catalog(max_pages=10)
    matches = []
    fields = ("name", "brand", "category", "sku", "id")
    for item in items:
        if any(query_clean in str(item.get(k, "")).lower() for k in fields):
            matches.append(item)
            if len(matches) >= limit:
                break
    return matches

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
    for item in get_full_catalog(max_pages=10):
        if item.get("id") == pid:
            return item
    return None
