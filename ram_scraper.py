"""
RAM scraper module for Amazon.ae, Microless, and Al Ershad Infotech.
Extracts DDR5, DDR4, DDR3 across Desktop UDIMM, Laptop/NAS SODIMM, and Server ECC.
Computes AED/GB value metrics and tracks all-time lows.
"""
import json
import logging
import os
import re
import ssl
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright
from rich.console import Console

import db

console = Console()
logger = logging.getLogger("ram-scraper")

# Non-memory keywords & accessories to discard
EXCLUDE_KEYWORDS = [
    "heatsink", "heat sink", "heat spreader", "thermal pad", "thermal paste", "thermo pad",
    "cooler", "cooling vest", "copper heatsink", "rgb dummy", "dummy light", "dummy module",
    "light enhancement kit", "lighting kit", "memory fan", "cooler fan", "liquid cooler",
    "enclosure", "adapter card", "riser card", "pci-e", "pcie card", "docking", "dock station",
    "caddy", "hard drive bag", "protective box", "case only", "motherboard", "graphics card",
    "gpu", "rtx", "gtx", "radeon", "cpu processor", "core i7", "core i9", "ryzen 7", "ryzen 9",
    "nvme", "m.2", "solid state drive", "sata ssd", "ssd drive", "flash drive", "pen drive",
    "thumb drive", "sd card", "microsd", "tf card", "hard drive", "hdd", "portable drive",
    "power supply", "psu", "smartphone", "iphone", "galaxy s", "tablet", "ipad", "laptop computer",
    "notebook computer", "desktop pc", "mini pc with", "gaming pc", "prebuilt"
]

RECOGNIZED_BRANDS = [
    "corsair", "g.skill", "gskill", "kingston", "crucial", "teamgroup", "t-force", "t-create",
    "adata", "xpg", "patriot", "viper", "lexar", "silicon power", "pny", "samsung", "sk hynix",
    "hynix", "micron", "owc", "timetec", "klevv", "transcend", "oloy", "geil", "kingspec",
    "fanxiang", "integral", "netac", "acer predator", "apacer", "v-color", "mushkin"
]

PC_SPEED_MAP = {
    "pc5-51200": 6400, "pc5-48000": 6000, "pc5-44800": 5600, "pc5-41600": 5200, "pc5-38400": 4800,
    "pc4-28800": 3600, "pc4-25600": 3200, "pc4-23400": 2933, "pc4-21300": 2666, "pc4-19200": 2400,
    "pc4-17000": 2133, "pc3-14900": 1866, "pc3-12800": 1600, "pc3-10600": 1333, "pc3-8500": 1066
}


def parse_price(price_str: str) -> float | None:
    if not price_str:
        return None
    clean = re.sub(r'[^\d.]', '', str(price_str).replace(',', ''))
    try:
        val = float(clean)
        return round(val, 2) if val > 0 else None
    except ValueError:
        return None


def parse_ram_specs(title: str, attribute_text: str = "") -> dict | None:
    """
    Parses title and attribute text to extract RAM specs:
    - generation: DDR5, DDR4, DDR3, DDR2
    - form_factor: Desktop (UDIMM), Laptop / NAS (SODIMM), Server (ECC)
    - capacity_gb: Total capacity in GB
    - kit_config: e.g. 2x16GB, 1x32GB, 2x32GB
    - speed_mhz: e.g. 6000, 5600, 3200, 1600
    - cas_latency: e.g. CL36, CL40, CL16
    - model_number: MPN if present
    """
    combined = f"{title} {attribute_text}".lower()

    # 1. Anti-pollution filtering
    for kw in EXCLUDE_KEYWORDS:
        if kw in combined:
            return None

    # Must contain a memory keyword or recognized RAM brand
    has_ram_kw = any(k in combined for k in [
        "ram", "memory", "dimm", "sodimm", "so-dimm", "sodim", "ddr5", "ddr4", "ddr3", "ddr2", "dram"
    ])
    has_brand = any(b in combined for b in RECOGNIZED_BRANDS)
    if not (has_ram_kw or has_brand):
        return None

    # 2. Generation
    generation = None
    if re.search(r"\bddr5\b|\bpc5\b|\blpddr5\b", combined):
        generation = "DDR5"
    elif re.search(r"\bddr4\b|\bpc4\b|\blpddr4\b", combined):
        generation = "DDR4"
    elif re.search(r"\bddr3l?\b|\bpc3l?\b", combined):
        generation = "DDR3"
    elif re.search(r"\bddr2\b|\bpc2\b", combined):
        generation = "DDR2"
    else:
        return None

    # 3. Form Factor
    # Distinguish true server ECC RDIMM from consumer DDR5 on-die ECC
    is_server_ecc = (
        ("rdimm" in combined or "lrdimm" in combined or "server memory" in combined or "server ram" in combined)
        or ("ecc" in combined and "registered" in combined)
    )
    is_sodimm = any(k in combined for k in [
        "sodimm", "so-dimm", "sodim", "laptop", "notebook", "csodimm",
        "260-pin", "262-pin", "204-pin"
    ])

    if is_server_ecc:
        form_factor = "Server (ECC)"
    elif is_sodimm:
        form_factor = "Laptop / NAS (SODIMM)"
    else:
        form_factor = "Desktop (UDIMM)"

    # 4. Capacity and Kit Configuration
    total_gb = None
    kit_config = "1x"

    # Pattern A: "32GB (2x16GB)" or "32GB Kit (2x16GB)" or "32GB Kit (16GBx2)"
    m_full = re.search(r"(\d+)\s*gb[^\(\)]*?\(\s*(\d+)\s*[xX*]\s*(\d+)\s*gb\s*\)", combined)
    if m_full:
        declared_total = int(m_full.group(1))
        count = int(m_full.group(2))
        single = int(m_full.group(3))
        if count in [1, 2, 4, 8] and single in [4, 8, 16, 24, 32, 48, 64, 128]:
            calc_total = count * single
            total_gb = declared_total if declared_total == calc_total else calc_total
            kit_config = f"{count}x{single}GB"

    # Pattern A.2: Explicit single variant in parentheses at end of title e.g. "(16GB)" or "[32GB]"
    if not total_gb:
        m_end = re.search(r"[\(\[]\s*(\d+)\s*gb\s*[\)\]]\s*$", title.strip(), re.IGNORECASE)
        if m_end:
            v_cap = int(m_end.group(1))
            if v_cap in [4, 8, 16, 24, 32, 48, 64, 96, 128]:
                total_gb = v_cap
                kit_config = f"1x{total_gb}GB"

    # Pattern B: "32GB (2x 16GB)" or "64GB (2 x 32GB)"
    if not total_gb:
        m_kit = re.search(r"\b(\d+)\s*[xX*]\s*(\d+)\s*gb\b", combined)
        if m_kit:
            count = int(m_kit.group(1))
            single = int(m_kit.group(2))
            if count in [1, 2, 4, 8] and single in [2, 4, 8, 16, 24, 32, 48, 64, 128]:
                total_gb = count * single
                kit_config = f"{count}x{single}GB"

    # Pattern C: "16GBx2" or "16GB x 2" or "32GBx2"
    if not total_gb:
        m_rev = re.search(r"\b(\d+)\s*gb\s*[xX*]\s*(\d+)\b", combined)
        if m_rev:
            single = int(m_rev.group(1))
            count = int(m_rev.group(2))
            if count in [1, 2, 4, 8] and single in [2, 4, 8, 16, 24, 32, 48, 64, 128]:
                total_gb = count * single
                kit_config = f"{count}x{single}GB"

    # Pattern D: Standalone capacity like "32GB", "16GB", "64GB", "8GB"
    if not total_gb:
        caps = re.findall(r"\b(4|8|16|24|32|48|64|96|128|192|256)\s*gb\b", combined)
        if caps:
            valid_caps = [int(c) for c in caps if int(c) in [4, 8, 16, 24, 32, 48, 64, 96, 128, 192, 256]]
            if valid_caps:
                total_gb = valid_caps[0]
                kit_config = f"1x{total_gb}GB"

    if not total_gb or total_gb < 4 or total_gb > 512:
        return None

    # 5. Speed (MHz / MT/s)
    speed = 0
    m_spd = re.search(r"\b([1-8]\d{3})\s*(?:mhz|mt/s|mts)\b", combined)
    if m_spd:
        spd_val = int(m_spd.group(1))
        if 800 <= spd_val <= 8800:
            speed = spd_val

    if not speed:
        # Check PC rating
        for code, val in PC_SPEED_MAP.items():
            if code in combined:
                speed = val
                break

    if not speed:
        # Look for 4-digit speed number standalone after DDR4/DDR5
        m_ddr_spd = re.search(r"ddr[345][-\s]?([1-8]\d{3})\b", combined)
        if m_ddr_spd:
            spd_val = int(m_ddr_spd.group(1))
            if 800 <= spd_val <= 8800:
                speed = spd_val

    # Default speeds by generation if unspecified
    if not speed:
        default_speeds = {"DDR5": 5600, "DDR4": 3200, "DDR3": 1600, "DDR2": 800}
        speed = default_speeds.get(generation, 0)

    # 6. CAS Latency
    cas_latency = ""
    m_cl = re.search(r"\b(?:cl|cas)[-\s]?(\d{1,2})\b", combined)
    if m_cl:
        cl_num = int(m_cl.group(1))
        if 9 <= cl_num <= 56:
            cas_latency = f"CL{cl_num}"

    # 7. Model number / MPN
    model_number = ""
    m_mpn = re.search(r'[\|\(]\s*([A-Za-z0-9_-]{6,25})\s*[\)\/]?$', title)
    if m_mpn:
        model_number = m_mpn.group(1).strip()

    return {
        "generation": generation,
        "form_factor": form_factor,
        "capacity_gb": total_gb,
        "kit_config": kit_config,
        "speed_mhz": speed,
        "cas_latency": cas_latency,
        "model_number": model_number
    }


# ── Retailer 1: Al Ershad Infotech ──────────────────────────

ERSHAD_RAM_QUERIES = [
    "ddr5",
    "ddr4",
    "ddr3",
    "sodimm",
    "corsair ram",
    "kingston ram",
    "gskill ram",
    "crucial ram",
    "desktop memory",
    "laptop memory"
]


def scrape_ershad_ram(
    progress_callback = None,
    batch_callback = None
) -> list[dict]:
    """Scrapes Al Ershad online catalog for RAM modules."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    results = []
    seen_ids = set()

    try:
        ignored = db.get_ignored_ram()
    except Exception:
        ignored = set()

    total_q = len(ERSHAD_RAM_QUERIES)

    for idx, q in enumerate(ERSHAD_RAM_QUERIES, 1):
        if progress_callback:
            try:
                progress_callback(idx, total_q, f"Al Ershad RAM: {q}", len(seen_ids))
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

                    sku = f"ERSHAD-{pid}"
                    if sku in seen_ids or sku in ignored:
                        continue

                    title = item.get("title", "").strip()
                    specs = parse_ram_specs(title)
                    if not specs:
                        continue

                    price_raw = item.get("price", "0")
                    price = parse_price(price_raw)
                    if not price or price < 30.0:  # Minimum 30 AED floor
                        continue

                    cap_gb = specs["capacity_gb"]
                    aed_per_gb = round(price / cap_gb, 2)
                    if aed_per_gb < 1.5:
                        continue

                    prod_url = item.get("url", "")
                    if prod_url and not prod_url.startswith("http"):
                        prod_url = f"https://alershadonline.com{prod_url}"

                    ram_dict = {
                        "sku": sku,
                        "title": title,
                        "generation": specs["generation"],
                        "form_factor": specs["form_factor"],
                        "capacity_gb": cap_gb,
                        "speed_mhz": specs["speed_mhz"],
                        "kit_config": specs["kit_config"],
                        "cas_latency": specs["cas_latency"],
                        "price_aed": price,
                        "aed_per_gb": aed_per_gb,
                        "store": "Al Ershad",
                        "model_number": specs["model_number"],
                        "is_renewed": False,
                        "url": prod_url
                    }

                    seen_ids.add(sku)
                    results.append(ram_dict)
                    query_batch.append(ram_dict)

            if batch_callback and query_batch:
                batch_callback(query_batch)

        except Exception as e:
            logger.warning(f"Al Ershad query '{q}' failed: {e}")

    logger.info(f"Al Ershad RAM scrape finished: found {len(results)} kits.")
    return results


# ── Retailer 2: Microless UAE ───────────────────────────────

MICROLESS_RAM_CATEGORIES = [
    ("https://uae.microless.com/desktop-memory/?page={page}", "Desktop Memory (UDIMM)", 3),
    ("https://uae.microless.com/laptop-memory/?page={page}", "Laptop / NAS (SODIMM)", 3),
    ("https://uae.microless.com/server-memory/?page={page}", "Server Memory (ECC)", 2),
]


def scrape_microless_ram(
    progress_callback = None,
    batch_callback = None
) -> list[dict]:
    """Crawls Microless UAE memory categories using Playwright."""
    results = []
    seen_ids = set()

    try:
        ignored = db.get_ignored_ram()
    except Exception:
        ignored = set()

    pages_to_crawl = []
    for base_url, cat_name, max_pages in MICROLESS_RAM_CATEGORIES:
        for p in range(1, max_pages + 1):
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
                    progress_callback(idx, total_pages, f"Microless RAM: {label}", len(seen_ids))
                except Exception:
                    pass

            console.print(f"[magenta]🔍 [Microless RAM] ({idx}/{total_pages})[/magenta] {label}...")
            query_batch = []

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(1500)

                items = page.locator(".product")
                count = items.count()
                if count == 0:
                    continue

                for i in range(count):
                    item = items.nth(i)
                    prod_id = item.get_attribute("data-prodid")
                    if not prod_id:
                        continue

                    sku = f"ML-{prod_id}"
                    if sku in seen_ids or sku in ignored:
                        continue

                    title_el = item.locator(".product-title a")
                    if title_el.count() == 0:
                        continue
                    title = title_el.first.inner_text().strip().replace("\n", " ")
                    product_url = title_el.first.get_attribute("href") or f"https://uae.microless.com/product/{prod_id}/"
                    if not product_url.startswith("http"):
                        product_url = f"https://uae.microless.com{product_url}"

                    attr_el = item.locator(".attribute-list")
                    attr_text = attr_el.first.inner_text() if attr_el.count() > 0 else ""

                    specs = parse_ram_specs(title, attr_text)
                    if not specs:
                        continue

                    price_el = item.locator(".product-price .price-amount")
                    if price_el.count() == 0:
                        continue
                    price = parse_price(price_el.first.inner_text())
                    if not price or price < 30.0:
                        continue

                    cap_gb = specs["capacity_gb"]
                    aed_per_gb = round(price / cap_gb, 2)
                    if aed_per_gb < 1.5:
                        continue

                    is_renewed = any(
                        k in title.lower()
                        for k in ["renewed", "refurbished", "recertified", "used"]
                    )

                    ram_dict = {
                        "sku": sku,
                        "title": title,
                        "generation": specs["generation"],
                        "form_factor": specs["form_factor"],
                        "capacity_gb": cap_gb,
                        "speed_mhz": specs["speed_mhz"],
                        "kit_config": specs["kit_config"],
                        "cas_latency": specs["cas_latency"],
                        "price_aed": price,
                        "aed_per_gb": aed_per_gb,
                        "store": "Microless",
                        "model_number": specs["model_number"],
                        "is_renewed": is_renewed,
                        "url": product_url
                    }

                    seen_ids.add(sku)
                    results.append(ram_dict)
                    query_batch.append(ram_dict)

                if batch_callback and query_batch:
                    batch_callback(query_batch)

            except Exception as e:
                console.print(f"[red]Error scraping {label}: {e}[/red]")

        browser.close()

    logger.info(f"Microless RAM scrape finished: found {len(results)} kits.")
    return results


# ── Retailer 3: Amazon.ae ───────────────────────────────────

AMAZON_RAM_QUERIES = [
    # DDR5 Desktop UDIMM
    "DDR5 32GB 6000MHz",
    "DDR5 32GB RAM",
    "DDR5 64GB RAM",
    "Corsair Vengeance DDR5 32GB",
    "Kingston Fury Beast DDR5 32GB",
    "G.Skill Trident Z5 DDR5",
    "Crucial DDR5 RAM",

    # DDR5 SODIMM (Laptop / NAS)
    "DDR5 SODIMM 32GB",
    "DDR5 SODIMM 16GB",
    "Crucial DDR5 SODIMM",
    "Corsair DDR5 SODIMM",

    # DDR4 Desktop UDIMM
    "DDR4 32GB 3200MHz",
    "DDR4 16GB 3200MHz",
    "DDR4 64GB RAM",
    "Corsair Vengeance LPX DDR4 32GB",
    "Kingston Fury DDR4 32GB",

    # DDR4 SODIMM (Laptop / NAS)
    "DDR4 SODIMM 32GB",
    "DDR4 SODIMM 16GB",
    "Crucial DDR4 SODIMM 32GB",

    # DDR3 Desktop & SODIMM
    "DDR3 SODIMM 8GB",
    "DDR3 SODIMM 16GB",
    "DDR3 RAM 16GB"
]


def scrape_amazon_ram(
    queries: list[str] | None = None,
    progress_callback = None,
    batch_callback = None
) -> list[dict]:
    """Scrapes Amazon.ae for RAM modules using Playwright."""
    if queries is None:
        queries = AMAZON_RAM_QUERIES

    results = []
    seen_asins = set()

    try:
        ignored = db.get_ignored_ram()
    except Exception:
        ignored = set()

    total_queries = len(queries)

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
                    progress_callback(idx, total_queries, f"Amazon.ae RAM: {q}", len(seen_asins))
                except Exception:
                    pass

            url = f"https://www.amazon.ae/s?k={urllib.parse.quote(q)}"
            console.print(f"[cyan]🔍 [Amazon RAM] ({idx}/{total_queries})[/cyan] {q}...")
            query_batch = []

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=25000)
                page.wait_for_timeout(1000)

                items = page.locator('div[data-component-type="s-search-result"]')
                count = items.count()

                for i in range(count):
                    item = items.nth(i)
                    asin = item.get_attribute("data-asin")
                    if not asin or asin in seen_asins or asin in ignored:
                        continue

                    title_el = item.locator("h2")
                    if title_el.count() == 0:
                        continue
                    title = title_el.first.inner_text().strip().replace("\n", " ")

                    specs = parse_ram_specs(title)
                    if not specs:
                        continue

                    price_el = item.locator(".a-price .a-offscreen")
                    if price_el.count() == 0:
                        continue
                    price = parse_price(price_el.first.inner_text())
                    if not price or price < 30.0:
                        continue

                    cap_gb = specs["capacity_gb"]
                    aed_per_gb = round(price / cap_gb, 2)
                    if aed_per_gb < 1.5:
                        continue

                    is_renewed = any(
                        k in title.lower()
                        for k in ["renewed", "refurbished", "used -", "recertified"]
                    )

                    prod_url = f"https://www.amazon.ae/dp/{asin}"

                    ram_dict = {
                        "sku": asin,
                        "title": title,
                        "generation": specs["generation"],
                        "form_factor": specs["form_factor"],
                        "capacity_gb": cap_gb,
                        "speed_mhz": specs["speed_mhz"],
                        "kit_config": specs["kit_config"],
                        "cas_latency": specs["cas_latency"],
                        "price_aed": price,
                        "aed_per_gb": aed_per_gb,
                        "store": "Amazon.ae",
                        "model_number": specs["model_number"],
                        "is_renewed": is_renewed,
                        "url": prod_url
                    }

                    seen_asins.add(asin)
                    results.append(ram_dict)
                    query_batch.append(ram_dict)

                if batch_callback and query_batch:
                    batch_callback(query_batch)

            except Exception as e:
                console.print(f"[red]Error querying {q}: {e}[/red]")

        browser.close()

    logger.info(f"Amazon.ae RAM scrape finished: found {len(results)} kits.")
    return results


# ── Combined Pipeline Runner ────────────────────────────────

def scrape_all_ram(
    progress_callback = None,
    batch_callback = None
) -> list[dict]:
    """Runs all 3 RAM scrapers (Al Ershad, Microless, Amazon.ae) sequentially."""
    logger.info("Starting unified RAM scrape across Amazon.ae, Microless, and Al Ershad...")
    total_found = []

    def on_batch(batch):
        try:
            db.store_ram_scrape_results(batch)
        except Exception as err:
            logger.warning(f"Error storing RAM batch: {err}")
        if batch_callback:
            batch_callback(batch)

    # 1. Al Ershad
    ershad_kits = scrape_ershad_ram(
        progress_callback=progress_callback,
        batch_callback=on_batch
    )
    total_found.extend(ershad_kits)

    # 2. Microless
    microless_kits = scrape_microless_ram(
        progress_callback=progress_callback,
        batch_callback=on_batch
    )
    total_found.extend(microless_kits)

    # 3. Amazon.ae
    amazon_kits = scrape_amazon_ram(
        progress_callback=progress_callback,
        batch_callback=on_batch
    )
    total_found.extend(amazon_kits)

    logger.info(f"Unified RAM scrape completed: {len(total_found)} total RAM kits ingested.")
    return total_found


if __name__ == "__main__":
    db.init_db()
    results = scrape_all_ram()
    print(f"Total RAM scraped: {len(results)}")
