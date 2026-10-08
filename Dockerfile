# DealBot 24/7 - fully automatic Telegram deal finder
FROM python:3.12-slim

WORKDIR /app

# deps first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# app code
COPY bot.py database.py matcher.py scorer.py formatter.py scraper_base.py telegram_client.py config.json ./
COPY scrapers/ ./scrapers/

# persistent state (pre-registered chats + price history)
RUN mkdir -p data logs
COPY data/deals.db* data/

# PORT env var (optional) opens a health endpoint; bot runs as the main process
ENV PYTHONUNBUFFERED=1
CMD ["python", "bot.py"]
