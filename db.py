"""
SQLite database layer for HDD price tracking with fluctuation analysis,
CMR/SMR detection, and 4-bay RAID 5 calculations.
"""
import sqlite3
import os
from datetime import datetime, timezone

DB_PATH = os.environ.get("HDD_TRACKER_DB", "hdd_prices.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS drives (
            asin TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            capacity_tb INTEGER NOT NULL,
            category TEXT NOT NULL DEFAULT 'Unknown',
            recording_tech TEXT NOT NULL DEFAULT 'CMR ✅',
            is_renewed INTEGER NOT NULL DEFAULT 0,
            url TEXT NOT NULL DEFAULT '',
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS price_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asin TEXT NOT NULL REFERENCES drives(asin),
            price_aed REAL NOT NULL,
            aed_per_tb REAL NOT NULL,
            scraped_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_snapshots_asin ON price_snapshots(asin);
        CREATE INDEX IF NOT EXISTS idx_snapshots_time ON price_snapshots(scraped_at);

        CREATE TABLE IF NOT EXISTS scrape_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            drives_found INTEGER DEFAULT 0,
            status TEXT DEFAULT 'running',
            error TEXT
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ignored_asins (
            asin TEXT PRIMARY KEY,
            reason TEXT DEFAULT 'user_hidden',
            ignored_at TEXT NOT NULL
        );
    """)

    # Migration: add recording_tech if table already existed without it
    try:
        conn.execute("ALTER TABLE drives ADD COLUMN recording_tech TEXT DEFAULT 'CMR ✅'")
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Migration: add store and model_number for multi-retailer support
    try:
        conn.execute("ALTER TABLE drives ADD COLUMN store TEXT DEFAULT 'Amazon.ae'")
    except sqlite3.OperationalError:
        pass

    try:
        conn.execute("ALTER TABLE drives ADD COLUMN model_number TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass

    try:
        conn.execute("UPDATE drives SET store = 'Amazon.ae' WHERE store IS NULL OR store = ''")
    except sqlite3.OperationalError:
        pass

    # Seed default settings
    defaults = {
        "scrape_interval_hours": "6",
        "min_capacity_tb": "4",
        "alert_aed_per_tb_threshold": "100",
        "telegram_bot_token": "",
        "telegram_chat_id": "",
        "alerts_enabled": "false"
    }
    for k, v in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v)
        )
    conn.commit()
    conn.close()


def get_setting(key: str) -> str:
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else ""


def set_setting(key: str, value: str):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value)
    )
    conn.commit()
    conn.close()


def get_all_settings() -> dict:
    conn = get_conn()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}


def upsert_drive(drive: dict, scraped_at: str):
    conn = get_conn()
    rec_tech = drive.get("recording_tech", "CMR ✅")
    store = drive.get("store", "Amazon.ae")
    model_number = drive.get("model_number", "")
    existing = conn.execute("SELECT asin FROM drives WHERE asin = ?", (drive["asin"],)).fetchone()
    if existing:
        conn.execute("""
            UPDATE drives SET title=?, capacity_tb=?, category=?, recording_tech=?, is_renewed=?, url=?, store=?, model_number=?, last_seen=?
            WHERE asin=?
        """, (drive["title"], drive["capacity"], drive["category"], rec_tech,
              1 if drive["is_renewed"] else 0, drive["url"], store, model_number, scraped_at, drive["asin"]))
    else:
        conn.execute("""
            INSERT INTO drives (asin, title, capacity_tb, category, recording_tech, is_renewed, url, store, model_number, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (drive["asin"], drive["title"], drive["capacity"], drive["category"], rec_tech,
              1 if drive["is_renewed"] else 0, drive["url"], store, model_number, scraped_at, scraped_at))

    conn.execute("""
        INSERT INTO price_snapshots (asin, price_aed, aed_per_tb, scraped_at)
        VALUES (?, ?, ?, ?)
    """, (drive["asin"], drive["price"], drive["aed_per_tb"], scraped_at))
    conn.commit()
    conn.close()


def store_scrape_results(drives: list):
    now = datetime.now(timezone.utc).isoformat()
    for d in drives:
        upsert_drive(d, now)


def log_scrape_start() -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO scrape_log (started_at, status) VALUES (?, 'running')",
        (datetime.now(timezone.utc).isoformat(),)
    )
    scrape_id = cur.lastrowid
    conn.commit()
    conn.close()
    return scrape_id


def log_scrape_end(scrape_id: int, drives_found: int, status: str = "success", error: str = None):
    conn = get_conn()
    conn.execute("""
        UPDATE scrape_log SET finished_at=?, drives_found=?, status=?, error=?
        WHERE id=?
    """, (datetime.now(timezone.utc).isoformat(), drives_found, status, error, scrape_id))
    conn.commit()
    conn.close()


def get_latest_deals(new_only: bool = False, min_tb: int = 1, limit: int = 500, store: str | None = None) -> list:
    conn = get_conn()
    query = """
        SELECT d.asin, d.title, d.capacity_tb, d.category, d.recording_tech, d.is_renewed, d.url,
               COALESCE(d.store, 'Amazon.ae') AS store, COALESCE(d.model_number, '') AS model_number,
               ps.price_aed, ps.aed_per_tb, ps.scraped_at,
               (SELECT MIN(price_aed) FROM price_snapshots WHERE asin = d.asin) AS min_price,
               (SELECT MAX(price_aed) FROM price_snapshots WHERE asin = d.asin) AS max_price,
               (SELECT COUNT(*) FROM price_snapshots WHERE asin = d.asin) AS history_count,
               (SELECT price_aed FROM price_snapshots WHERE asin = d.asin ORDER BY scraped_at DESC LIMIT 1 OFFSET 1) AS prev_price,
               (SELECT price_aed FROM price_snapshots WHERE asin = d.asin AND ABS(price_aed - ps.price_aed) >= 0.5 ORDER BY scraped_at DESC LIMIT 1) AS last_different_price
        FROM drives d
        JOIN price_snapshots ps ON d.asin = ps.asin
        WHERE ps.id = (
            SELECT id FROM price_snapshots WHERE asin = d.asin ORDER BY scraped_at DESC LIMIT 1
        )
        AND d.capacity_tb >= ?
    """
    params = [min_tb]
    if new_only:
        query += " AND d.is_renewed = 0"
    if store and store.lower() != "all":
        query += " AND LOWER(d.store) LIKE ?"
        params.append(f"%{store.lower()}%")
    query += " ORDER BY ps.aed_per_tb ASC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()
    
    results = []
    for r in rows:
        d = dict(r)
        curr = d["price_aed"]
        prev = d["prev_price"]
        last_diff = d["last_different_price"]
        min_p = d["min_price"]
        cap = d["capacity_tb"]
        
        # Price movement:
        # If immediate previous price differs, use it.
        # If price hasn't changed from the immediate previous scrape, but it dropped from a higher
        # price earlier (last_diff > curr), preserve the drop comparison so the user keeps seeing the discount!
        diff = 0.0
        base_price = prev
        if prev is not None and prev > 0:
            imm_diff = curr - prev
            if abs(imm_diff) >= 0.5:
                diff = imm_diff
                base_price = prev
            elif last_diff is not None and (curr - last_diff) <= -0.5:
                # Keep the indication of the price drop comparison
                diff = curr - last_diff
                base_price = last_diff

        if base_price and base_price > 0 and abs(diff) >= 0.5:
            pct = (diff / base_price) * 100
            d["price_diff"] = round(diff, 2)
            d["pct_diff"] = round(pct, 1)
            d["prev_price"] = base_price
        else:
            d["price_diff"] = 0.0
            d["pct_diff"] = 0.0
            
        # All-Time Low check
        d["is_all_time_low"] = (min_p is not None and curr <= (min_p + 0.01))
        
        # Shuckable check for externals
        t_low = d["title"].lower()
        d["is_shuckable"] = (d["category"] == "External" and any(k in t_low for k in ["elements", "my book", "expansion", "one touch"]))
        
        # 4-Bay RAID 5 calculations for NAS users
        d["raid5_usable_tb"] = cap * 3  # 4 drives in RAID 5 = (4-1) * cap
        d["raid5_raw_tb"] = cap * 4
        d["raid5_pack_cost"] = round(curr * 4, 2)
        
        results.append(d)

    return results


def get_price_history(asin: str, limit: int = 100) -> dict:
    conn = get_conn()
    drive = conn.execute("SELECT * FROM drives WHERE asin = ?", (asin,)).fetchone()
    if not drive:
        conn.close()
        return {"drive": None, "history": []}
        
    rows = conn.execute("""
        SELECT price_aed, aed_per_tb, scraped_at
        FROM price_snapshots WHERE asin = ?
        ORDER BY scraped_at ASC LIMIT ?
    """, (asin, limit)).fetchall()
    conn.close()
    
    history = [dict(r) for r in rows]
    prices = [h["price_aed"] for h in history]
    
    d_dict = dict(drive)
    cap = d_dict["capacity_tb"]
    curr = prices[-1] if prices else 0
    
    return {
        "drive": d_dict,
        "raid5": {
            "drives": 4,
            "usable_tb": cap * 3,
            "raw_tb": cap * 4,
            "total_cost": round(curr * 4, 2)
        },
        "history": history,
        "stats": {
            "current": prices[-1] if prices else None,
            "lowest": min(prices) if prices else None,
            "highest": max(prices) if prices else None,
            "average": round(sum(prices) / len(prices), 2) if prices else None,
            "data_points": len(prices)
        }
    }


def get_stats() -> dict:
    conn = get_conn()
    deals = get_latest_deals(limit=1000)
    conn.close()
    
    if not deals:
        return {
            "total_drives": 0,
            "best_deal": None,
            "drives_under_100": 0,
            "all_time_lows": 0
        }
        
    best = deals[0]
    under_100 = sum(1 for d in deals if d["aed_per_tb"] <= 100)
    atls = sum(1 for d in deals if d.get("is_all_time_low"))
    
    return {
        "total_drives": len(deals),
        "best_deal": {
            "title": best["title"][:60],
            "capacity": best["capacity_tb"],
            "aed_per_tb": best["aed_per_tb"],
            "price": best["price_aed"]
        },
        "drives_under_100": under_100,
        "all_time_lows": atls
    }


def get_last_scrape() -> dict | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM scrape_log WHERE status != 'interrupted' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_drives_below_threshold(threshold: float) -> list:
    conn = get_conn()
    rows = conn.execute("""
        SELECT d.asin, d.title, d.capacity_tb, d.category, d.url,
               ps.price_aed, ps.aed_per_tb
        FROM drives d
        JOIN price_snapshots ps ON d.asin = ps.asin
        WHERE ps.id = (
            SELECT id FROM price_snapshots WHERE asin = d.asin ORDER BY scraped_at DESC LIMIT 1
        )
        AND ps.aed_per_tb <= ?
        AND d.is_renewed = 0
        ORDER BY ps.aed_per_tb ASC
    """, (threshold,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def ignore_asin(asin: str, reason: str = "user_hidden"):
    """Blacklist an ASIN, deleting it from active tracking and preventing future scrapes."""
    conn = get_conn()
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO ignored_asins (asin, reason, ignored_at) VALUES (?, ?, ?)",
        (asin, reason, now),
    )
    conn.execute("DELETE FROM price_snapshots WHERE asin = ?", (asin,))
    conn.execute("DELETE FROM drives WHERE asin = ?", (asin,))
    conn.commit()
    conn.close()


def unignore_asin(asin: str):
    """Remove an ASIN from the blacklist so it can be tracked again."""
    conn = get_conn()
    conn.execute("DELETE FROM ignored_asins WHERE asin = ?", (asin,))
    conn.commit()
    conn.close()


def get_ignored_asins() -> set[str]:
    """Returns a set of all ignored/blacklisted ASINs."""
    conn = get_conn()
    rows = conn.execute("SELECT asin FROM ignored_asins").fetchall()
    conn.close()
    return {r["asin"] for r in rows}


def get_ignored_list() -> list[dict]:
    """Returns detailed records of all ignored ASINs."""
    conn = get_conn()
    rows = conn.execute("SELECT * FROM ignored_asins ORDER BY ignored_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

