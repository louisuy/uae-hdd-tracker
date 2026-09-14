"""
FastAPI application — HDD Price Tracker Dashboard & API.
"""
import asyncio
import threading
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import csv
import io
import os

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from apscheduler.schedulers.background import BackgroundScheduler

import db
import alerts

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hdd-tracker")


live_scrape_state = {
    "is_running": False,
    "current_query": "",
    "current_index": 0,
    "total_queries": 0,
    "drives_found": 0,
    "pct": 0,
    "started_at": None,
    "finished_at": None,
    "error": None
}


def on_scrape_progress(current_idx, total_queries, query, drives_found):
    live_scrape_state["current_index"] = current_idx
    live_scrape_state["total_queries"] = total_queries
    live_scrape_state["current_query"] = query
    live_scrape_state["drives_found"] = drives_found
    live_scrape_state["pct"] = int((current_idx / total_queries) * 100) if total_queries else 0


def run_scrape_job():
    """Run the scraper across all supported UAE retailers (Amazon.ae, Microless)."""
    from scraper import scrape_amazon_hdds
    from microless_scraper import scrape_microless_hdds
    logger.info("Multi-retailer scrape starting...")
    live_scrape_state["is_running"] = True
    live_scrape_state["pct"] = 0
    live_scrape_state["current_index"] = 0
    live_scrape_state["drives_found"] = 0
    live_scrape_state["error"] = None
    live_scrape_state["started_at"] = datetime.now(timezone.utc).isoformat()
    live_scrape_state["finished_at"] = None

    total_drives_found = 0

    def on_drives_batch(batch):
        try:
            db.store_scrape_results(batch)
        except Exception as err:
            logger.warning(f"Error storing batch: {err}")

    scrape_id = db.log_scrape_start()
    try:
        settings = db.get_all_settings()
        min_tb = int(settings.get("min_capacity_tb", "1"))

        # 1. Amazon.ae Scraper
        def on_amazon_progress(cur, total, q, found):
            live_scrape_state["current_index"] = cur
            live_scrape_state["total_queries"] = total + 6
            live_scrape_state["current_query"] = f"[Amazon.ae] {q}"
            live_scrape_state["drives_found"] = found
            live_scrape_state["pct"] = int((cur / (total + 6)) * 100)

        amazon_drives = scrape_amazon_hdds(
            min_capacity=min_tb,
            progress_callback=on_amazon_progress,
            batch_callback=on_drives_batch
        )
        total_drives_found += len(amazon_drives)

        # 2. Microless Scraper
        def on_microless_progress(cur, total, q, found):
            live_scrape_state["current_index"] = 82 + cur
            live_scrape_state["total_queries"] = 82 + total + 10
            live_scrape_state["current_query"] = f"[{q}]"
            live_scrape_state["drives_found"] = total_drives_found + found
            live_scrape_state["pct"] = int(((82 + cur) / (82 + total + 10)) * 100)

        microless_drives = scrape_microless_hdds(
            min_capacity=min_tb,
            progress_callback=on_microless_progress,
            batch_callback=on_drives_batch
        )
        total_drives_found += len(microless_drives)

        # 3. Al Ershad Scraper
        from ershad_scraper import scrape_ershad_hdds

        def on_ershad_progress(cur, total, q, found):
            live_scrape_state["current_index"] = 88 + cur
            live_scrape_state["total_queries"] = 88 + total
            live_scrape_state["current_query"] = f"[{q}]"
            live_scrape_state["drives_found"] = total_drives_found + found
            live_scrape_state["pct"] = int(((88 + cur) / (88 + total)) * 100)

        ershad_drives = scrape_ershad_hdds(
            min_capacity=min_tb,
            progress_callback=on_ershad_progress,
            batch_callback=on_drives_batch
        )
        total_drives_found += len(ershad_drives)

        db.log_scrape_end(scrape_id, total_drives_found, "success")
        live_scrape_state["pct"] = 100
        live_scrape_state["drives_found"] = total_drives_found
        logger.info(f"Multi-retailer scrape complete: {total_drives_found} drives found ({len(amazon_drives)} Amazon.ae, {len(microless_drives)} Microless, {len(ershad_drives)} Al Ershad)")
        # Fire alerts
        asyncio.run(alerts.check_and_alert())
    except Exception as e:
        logger.error(f"Scrape failed: {e}")
        db.log_scrape_end(scrape_id, total_drives_found, "error", str(e))
        live_scrape_state["error"] = str(e)
    finally:
        live_scrape_state["is_running"] = False
        live_scrape_state["finished_at"] = datetime.now(timezone.utc).isoformat()


scheduler = BackgroundScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    interval = int(db.get_setting("scrape_interval_hours") or "6")
    scheduler.add_job(run_scrape_job, "interval", hours=interval, id="scrape_job")
    scheduler.start()
    logger.info(f"Scheduler started — scraping every {interval}h")
    yield
    scheduler.shutdown()


app = FastAPI(title="HDD Price Tracker", lifespan=lifespan)
templates = Jinja2Templates(directory="templates")


# ── Dashboard ────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, new_only: bool = False):
    deals = db.get_latest_deals(new_only=new_only)
    last_scrape = db.get_last_scrape()
    settings = db.get_all_settings()
    stats = db.get_stats()
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "deals": deals,
            "last_scrape": last_scrape,
            "settings": settings,
            "stats": stats,
            "new_only": new_only,
        }
    )


# ── API Endpoints ────────────────────────────────────────────

@app.get("/api/deals")
async def api_deals(new_only: bool = False, min_tb: int = 1, store: str | None = None):
    return db.get_latest_deals(new_only=new_only, min_tb=min_tb, store=store)


@app.get("/api/export/csv")
async def api_export_csv(new_only: bool = False, min_tb: int = 1, store: str | None = None):
    deals = db.get_latest_deals(new_only=new_only, min_tb=min_tb, limit=5000, store=store)
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Rank",
        "Store",
        "Model Number",
        "ASIN / SKU",
        "Title",
        "Capacity (TB)",
        "Category",
        "Recording Tech",
        "Price (AED)",
        "AED / TB",
        "All-Time Low Price (AED)",
        "All-Time Low AED/TB",
        "Condition",
        "Shuckable",
        "Is Record Low",
        "Product URL"
    ])
    
    for idx, d in enumerate(deals, 1):
        writer.writerow([
            idx,
            d.get("store", "Amazon.ae"),
            d.get("model_number", ""),
            d.get("asin", ""),
            d.get("title", ""),
            d.get("capacity_tb", ""),
            d.get("category", ""),
            d.get("recording_tech", ""),
            f"{d.get('price_aed', 0):.2f}",
            f"{d.get('aed_per_tb', 0):.2f}",
            f"{d.get('min_price', d.get('price_aed', 0)):.2f}",
            f"{d.get('min_aed_per_tb', d.get('aed_per_tb', 0)):.2f}",
            "Renewed" if d.get("is_renewed") else "New",
            "Yes" if d.get("is_shuckable") else "No",
            "Yes" if d.get("is_all_time_low") else "No",
            d.get("url", "")
        ])
    
    filename = f"uae_hdd_deals_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        }
    )


@app.get("/api/stats")
async def api_stats():
    return db.get_stats()


@app.get("/api/history/{asin}")
async def api_history(asin: str):
    return db.get_price_history(asin)


@app.get("/api/status")
async def api_status():
    last = db.get_last_scrape()
    jobs = scheduler.get_jobs()
    next_run = str(jobs[0].next_run_time) if jobs else None
    return {
        "last_scrape": last,
        "next_scrape": next_run,
        "live_scrape": live_scrape_state
    }


@app.post("/api/test-alert")
async def api_test_alert(request: Request):
    data = await request.json()
    token = data.get("telegram_bot_token")
    chat_id = data.get("telegram_chat_id")
    if not token or not chat_id:
        return JSONResponse({"status": "error", "message": "Missing token or chat_id"}, status_code=400)
    try:
        await alerts.send_telegram_alert(
            token, chat_id,
            "🔔 <b>HDD Price Tracker Test Alert!</b>\n\nYour Telegram alerts are successfully configured. You will receive notifications when HDDs hit your AED/TB target!"
        )
        return {"status": "ok", "message": "Test alert sent successfully!"}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/scrape")
async def api_trigger_scrape():
    """Trigger an immediate scrape in a background thread."""
    thread = threading.Thread(target=run_scrape_job, daemon=True)
    thread.start()
    return {"status": "started", "message": "Scrape running in background"}


@app.post("/api/settings")
async def api_update_settings(request: Request):
    data = await request.json()
    for key, value in data.items():
        db.set_setting(key, str(value))
    # Reschedule if interval changed
    if "scrape_interval_hours" in data:
        interval = int(data["scrape_interval_hours"])
        scheduler.reschedule_job("scrape_job", trigger="interval", hours=interval)
        logger.info(f"Rescheduled scraper to every {interval}h")
    return {"status": "ok"}


@app.get("/api/settings")
async def api_get_settings():
    return db.get_all_settings()


@app.post("/api/hide/{asin}")
async def api_hide_listing(asin: str):
    db.ignore_asin(asin, reason="user_hidden")
    return {"status": "ok", "asin": asin}


@app.get("/api/hidden")
async def api_get_hidden():
    return db.get_ignored_list()


@app.post("/api/unhide/{asin}")
async def api_unhide_listing(asin: str):
    db.unignore_asin(asin)
    return {"status": "ok", "asin": asin}


# ── PWA ──────────────────────────────────────────────────────

@app.get("/manifest.json")
async def manifest():
    return JSONResponse({
        "name": "HDD Price Tracker",
        "short_name": "HDD Tracker",
        "description": "Amazon.ae HDD price monitor — find the best AED/TB deals",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0f172a",
        "theme_color": "#0ea5e9",
        "icons": [
            {"src": "/icon-192.svg", "sizes": "192x192", "type": "image/svg+xml"},
            {"src": "/icon-512.svg", "sizes": "512x512", "type": "image/svg+xml"},
        ]
    })


@app.get("/sw.js")
async def service_worker():
    return HTMLResponse(
        content="""
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => e.respondWith(fetch(e.request)));
""",
        media_type="application/javascript"
    )


@app.get("/favicon.ico")
@app.get("/icon-192.svg")
@app.get("/icon-512.svg")
async def icon():
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
    <rect width="100" height="100" rx="20" fill="#0ea5e9"/>
    <text x="50" y="62" font-size="48" text-anchor="middle" fill="white" font-family="sans-serif" font-weight="bold">💾</text>
    </svg>'''
    return HTMLResponse(content=svg, media_type="image/svg+xml")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 5055))
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=port,
        reload=True,
        reload_includes=["*.py", "*.html"],
        reload_excludes=["*.db*", "*.sqlite*", "*.csv", "*.log", "__pycache__", ".git/*"]
    )
