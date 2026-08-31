"""SQLite schema + helpers for run metadata and the fetch/dedup index.

Uses stdlib sqlite3 with `check_same_thread=False` behind a module-level lock
since the app is single-process and fetch jobs run in a worker thread.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from config import DB_PATH
from connectors.base import Item

_LOCK = threading.RLock()
_CONN: Optional[sqlite3.Connection] = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    condition_text TEXT NOT NULL,
    structured_query TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    requested_count INTEGER NOT NULL,
    fetched_count INTEGER NOT NULL DEFAULT 0,
    output_mode TEXT NOT NULL,
    dataset_path TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- `id` (a hash of the source URL) is scoped to (run_id, id), NOT globally
-- unique on its own — a globally-unique id meant that once a URL was
-- fetched in ANY run, it could never be fetched again in any FUTURE run
-- either, even a totally unrelated one. Since APIs like Pexels return the
-- same top-ranked URLs for the same/similar search every time, that made a
-- repeated or refined query silently return far fewer results each time,
-- with no indication why. Cross-run dedup is now an explicit opt-in
-- (StructuredQuery.filters.dedupe_across_runs) handled in source_router.py.
CREATE TABLE IF NOT EXISTS items (
    id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    data_type TEXT NOT NULL,
    source_name TEXT,
    source_url TEXT,
    local_path TEXT,
    title TEXT,
    text_snippet TEXT,
    width INTEGER,
    height INTEGER,
    phash TEXT,
    text_hash TEXT,
    license TEXT,
    attribution TEXT,
    author TEXT,
    published_at TEXT,
    query_text TEXT,
    fetched_at TEXT,
    extra_json TEXT,
    PRIMARY KEY (run_id, id),
    FOREIGN KEY (run_id) REFERENCES runs(id)
);

CREATE INDEX IF NOT EXISTS idx_items_run_id ON items(run_id);
CREATE INDEX IF NOT EXISTS idx_items_phash ON items(phash);
CREATE INDEX IF NOT EXISTS idx_items_text_hash ON items(text_hash);

CREATE TABLE IF NOT EXISTS scrape_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL,
    url TEXT NOT NULL,
    allowed INTEGER NOT NULL,
    reason TEXT,
    ts TEXT NOT NULL
);
"""


def _migrate_items_pk_if_needed(conn: sqlite3.Connection) -> None:
    """Upgrades a pre-existing `items` table from the old `id TEXT PRIMARY
    KEY` (globally unique) to `PRIMARY KEY (run_id, id)` (unique per run),
    preserving every row. `CREATE TABLE IF NOT EXISTS` in SCHEMA is a no-op
    against an already-existing `items` table regardless of its old shape,
    so this runs separately to actually fix it in place."""
    cur = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='items'")
    row = cur.fetchone()
    if row is None or "PRIMARY KEY (run_id, id)" in row[0]:
        return  # fresh DB (SCHEMA above already created the new shape) or already migrated
    conn.executescript(
        """
        ALTER TABLE items RENAME TO items_pk_migration;
        CREATE TABLE items (
            id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            data_type TEXT NOT NULL,
            source_name TEXT,
            source_url TEXT,
            local_path TEXT,
            title TEXT,
            text_snippet TEXT,
            width INTEGER,
            height INTEGER,
            phash TEXT,
            text_hash TEXT,
            license TEXT,
            attribution TEXT,
            author TEXT,
            published_at TEXT,
            query_text TEXT,
            fetched_at TEXT,
            extra_json TEXT,
            PRIMARY KEY (run_id, id),
            FOREIGN KEY (run_id) REFERENCES runs(id)
        );
        INSERT INTO items SELECT * FROM items_pk_migration;
        DROP TABLE items_pk_migration;
        """
    )


def get_conn() -> sqlite3.Connection:
    global _CONN
    if _CONN is None:
        _CONN = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _CONN.row_factory = sqlite3.Row
        with _LOCK:
            _CONN.executescript(SCHEMA)
            _migrate_items_pk_if_needed(_CONN)
            _CONN.commit()
    return _CONN


@contextmanager
def cursor() -> Iterator[sqlite3.Cursor]:
    conn = get_conn()
    with _LOCK:
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --- runs ---

def create_run(run_id: str, condition_text: str, structured_query: dict, requested_count: int, output_mode: str) -> None:
    with cursor() as cur:
        cur.execute(
            "INSERT INTO runs (id, condition_text, structured_query, status, requested_count, "
            "fetched_count, output_mode, created_at, updated_at) VALUES (?, ?, ?, 'pending', ?, 0, ?, ?, ?)",
            (run_id, condition_text, json.dumps(structured_query), requested_count, output_mode, _now(), _now()),
        )


def update_run_status(run_id: str, status: str, error: Optional[str] = None) -> None:
    with cursor() as cur:
        cur.execute(
            "UPDATE runs SET status = ?, error = ?, updated_at = ? WHERE id = ?",
            (status, error, _now(), run_id),
        )


def set_run_fetched_count(run_id: str, count: int) -> None:
    with cursor() as cur:
        cur.execute(
            "UPDATE runs SET fetched_count = ?, updated_at = ? WHERE id = ?",
            (count, _now(), run_id),
        )


def set_run_requested_count(run_id: str, count: int) -> None:
    """Used by /topup to raise the target when the user asks for replacements
    on top of what a run already fetched."""
    with cursor() as cur:
        cur.execute(
            "UPDATE runs SET requested_count = ?, updated_at = ? WHERE id = ?",
            (count, _now(), run_id),
        )


def set_run_dataset_path(run_id: str, path: str) -> None:
    with cursor() as cur:
        cur.execute(
            "UPDATE runs SET dataset_path = ?, updated_at = ? WHERE id = ?",
            (path, _now(), run_id),
        )


def get_run(run_id: str) -> Optional[dict]:
    with cursor() as cur:
        cur.execute("SELECT * FROM runs WHERE id = ?", (run_id,))
        row = cur.fetchone()
        return dict(row) if row else None


# --- items ---

def insert_item(run_id: str, item: Item) -> bool:
    """Returns False if this exact item id already exists within this same
    run (a defensive check against double-inserting one item; shouldn't
    normally trigger). Dedup ACROSS runs is a separate, opt-in decision made
    by the caller — see source_router.route()'s `dedupe_across_runs` handling,
    which controls whether existing_phashes/existing_text_sigs start
    pre-loaded with everything ever fetched or start empty."""
    with cursor() as cur:
        cur.execute("SELECT 1 FROM items WHERE id = ? AND run_id = ?", (item.id, run_id))
        if cur.fetchone():
            return False
        cur.execute(
            """INSERT INTO items (id, run_id, data_type, source_name, source_url, local_path,
               title, text_snippet, width, height, phash, text_hash, license, attribution,
               author, published_at, query_text, fetched_at, extra_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                item.id, run_id, item.data_type, item.source_name, item.source_url, item.local_path,
                item.title, (item.text or "")[:500], item.width, item.height, item.phash, item.text_hash,
                item.license, item.attribution, item.author, item.published_at, item.query_text,
                item.fetched_at, json.dumps(item.extra),
            ),
        )
        return True


def get_existing_phashes() -> list[tuple[str, str]]:
    with cursor() as cur:
        cur.execute("SELECT id, phash FROM items WHERE phash IS NOT NULL")
        return [(r["id"], r["phash"]) for r in cur.fetchall()]


def get_existing_text_hashes() -> list[tuple[str, str]]:
    with cursor() as cur:
        cur.execute("SELECT id, text_hash FROM items WHERE text_hash IS NOT NULL")
        return [(r["id"], r["text_hash"]) for r in cur.fetchall()]


def list_items_for_run(run_id: str, offset: int = 0, limit: int = 100) -> list[dict]:
    with cursor() as cur:
        cur.execute(
            "SELECT * FROM items WHERE run_id = ? ORDER BY rowid LIMIT ? OFFSET ?",
            (run_id, limit, offset),
        )
        return [dict(r) for r in cur.fetchall()]


def count_items_for_run(run_id: str) -> int:
    with cursor() as cur:
        cur.execute("SELECT COUNT(*) as c FROM items WHERE run_id = ?", (run_id,))
        return cur.fetchone()["c"]


def get_item(item_id: str) -> Optional[dict]:
    with cursor() as cur:
        cur.execute("SELECT * FROM items WHERE id = ?", (item_id,))
        row = cur.fetchone()
        return dict(row) if row else None


# --- scrape log ---

def log_scrape_decision(domain: str, url: str, allowed: bool, reason: str = "") -> None:
    with cursor() as cur:
        cur.execute(
            "INSERT INTO scrape_log (domain, url, allowed, reason, ts) VALUES (?, ?, ?, ?, ?)",
            (domain, url, 1 if allowed else 0, reason, _now()),
        )
