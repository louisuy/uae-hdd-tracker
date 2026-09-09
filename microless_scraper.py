"""
Playwright-based scraper for Microless UAE (uae.microless.com).
Extracts internal and external HDDs with model numbers, AED prices, and stock status.
"""
import re
from playwright.sync_api import sync_playwright
from rich.console import Console

import db
from scraper import (
    parse_capacity,
    parse_price,
    is_real_hdd,
    classify_drive,
    detect_recording_tech,
    CAPACITY_PRICE_FLOORS
)

console = Console()

MICROLESS_CATEGORY_URLS = [
    ("https://uae.microless.com/internal_hard_drives/?page={page}", "Internal HDDs"),
    ("https://uae.microless.com/external_hard_drives/?page={page}", "External HDDs"),
]


def extract_model_number(attribute_text: str, title: str) -> str:
    """Extracts explicit model number like ST10000VN000 or WDBBGB0140HBK-EESN if present."""
    if attribute_text:
        m = re.search(r'Model:\s*([A-Za-z0-9_-]+)', attribute_text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
            
    # Try finding model number at end of title (e.g., "| ST10000VN000" or "| WDBBGB0140HBK-EESN")
    m_title = re.search(r'[\|\(]\s*([A-Za-z0-9_-]{5,25})\s*[\)\/]?$', title)
    if m_title:
        return m_title.group(1).strip()
        
    return ""


def scrape_microless_hdds(
    min_capacity: int = 1,
    include_renewed: bool = True,
    progress_callback = None,
    batch_callback = None
) -> list[dict]:
    """
    Crawls Microless UAE hard drive categories and extracts real mechanical HDDs.
    """
    results = []
    seen_ids = set()

    try:
        ignored_asins = db.get_ignored_asins()
    except Exception:
        ignored_asins = set()

    pages_to_crawl = []
    for base_url, cat_name in MICROLESS_CATEGORY_URLS:
        # We check pages 1, 2, 3 for each category
        for p in range(1, 4):
            pages_to_crawl.append((base_url.format(page=p), f"{cat_name} (p.{p})"))

    total_pages = len(pages_to_crawl)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-gpu",
            ]
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
            locale="en-AE",
        )
        page = context.new_page()
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        for idx, (url, label) in enumerate(pages_to_crawl, 1):
            if progress_callback:
                try:
                    progress_callback(idx, total_pages, f"Microless: {label}", len(seen_ids))
                except Exception:
                    pass

            console.print(f"[magenta]🔍 [Microless] ({idx}/{total_pages})[/magenta] {label}...")
            query_batch = []

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(1500)

                items = page.locator(".product")
                count = items.count()
                if count == 0:
                    console.print(f"[yellow]No more products on {label}, moving to next section.[/yellow]")
                    continue

                for i in range(count):
                    item = items.nth(i)
                    prod_id = item.get_attribute("data-prodid")
                    if not prod_id:
                        continue
                        
                    asin_key = f"ML-{prod_id}"
                    if asin_key in seen_ids or asin_key in ignored_asins:
                        continue

                    title_el = item.locator(".product-title a")
                    if title_el.count() == 0:
                        continue
                    title = title_el.first.inner_text().strip().replace("\n", " ")
                    product_url = title_el.first.get_attribute("href") or f"https://uae.microless.com/product/{prod_id}/"
                    if not product_url.startswith("http"):
                        product_url = f"https://uae.microless.com{product_url}"

                    if not is_real_hdd(title):
                        continue

                    capacity = parse_capacity(title)
                    if not capacity or capacity < min_capacity:
                        continue

                    # Extract price
                    price_el = item.locator(".product-price .price-amount")
                    if price_el.count() == 0:
                        continue
                    price_text = price_el.first.inner_text()
                    price = parse_price(price_text)
                    if not price or price <= 0:
                        continue

                    # Model number
                    attr_el = item.locator(".attribute-list")
                    attr_text = attr_el.first.inner_text() if attr_el.count() > 0 else ""
                    model_num = extract_model_number(attr_text, title)

                    # Condition & price floor check
                    is_renewed = any(
                        k in title.lower()
                        for k in ["renewed", "refurbished", "recertified", "used"]
                    )
                    if not include_renewed and is_renewed:
                        continue

                    floor = CAPACITY_PRICE_FLOORS.get(capacity, capacity * 60)
                    min_allowed_price = floor * 0.65 if is_renewed else floor
                    if price < min_allowed_price or (price / capacity) < 25:
                        continue

                    seen_ids.add(asin_key)
                    drive_obj = {
                        "asin": asin_key,
                        "title": title,
                        "capacity": capacity,
                        "price": round(price, 2),
                        "aed_per_tb": round(price / capacity, 2),
                        "category": classify_drive(title),
                        "recording_tech": detect_recording_tech(title, capacity),
                        "is_renewed": is_renewed,
                        "store": "Microless",
                        "model_number": model_num,
                        "url": product_url,
                    }
                    results.append(drive_obj)
                    query_batch.append(drive_obj)

                # Stream to DB via batch_callback
                if batch_callback and query_batch:
                    try:
                        batch_callback(query_batch)
                    except Exception as b_err:
                        console.print(f"[yellow]Batch callback error:[/yellow] {b_err}")

            except Exception as e:
                console.print(f"[red]Error on {label}:[/red] {e}")

        browser.close()

    results.sort(key=lambda x: x["aed_per_tb"])
    console.print(f"[green]✓ Found {len(results)} Microless drives[/green]")
    return results


if __name__ == "__main__":
    import json
    deals = scrape_microless_hdds(min_capacity=1)
    print(f"Total found: {len(deals)}")
    if deals:
        print(json.dumps(deals[:5], indent=2))
