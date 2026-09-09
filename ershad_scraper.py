"""
Lightweight scraper for Al Ershad Infotech (alershadonline.com).
Al Ershad is an established Computer Plaza (Al Ain Centre) retailer in Bur Dubai.
Uses their high-speed Shopify search catalog API.
"""
import urllib.request
import urllib.parse
import json
import ssl
import re

import db
from scraper import (
    parse_capacity,
    is_real_hdd,
    classify_drive,
    detect_recording_tech,
    CAPACITY_PRICE_FLOORS
)

ERSHAD_QUERIES = [
    "ironwolf",
    "wd red",
    "seagate exos",
    "toshiba nas",
    "hard drive",
    "internal hdd",
    "nas hdd",
    "enterprise hdd",
    "wd elements",
    "seagate expansion"
]


def extract_model_from_title(title: str) -> str:
    """Extracts MPN like ST10000VN000, WD40EFZZ, or WUH722422ALE6L4."""
    m = re.search(r'[\|\(]\s*([A-Za-z0-9_-]{5,25})\s*[\)\/]?$', title)
    if m:
        return m.group(1).strip()
    return ""


def scrape_ershad_hdds(
    min_capacity: int = 1,
    progress_callback = None,
    batch_callback = None
) -> list[dict]:
    """
    Scrapes Al Ershad online catalog for mechanical hard drives.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    results = []
    seen_ids = set()

    try:
        ignored = db.get_ignored_asins()
    except Exception:
        ignored = set()

    total_q = len(ERSHAD_QUERIES)

    for idx, q in enumerate(ERSHAD_QUERIES, 1):
        if progress_callback:
            try:
                progress_callback(idx, total_q, f"Al Ershad: {q}", len(seen_ids))
            except Exception:
                pass

        url = f"https://alershadonline.com/search/suggest.json?q={urllib.parse.quote(q)}&resources[type]=product"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko)"
            }
        )

        query_batch = []
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=12) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                items = data.get("resources", {}).get("results", {}).get("products", [])

                for item in items:
                    pid = str(item.get("id", ""))
                    if not pid:
                        continue

                    asin_key = f"ERSHAD-{pid}"
                    if asin_key in seen_ids or asin_key in ignored:
                        continue

                    title = item.get("title", "").strip()
                    if not is_real_hdd(title):
                        continue

                    capacity = parse_capacity(title)
                    if not capacity or capacity < min_capacity:
                        continue

                    price_raw = item.get("price")
                    if not price_raw:
                        continue
                    try:
                        price = float(price_raw)
                    except ValueError:
                        continue

                    if price <= 0:
                        continue

                    # Validate price floor
                    floor = CAPACITY_PRICE_FLOORS.get(capacity, capacity * 60)
                    if price < floor or (price / capacity) < 25:
                        continue

                    seen_ids.add(asin_key)
                    model_num = extract_model_from_title(title)
                    product_url = f"https://alershadonline.com{item.get('url', '')}"

                    drive_obj = {
                        "asin": asin_key,
                        "title": title,
                        "capacity": capacity,
                        "price": round(price, 2),
                        "aed_per_tb": round(price / capacity, 2),
                        "category": classify_drive(title),
                        "recording_tech": detect_recording_tech(title, capacity),
                        "is_renewed": False,
                        "store": "Al Ershad",
                        "model_number": model_num,
                        "url": product_url,
                    }
                    results.append(drive_obj)
                    query_batch.append(drive_obj)

            if batch_callback and query_batch:
                try:
                    batch_callback(query_batch)
                except Exception as b_err:
                    print(f"Al Ershad batch error: {b_err}")

        except Exception as e:
            print(f"Al Ershad query error for '{q}': {e}")

    results.sort(key=lambda x: x["aed_per_tb"])
    return results


if __name__ == "__main__":
    drives = scrape_ershad_hdds()
    print(f"Found {len(drives)} Al Ershad drives")
    for d in drives[:5]:
        print(d)
