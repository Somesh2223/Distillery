# DataFetcher

A local web app that fetches data from the web — images, articles, or
structured tables/listings — based on a plain-English condition, and can
optionally package the results into a labeled dataset for training/testing an
ML model.

You type something like:

> 50 high-resolution photos of red sports cars from the side, no watermarks

...and DataFetcher parses that into a structured query, routes it to the
right API (or a robots.txt-respecting fallback scraper if no API fits or the
API comes up short), deduplicates against everything it's fetched before, and
either shows you a preview grid or exports a train/val/test-split dataset zip
with full manifests.

## How it works

1. **Query parsing** (`query_parser.py`) — turns your condition into a strict
   JSON structure: `data_type`, `keywords`, `count`, `filters` (resolution,
   orientation, date range, domain allowlist, language, watermark),
   `output_mode`, and a `label` for the dataset folder name. Tried in order:
   an Anthropic tool-use call, then a Google Gemini structured-output call
   (free tier, no credit card — see the key table below), then a heuristic
   word-filter parser if neither LLM key is set. The heuristic parser handles
   simple/common phrasings fine but can misfire on more conversational
   sentences ("find me pictures that I can give my model to check if...") —
   set either LLM key for reliably accurate parsing of arbitrary phrasing.
2. **Source routing** (`source_router.py`) — tries configured API connectors
   for the data type first (in order), and falls back to the generic scraper
   (`scraper.py`) if none are configured or they return fewer results than
   requested. The scraper discovers candidate URLs either via Google's
   Custom Search JSON API (if configured — an official, ToS-compliant search
   API) or by crawling domains you explicitly list in `domain_allowlist`; it
   deliberately does not scrape a search engine's results page directly,
   since engines like Google/DuckDuckGo actively fingerprint and block that
   kind of automated access, and defeating it would mean bypassing bot
   detection.
3. **Dedup** (`dedup.py`) — perceptual hashing (`imagehash`) for images,
   shingle/MinHash similarity for text — checked against both the current
   batch and everything previously indexed in SQLite.
4. **Storage** (`storage.py`) — every item's source URL, source name, fetch
   timestamp, license/attribution (when the API provides it), and originating
   query is recorded in SQLite (`data/index.db`).
5. **Dataset export** (`dataset_export.py`) — on request, builds a
   folder-per-split/per-label layout, a COCO-style `annotations.json` for
   images, `metadata.csv` + `metadata.json` manifests, optional image
   resizing, and zips it all up for download.

## Project structure

```
DataFetcher/
├── app.py                 # FastAPI app + endpoints
├── config.py               # loads .env, paths, thresholds
├── models.py               # StructuredQuery / filters / export options
├── query_parser.py         # NL -> StructuredQuery (LLM + heuristic fallback)
├── source_router.py        # API-vs-scraper routing, materialize + dedup + persist
├── scraper.py               # generic fallback scraper (robots.txt, rate limit, retry)
├── dedup.py                 # perceptual hash / text shingle dedup
├── dataset_export.py        # folder layout, split, manifests, zip
├── storage.py                # SQLite schema + helpers
├── logging_setup.py          # structured JSON logging
├── connectors/
│   ├── base.py               # Item dataclass + BaseConnector interface
│   ├── unsplash.py            # images
│   ├── pexels.py               # images
│   ├── pixabay.py               # images
│   ├── newsapi.py                # articles
│   ├── reddit.py                  # text (OAuth2 app-only)
│   ├── hackernews.py               # text (no key needed)
│   └── wikipedia.py                 # text summaries + structured tables (no key needed)
├── static/
│   ├── index.html                    # single-page UI
│   └── app.js                         # UI logic
├── data/                                # created at runtime: fetched files, SQLite index, dataset zips
├── .env.example
├── requirements.txt
└── README.md
```

## Setup

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env         # then fill in whichever keys you have
uvicorn app:app --reload
```

Open http://127.0.0.1:8000 in your browser.

Every API integration is optional. DataFetcher checks each connector's
`is_configured()` and skips it (falling through to the next connector, then
to the scraper) if its keys are missing — so it runs out of the box with
zero keys, using Wikipedia + Hacker News (no key required) and the scraper
fallback, and a heuristic query parser instead of the LLM.

### API keys — where to get free-tier ones

| Service | Used for | Free tier | Get a key |
|---|---|---|---|
| Anthropic | parsing your condition into a structured query | pay-as-you-go, has a free credit for new accounts | https://console.anthropic.com/ |
| Google Gemini | parsing your condition into a structured query (free alternative to Anthropic) | free tier, no credit card required | https://aistudio.google.com/apikey |
| Unsplash | image search | 50 req/hour (demo apps) | https://unsplash.com/developers |
| Pexels | image search | 200 req/hour | https://www.pexels.com/api/ |
| Pixabay | image search | 5,000 req/hour | https://pixabay.com/api/docs/ |
| NewsAPI | news articles | 100 req/day, articles from the last month | https://newsapi.org/register |
| Reddit | text/discussion posts (OAuth2 client-credentials, no user login) | generous free tier | create a "script" app at https://www.reddit.com/prefs/apps |
| Hacker News (Algolia) | text/discussion posts | free, no key | n/a |
| Wikipedia | text summaries + structured tables | free, no key | n/a |
| Google Custom Search JSON API | scraper fallback's web-wide URL/image discovery | 100 queries/day free | create a search engine at https://programmablesearchengine.google.com/ (enable "Search the entire web"), then get an API key at https://console.cloud.google.com/apis/credentials (enable "Custom Search API") |

## Non-functional behavior

- **robots.txt is always respected.** The scraper checks `robots.txt` for
  every domain it visits before fetching anything and logs the decision
  (`storage.scrape_log`). A domain's `robots.txt` disallowing a path means
  that path is skipped — full stop. The `domain_allowlist` filter only
  *narrows* which domains the scraper is allowed to try; it never bypasses
  robots.txt.
- **Rate limiting + backoff.** Requests to a given domain are spaced out
  (`SCRAPER_MIN_DELAY_SECONDS`), retried with exponential backoff on 429/5xx
  (`SCRAPER_MAX_RETRIES`), and capped at `SCRAPER_MAX_PAGES_PER_DOMAIN` pages
  per domain per run.
- **License/attribution tracking.** Every item stores whatever license and
  attribution info its source provides (e.g. "Pexels License — no attribution
  required" vs "Photo by X on Unsplash"); scraped items are marked "Unknown —
  verify the site's terms before reuse" since scraped pages don't self-report
  licensing. Check this column in `metadata.csv` before using anything for
  training.
- **Structured logging.** Every fetch decision (which connector was tried,
  why, how many results it returned, why the router fell back to the
  scraper, which domains were skipped for robots.txt) is logged as JSON lines
  to stdout.
- **No hardcoded keys.** Everything comes from `.env` via `python-dotenv`.
- **Cancellable fetches.** A "Stop fetch" button appears while a run is in
  progress (`POST /api/runs/{run_id}/cancel`). Cancellation is checked
  between pages/requests inside every connector and the scraper — not just
  between whole connectors — so it takes effect within roughly one
  page-fetch, and whatever was already fetched is kept and shown (run status
  becomes `cancelled` rather than `completed`).

## Example queries

**Image:**
> 30 high-resolution photos of golden retriever puppies, landscape orientation, no watermarks

Router tries Unsplash → Pexels → Pixabay in order (whichever have keys
configured), applies the `orientation=landscape` filter each API supports,
dedups by perceptual hash, and (since this didn't mention "dataset") returns
a preview grid of 30 thumbnails with attribution badges.

**Text:**
> 200 short news articles about renewable energy from the last year, build a training dataset

Parses to `data_type: text`, `output_mode: dataset`, a 1-year `date_range`.
Tries NewsAPI first (needs a key; free tier only covers the last month, so
count may come up short), then Wikipedia and Hacker News fill the rest, then
the scraper covers any remainder. On export you get
`text/{train,val,test}/renewable_energy/*.txt` plus `metadata.csv/json`.

**Structured:**
> a table of the top 100 companies by market cap

Parses to `data_type: structured`. `WikipediaTableConnector` searches
Wikipedia for the best-matching list article, extracts the largest table on
the page via `pandas.read_html`, and returns up to 100 rows as items (each
row's JSON stored in `local_path`, later merged into `metadata.csv/json` on
export). No dataset split is very meaningful for a single ranked table, but
the same 80/10/10 mechanism still applies if you choose dataset mode.

## Notes / limitations

- Without `GOOGLE_CSE_API_KEY`/`GOOGLE_CSE_CX` and without a `domain_allowlist`
  on the query, the scraper fallback has no way to discover URLs and
  contributes nothing — API connectors (and the heuristic/LLM parser's
  chosen data type) are doing all the work in that case. This is intentional:
  we do not scrape a general search engine's results page to work around
  that, since it requires defeating bot detection.
- JS-rendered pages are skipped by the fallback scraper unless you set
  `SCRAPER_RENDER_JS=true` and `pip install playwright && playwright install
  chromium` — this is optional and off by default to keep the base install
  light.
- SQLite dedup index and fetched files persist in `data/` across runs (not
  committed to git) — delete that folder to reset the index.
