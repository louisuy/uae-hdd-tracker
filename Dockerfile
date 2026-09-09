FROM mcr.microsoft.com/playwright/python:v1.50.0-noble

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Ensure Chromium browser binary is present
RUN playwright install chromium

# Copy app code
COPY . .

# Directory for persistent SQLite storage
RUN mkdir -p /app/data
ENV HDD_TRACKER_DB=/app/data/hdd_prices.db
ENV PORT=5055

EXPOSE 5055

# Run with dynamic PORT support for cloud hosts (Railway, Render, Fly.io, etc.)
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-5055}"]
