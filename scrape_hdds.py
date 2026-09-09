#!/usr/bin/env python3
"""
Amazon.ae HDD Live Scraper & Deal Finder
Calculates the best AED/TB value on real hard drives 10TB and larger.
"""

import re
import json
import argparse
from playwright.sync_api import sync_playwright
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

console = Console()

EXCLUDE_KEYWORDS = [
    "enclosure", "docking", "dock", "cloner", "duplicator", "diskless",
    "caddy", "case", "adapter", "housing", "cable", "portable external hard drive",
    "mini portable", "aluminum portable", "nvme", "m.2"
]

KNOWN_HDD_BRANDS = [
    "seagate", "western digital", "wd", "toshiba", "hgst", "hitachi",
    "maxdigitaldata", "mdd", "synology", "ironwolf", "exos", "barracuda",
    "ultrastar", "my book", "elements", "expansion", "one touch", "n300"
]

def parse_capacity(title: str):
    # Matches patterns like 10TB, 12 TB, 14 Terabytes, 16-TB, 20TB, etc.
    # Exclude matches preceded by "up to"
    if re.search(r'up to\s+\d+\s*tb', title, re.IGNORECASE):
        # Could be an enclosure claiming "up to 20TB"
        title = re.sub(r'up to\s+\d+\s*tb', '', title, flags=re.IGNORECASE)

    match = re.search(r'\b(1[0-9]|2[0-9]|3[0-2])\s*[-–]?\s*(?:TB|tb|Terabyte|terabytes|Tera)\b', title)
    if match:
        return int(match.group(1))
    return None

def parse_price(price_str: str):
    if not price_str:
        return None
    clean = re.sub(r'[^\d.]', '', price_str.replace(',', ''))
    try:
        val = float(clean)
        return val
    except ValueError:
        return None

def is_real_hdd(title: str) -> bool:
    t = title.lower()
    # Check exclusions (enclosures, fake portable flash drives, docks)
    for kw in EXCLUDE_KEYWORDS:
        if kw in t:
            return False
            
    # Must match at least one known reliable storage keyword or brand
    brand_match = any(b in t for b in KNOWN_HDD_BRANDS)
    form_factor_match = any(term in t for term in ["3.5", "7200rpm", "5400rpm", "sata", "sas", "enterprise", "nas"])
    return brand_match or form_factor_match

def scrape_amazon_hdds(queries, min_capacity=10, max_capacity=30, include_renewed=True):
    results = []
    seen_asins = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-setuid-sandbox',
            ]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 900},
            locale="en-AE"
        )
        page = context.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        for q in queries:
            url = f"https://www.amazon.ae/s?k={q.replace(' ', '+')}&s=price-asc-rank"
            console.print(f"[cyan]🔍 Searching Amazon.ae for:[/cyan] [bold]{q}[/bold]...")
            
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=25000)
                page.wait_for_timeout(1200)

                items = page.locator('div[data-component-type="s-search-result"]')
                count = items.count()

                for i in range(count):
                    item = items.nth(i)
                    asin = item.get_attribute("data-asin")
                    if not asin or asin in seen_asins:
                        continue

                    title_elem = item.locator("h2")
                    if title_elem.count() == 0:
                        continue
                    title = title_elem.first.inner_text().strip().replace('\n', ' ')

                    # Validate that it's a real drive and not an accessory
                    if not is_real_hdd(title):
                        continue

                    capacity = parse_capacity(title)
                    if not capacity or capacity < min_capacity or capacity > max_capacity:
                        continue

                    price_elem = item.locator(".a-price .a-offscreen")
                    if price_elem.count() == 0:
                        continue
                    price_text = price_elem.first.inner_text()
                    price = parse_price(price_text)
                    if not price or price < 400:  # Real 10TB+ drives will not be under 400 AED
                        continue

                    is_renewed = any(k in title.lower() for k in ["renewed", "refurbished", "used -", "recertified"])
                    if not include_renewed and is_renewed:
                        continue

                    has_prime = item.locator(".a-icon-prime").count() > 0
                    full_url = f"https://www.amazon.ae/dp/{asin}"
                    aed_per_tb = price / capacity
                    seen_asins.add(asin)

                    # Classify Drive Type
                    t_lower = title.lower()
                    if "exos" in t_lower or "ultrastar" in t_lower or "mg0" in t_lower or "enterprise" in t_lower or "he10" in t_lower or "he12" in t_lower:
                        category = "Enterprise"
                    elif "ironwolf" in t_lower or "wd red" in t_lower or "n300" in t_lower or "nas" in t_lower:
                        category = "NAS"
                    elif "elements" in t_lower or "my book" in t_lower or "expansion" in t_lower or "one touch" in t_lower or "external" in t_lower:
                        category = "External"
                    else:
                        category = "Internal HDD"

                    results.append({
                        "asin": asin,
                        "title": title,
                        "capacity": capacity,
                        "price": round(price, 2),
                        "aed_per_tb": round(aed_per_tb, 2),
                        "category": category,
                        "is_renewed": is_renewed,
                        "has_prime": has_prime,
                        "url": full_url
                    })

            except Exception as e:
                console.print(f"[red]Error scraping '{q}':[/red] {e}")

        browser.close()

    # Sort results by best value (lowest AED/TB first)
    results.sort(key=lambda x: x["aed_per_tb"])
    return results

def main():
    parser = argparse.ArgumentParser(description="Live Scrape Amazon.ae for Best HDD Deals (AED/TB)")
    parser.add_argument("--min-tb", type=int, default=10, help="Minimum drive capacity in TB (default: 10)")
    parser.add_argument("--no-renewed", action="store_true", help="Exclude renewed/refurbished drives")
    parser.add_argument("--export", type=str, default="amazon_hdd_deals.json", help="JSON file to export results")
    args = parser.parse_args()

    console.print(Panel.fit(
        "[bold cyan]Amazon.ae Live Hard Drive Deal Hunter[/bold cyan]\n"
        f"[dim]Scanning 10TB+ HDDs, verifying authentic drives & calculating live AED/TB value[/dim]",
        border_style="cyan"
    ))

    search_queries = [
        "10TB internal hard drive",
        "12TB internal hard drive",
        "14TB internal hard drive",
        "16TB internal hard drive",
        "18TB internal hard drive",
        "20TB internal hard drive",
        "Seagate Exos 16TB",
        "Seagate Exos 18TB",
        "Seagate Exos 20TB",
        "WD Elements 14TB",
        "WD Elements 18TB",
        "WD My Book 18TB",
        "WD My Book 14TB",
        "Seagate Expansion 16TB",
        "Seagate Expansion 18TB"
    ]

    deals = scrape_amazon_hdds(
        queries=search_queries,
        min_capacity=args.min_tb,
        include_renewed=not args.no_renewed
    )

    if not deals:
        console.print("[yellow]No matching drives found.[/yellow]")
        return

    with open(args.export, "w", encoding="utf-8") as f:
        json.dump(deals, f, indent=2, ensure_ascii=False)

    console.print(f"\n[green]✓ Successfully filtered and found {len(deals)} genuine 10TB+ hard drives![/green]")
    console.print(f"[dim]Exported to {args.export}[/dim]\n")

    table = Table(
        title="🏆 Live Amazon.ae HDD Deals Ranked by Best AED/TB (Lowest = Best Value)",
        show_header=True,
        header_style="bold magenta",
        show_lines=True
    )
    table.add_column("Rank", justify="right", style="cyan", width=5)
    table.add_column("Category", justify="center", style="bold blue", width=12)
    table.add_column("Size", justify="right", style="green", width=7)
    table.add_column("Price (AED)", justify="right", style="yellow", width=12)
    table.add_column("AED / TB", justify="right", style="bold green", width=11)
    table.add_column("Cond", justify="center", width=8)
    table.add_column("Prime", justify="center", width=6)
    table.add_column("Product Title & Direct Link", style="white")

    for idx, d in enumerate(deals[:25], 1):
        cond = "[yellow]Renewed[/yellow]" if d["is_renewed"] else "[green]New[/green]"
        prime = "[bold cyan]✓[/bold cyan]" if d["has_prime"] else "[dim]-[/dim]"
        title_disp = d["title"][:72] + "..." if len(d["title"]) > 72 else d["title"]
        table.add_row(
            str(idx),
            d["category"],
            f"{d['capacity']}TB",
            f"{d['price']:.2f}",
            f"{d['aed_per_tb']:.2f}",
            cond,
            prime,
            f"{title_disp}\n[dim underline blue]{d['url']}[/dim underline blue]"
        )

    console.print(table)

if __name__ == "__main__":
    main()
