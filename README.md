# 💽 UAE HDD Price Tracker & NAS Deals Dashboard

An automated, multi-retailer real-time HDD price tracker and deal discovery dashboard built for home NAS builders, homelabs, and storage enthusiasts in the UAE (Amazon.ae, Microless, and Al Ershad).

Built specifically to help find the best **AED/TB** deals, filter for **CMR drives**, compute **4-bay RAID 5 total build costs**, and track price drops over time.

---

## ✨ Features

- **Multi-Retailer Ingestion**:
  - 📦 **Amazon.ae**: Headless Playwright crawler with pagination, bot-avoidance, and ASIN tracking.
  - 🔴 **Microless UAE**: Category crawler with automatic Manufacturer Part Number (MPN) parsing.
  - 🟣 **Al Ershad Infotech**: Bur Dubai / Computer Plaza retailer API integration via high-speed Shopify catalog search.
- **Smart Hardware Classification**:
  - **CMR vs. SMR Detection**: Automatically identifies recording technology to prevent unsafe SMR drives in RAID configurations.
  - **Drive Type Classification**: Categorizes into NAS (IronWolf, WD Red Plus/Pro), Enterprise (Exos, Ultrastar, MG Series), Surveillance (SkyHawk, Purple), Desktop (Barracuda, Blue), and External / Shuckable (Elements, Expansion).
  - **Anti-Pollution / Price Floors**: Automatically filters out enclosures, cables, dummy listings, and SSD accessories.
- **4-Bay RAID 5 Calculator**:
  - Calculates 4-drive pack cost and usable capacity in real-time.
- **Interactive Dashboard**:
  - Live filtering by Retailer, Condition (New vs. Renewed), Capacity (1TB to 32TB), and Recording Technology (CMR only).
  - Search by Drive Model, MPN, or Title.
  - Full and filtered view CSV export.
  - Price history modal with Chart.js visualization and all-time low badges.
- **Automated Alerts & Scheduling**:
  - Background periodic scraper (configurable interval).
  - Instant Telegram alerts when a drive hits an all-time low or drops below AED/TB thresholds.
- **Docker Ready**:
  - Production-ready Dockerfile and `docker-compose.yml` supporting persistent volume mounts for SQLite.
  - Ready for Railway, VPS, or local NAS deployment (Ugreen UGOS Pro, Synology DSM, TrueNAS).

---

## 🚀 Quick Start (Local)

### 1. Prerequisites
- Python 3.10+
- Playwright browsers installed

### 2. Installation
```bash
git clone https://github.com/louisuy/uae-hdd-tracker.git
cd uae-hdd-tracker

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### 3. Run the Dashboard
```bash
python3 app.py
```
Open your browser at [http://localhost:5055](http://localhost:5055).

---

## 🐳 Docker Deployment

To run in Docker with persistent storage:

```bash
docker compose up -d --build
```
The database will be persisted in `./data/hdd_prices.db`.

---

## ⚙️ Configuration & Alerts

Telegram bot credentials and scraping settings can be configured directly inside the Dashboard Settings tab or stored in SQLite.

---

## 🛠️ Tech Stack

- **Backend**: FastAPI, SQLite, APScheduler, Playwright
- **Frontend**: Jinja2, Tailwind CSS, Chart.js
- **Containerization**: Docker (Noble-based Playwright image)
