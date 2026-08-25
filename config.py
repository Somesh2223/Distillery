"""Central configuration loaded from .env. Never hardcode API keys elsewhere."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data")).resolve()
FETCHED_DIR = DATA_DIR / "fetched"
DATASETS_DIR = DATA_DIR / "datasets"
DB_PATH = DATA_DIR / "index.db"

for d in (DATA_DIR, FETCHED_DIR, DATASETS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# --- LLM (query parsing) ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")

# --- Image API connectors ---
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "")
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "")
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY", "")

# --- Text/article API connectors ---
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "")
REDDIT_CLIENT_ID = os.getenv("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET", "")
REDDIT_USER_AGENT = os.getenv("REDDIT_USER_AGENT", "DataFetcher/0.1")

# --- Web search (used by the scraper fallback to discover candidate URLs
# across the whole web; without it, the scraper only works against domains
# the user explicitly allowlists) ---
GOOGLE_CSE_API_KEY = os.getenv("GOOGLE_CSE_API_KEY", "")
GOOGLE_CSE_CX = os.getenv("GOOGLE_CSE_CX", "")

# --- Scraper behavior ---
SCRAPER_USER_AGENT = os.getenv(
    "SCRAPER_USER_AGENT",
    "DataFetcherBot/0.1 (+https://github.com/Somesh2223/DataFetcher; contact: set SCRAPER_USER_AGENT in .env)",
)
SCRAPER_MAX_PAGES_PER_DOMAIN = int(os.getenv("SCRAPER_MAX_PAGES_PER_DOMAIN", "5"))
SCRAPER_MIN_DELAY_SECONDS = float(os.getenv("SCRAPER_MIN_DELAY_SECONDS", "1.0"))
SCRAPER_RENDER_JS = os.getenv("SCRAPER_RENDER_JS", "false").lower() == "true"
SCRAPER_MAX_RETRIES = int(os.getenv("SCRAPER_MAX_RETRIES", "3"))

# --- Dedup thresholds ---
IMAGE_PHASH_HAMMING_THRESHOLD = int(os.getenv("IMAGE_PHASH_HAMMING_THRESHOLD", "6"))
TEXT_JACCARD_THRESHOLD = float(os.getenv("TEXT_JACCARD_THRESHOLD", "0.8"))

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))
