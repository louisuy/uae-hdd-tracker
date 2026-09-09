"""
Playwright-based Amazon.ae HDD scraper.
Extracts real hard drives (4TB to 28TB+), calculates AED/TB, flags CMR/SMR for RAID.
"""
import re
import db
from playwright.sync_api import sync_playwright
from rich.console import Console

console = Console()

# Exclude non-drive products (enclosures, docks, cables, power supplies, fake flash drives)
EXCLUDE_KEYWORDS = [
    # Non-drive accessories & docks
    "enclosure", "docking", "dock station", "cloner", "duplicator", "diskless",
    "caddy", "adapter", "housing", "cable", "hub", "nvme", "m.2", "ssd",
    "hard drive bag", "carrying case", "protective box", "silicone case",
    "drive bracket", "mounting bracket", "thermal pad", "heatsink",
    # Power supplies & chargers
    "12v", "power supply", "power adapter", "ac adapter", "charger", "replacement cord",
    "power lead", "dc power", "for my book", "for western digital",
    # Fake flash drive / scam thumb stick keywords
    "type c usb 2.0 portable", "usb 2.0 portable", "ultra slim portable hard",
    "shockproof anti-drop", "pocket design", "ln2", "harddrive9", "anti-drop",
    "2.5'' aluminum portable", "2.5\" aluminum", "mini portable"
]

# Recognizes known storage brands, lines, and drive attributes
BRAND_AND_LINE_PATTERNS = [
    r'\bseagate\b', r'\bwestern digital\b', r'\bwd\b', r'\btoshiba\b',
    r'\bhgst\b', r'\bhitachi\b', r'\bmaxdigitaldata\b', r'\bmdd\b',
    r'\bsynology\b', r'\bqnap\b', r'\bwater panther\b', r'\bwaterpanther\b',
    r'\bironwolf\b', r'\bexos\b', r'\bbarracuda\b',
    r'\bultrastar\b', r'\bmy book\b', r'\belements\b', r'\bexpansion\b',
    r'\bone touch\b', r'\bn300\b', r'\bred plus\b', r'\bred pro\b',
    r'\bwd red\b', r'\bwd gold\b', r'\bwd purple\b', r'\bskyhawk\b',
    r'\blacie\b', r'\bsandisk professional\b', r'\bg-drive\b',
    r'\bavolusion\b', r'\bunionsine\b', r'\bkinhank\b', r'\bwhite label\b',
    r'\bneno tech\b', r'\bneno\b'
]

TECH_SPEC_PATTERNS = [
    r'\b7200\s*rpm\b', r'\b5400\s*rpm\b', r'\b7200rpm\b', r'\b5400rpm\b',
    r'3\.5\s*(?:inch|\"|\'\'|in)\b', r'2\.5\s*(?:inch|\"|\'\'|in)\b', r'\bsata\b', r'\bsas\b',
    r'\benterprise\b', r'\bnas\b', r'\bhard\s*drive\b', r'\bhdd\b',
    r'\bhard\s*disk\b', r'\bdesktop\s*(?:drive|storage|usb)\b',
    r'\bsurveillance\b', r'\bhelium\b', r'\bcmr\b'
]

# Realistic price floors by capacity (AED) to filter out mismatched/hijacked listings
# (e.g. A 1TB drive listed under a 6TB title for 380 AED)
CAPACITY_PRICE_FLOORS = {
    1: 75,
    2: 140,
    3: 200,
    4: 260,
    6: 420,
    8: 520,
    10: 650,
    12: 750,
    14: 850,
    16: 950,
    18: 1100,
    20: 1250,
    22: 1400,
    24: 1600,
}


def parse_capacity(title: str) -> int | None:
    t = title.lower()
    # Remove "up to XTB" (enclosure/dock claims)
    t = re.sub(r'(?:up to|support(?:s)?|max(?:imum)?)\s+\d+\s*(?:\*\s*\d+\s*)?tb', '', t)
    # Remove "XTB * N" patterns (multi-bay enclosures like "5 x 16TB")
    t = re.sub(r'\d+\s*[x×]\s*\d+\s*tb', '', t)

    # Match: 1TB, 2TB, 3TB, 4TB, 6TB, 8TB, 10TB, 12 TB, etc.
    match = re.search(r'\b([1-9]|[1-2][0-9]|3[0-2])(?:\.0)?\s*[-–]?\s*(?:tb|terabyte|terabytes)\b', t)
    if match:
        cap = int(match.group(1))
        if 1 <= cap <= 32:
            return cap
    return None


def parse_price(price_str: str) -> float | None:
    if not price_str:
        return None
    clean = re.sub(r'[^\d.]', '', price_str.replace(',', ''))
    try:
        return float(clean)
    except ValueError:
        return None


def is_real_hdd(title: str) -> bool:
    t = title.lower()
    for kw in EXCLUDE_KEYWORDS:
        if kw in t:
            return False
            
    # MUST have a recognized, legitimate storage brand or line
    has_brand = any(re.search(pat, t) for pat in BRAND_AND_LINE_PATTERNS)
    return has_brand


def classify_drive(title: str) -> str:
    t = title.lower()
    if any(k in t for k in ["exos", "ultrastar", "mg0", "mg1", "enterprise", "he10", "he12", "he14", "dc hc", "gold"]):
        return "Enterprise"
    if any(k in t for k in ["ironwolf", "red plus", "red pro", "wd red", "n300", "nas"]):
        return "NAS"
    if any(k in t for k in ["elements", "my book", "expansion", "one touch", "external", "desktop usb", "usb 3", "g-drive"]):
        return "External"
    if any(k in t for k in ["skyhawk", "purple", "surveillance", "dvr"]):
        return "Surveillance"
    return "Internal"


def detect_recording_tech(title: str, capacity: int) -> str:
    """
    Flags whether a drive is CMR (safe for RAID 5/6 rebuilds) or SMR (causes RAID timeout).
    Drives >= 10TB in 3.5" are almost universally CMR.
    1TB-8TB drives can be either CMR or SMR.
    """
    t = title.lower()
    # Explicit SMR indicators
    if "smr" in t or "barracuda" in t:
        return "SMR ⚠️"
    # WD Red non-plus / non-pro (EFAX models in 2-6TB) are SMR
    if "wd red" in t and not any(k in t for k in ["plus", "pro", "efrx"]):
        if capacity in [2, 3, 4, 6]:
            return "SMR ⚠️"

    # Known CMR lines
    if any(k in t for k in ["cmr", "ironwolf", "exos", "ultrastar", "red plus", "red pro", "n300", "mg0", "gold", "purple"]):
        return "CMR ✅"

    # In 3.5" drives, 10TB+ are all CMR
    if capacity >= 10:
        return "CMR ✅"

    return "CMR ✅" if "nas" in t or "enterprise" in t else "Unknown"


def scrape_amazon_hdds(
    queries: list[str] | None = None,
    min_capacity: int = 1,
    include_renewed: bool = True,
    progress_callback = None,
    batch_callback = None
) -> list[dict]:
    if queries is None:
        queries = [
            # ── 1TB, 2TB, 3TB Budget RAID / Storage ──
            "1TB HDD 3.5", "1TB hard drive 3.5", "2TB HDD 3.5", "2TB hard drive 3.5",
            "Seagate IronWolf 2TB", "WD Red Plus 2TB", "WD Blue 2TB 3.5", "Seagate Barracuda 2TB",
            "3TB HDD", "3TB hard drive",

            # ── 4TB, 6TB, 8TB Mid-Range RAID 5 ──
            "8TB HDD", "8TB hard drive", "8TB SATA",
            "6TB HDD", "6TB hard drive",
            "4TB HDD", "4TB hard drive", "4TB SATA",
            "Seagate IronWolf 8TB", "Seagate IronWolf 4TB", "Seagate IronWolf 6TB",
            "WD Red Plus 8TB", "WD Red Plus 4TB", "WD Red Plus 6TB",
            "Toshiba N300 8TB", "Toshiba N300 4TB",
            "WD Ultrastar 8TB", "Seagate Exos 8TB",
            "WD Elements 8TB", "WD Elements 6TB", "WD Elements 4TB",
            "WD My Book 8TB", "Seagate Expansion 8TB",

            # ── 10TB to 24TB High Capacity ──
            "10TB HDD", "12TB HDD", "14TB HDD", "16TB HDD", "18TB HDD", "20TB HDD", "22TB HDD",
            "10TB hard drive", "12TB hard drive", "14TB hard drive", "16TB hard drive", "18TB hard drive", "20TB hard drive",
            "14TB SATA", "16TB SATA", "18TB SATA", "20TB SATA", "22TB SATA",
            "Seagate Exos 16TB", "Seagate Exos 18TB", "Seagate Exos 20TB", "Seagate Exos 22TB",
            "Seagate IronWolf 12TB", "Seagate IronWolf 16TB", "Seagate IronWolf 18TB",
            "WD Ultrastar 14TB", "WD Ultrastar 16TB", "WD Ultrastar 18TB", "WD Ultrastar 20TB",
            "WD Red Plus 12TB", "WD Red Pro 14TB", "WD Red Pro 16TB", "WD Red Pro 18TB",
            "Toshiba MG 14TB", "Toshiba MG 16TB", "Toshiba MG 18TB", "Toshiba N300 12TB", "Toshiba N300 16TB",
            "WD Elements Desktop 14TB", "WD Elements Desktop 18TB", "WD Elements Desktop 20TB", "WD Elements 22TB",
            "WD My Book 14TB", "WD My Book 18TB", "WD My Book 22TB",
            "Seagate Expansion Desktop 14TB", "Seagate Expansion Desktop 16TB", "Seagate Expansion Desktop 18TB", "Seagate Expansion 20TB"
        ]

    results = []
    seen_asins = set()
    total_queries = len(queries)
    try:
        ignored_asins = db.get_ignored_asins()
    except Exception:
        ignored_asins = set()

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

        for idx, q in enumerate(queries, 1):
            if progress_callback:
                try:
                    progress_callback(idx, total_queries, q, len(seen_asins))
                except Exception:
                    pass

            url = f"https://www.amazon.ae/s?k={q.replace(' ', '+')}"
            console.print(f"[cyan]🔍 ({idx}/{total_queries})[/cyan] {q}...")
            query_batch = []

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=25000)
                page.wait_for_timeout(1000)

                items = page.locator('div[data-component-type="s-search-result"]')
                count = items.count()

                for i in range(count):
                    item = items.nth(i)
                    asin = item.get_attribute("data-asin")
                    if not asin or asin in seen_asins or asin in ignored_asins:
                        continue

                    title_el = item.locator("h2")
                    if title_el.count() == 0:
                        continue
                    title = title_el.first.inner_text().strip().replace("\n", " ")

                    if not is_real_hdd(title):
                        continue

                    capacity = parse_capacity(title)
                    if not capacity or capacity < min_capacity:
                        continue

                    price_el = item.locator(".a-price .a-offscreen")
                    if price_el.count() == 0:
                        continue
                    price = parse_price(price_el.first.inner_text())
                    
                    # Check capacity price floor to reject mismatched titles (e.g. 1TB drive listed under a 6TB title)
                    is_renewed = any(
                        k in title.lower()
                        for k in ["renewed", "refurbished", "recertified"]
                    )
                    if not include_renewed and is_renewed:
                        continue

                    floor = CAPACITY_PRICE_FLOORS.get(capacity, capacity * 60)
                    min_allowed_price = floor * 0.65 if is_renewed else floor
                    if not price or price < min_allowed_price or (price / capacity) < 25:
                        continue

                    seen_asins.add(asin)
                    drive_obj = {
                        "asin": asin,
                        "title": title,
                        "capacity": capacity,
                        "price": round(price, 2),
                        "aed_per_tb": round(price / capacity, 2),
                        "category": classify_drive(title),
                        "recording_tech": detect_recording_tech(title, capacity),
                        "is_renewed": is_renewed,
                        "has_prime": item.locator(".a-icon-prime").count() > 0,
                        "url": f"https://www.amazon.ae/dp/{asin}",
                    }
                    results.append(drive_obj)
                    query_batch.append(drive_obj)

                # Stream this query's newly discovered batch immediately to database
                if batch_callback and query_batch:
                    try:
                        batch_callback(query_batch)
                    except Exception as b_err:
                        console.print(f"[yellow]Batch callback error:[/yellow] {b_err}")

            except Exception as e:
                console.print(f"[red]Error:[/red] {q} — {e}")

        browser.close()

    results.sort(key=lambda x: x["aed_per_tb"])
    console.print(f"[green]✓ Found {len(results)} drives[/green]")
    return results


if __name__ == "__main__":
    import json
    deals = scrape_amazon_hdds(min_capacity=4)
    print(json.dumps(deals[:10], indent=2))
