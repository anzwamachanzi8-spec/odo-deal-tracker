"""SQLite storage: every run appends a snapshot, so price history builds up over time.

Tables
  runs       one row per fetch (when, fast/full, how many items)
  snapshots  one row per product per run; never updated or deleted
  changes    what changed compared with the previous run of the same mode:
             'new' (never seen before), 'returned' (seen in an older run, missing last run),
             'removed' or 'price_change'

Why compare with the previous run of the *same mode*? A fast check only sees Today's Deals.
Comparing it with a full run would wrongly report every Extra Time and Clearance item as removed.
"""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,      -- ISO time in UTC
    mode        TEXT NOT NULL,      -- 'fast' or 'full'
    item_count  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    run_id          INTEGER NOT NULL REFERENCES runs(run_id),
    product_id      INTEGER NOT NULL,
    name            TEXT,
    brand           TEXT,
    category        TEXT,
    section         TEXT,
    price           REAL,
    was_price       REAL,
    has_price_range INTEGER,        -- SQLite has no boolean type: 0 or 1
    is_sold_out     INTEGER,
    is_promo        INTEGER,
    deal_ends       TEXT,
    url             TEXT,
    fetched_at      TEXT NOT NULL,
    PRIMARY KEY (run_id, product_id)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_product ON snapshots(product_id);

CREATE TABLE IF NOT EXISTS changes (
    run_id        INTEGER NOT NULL REFERENCES runs(run_id),
    product_id    INTEGER NOT NULL,
    change_type   TEXT NOT NULL,    -- 'new', 'returned', 'removed', 'price_change'
    old_price     REAL,
    new_price     REAL,
    old_was_price REAL,
    new_was_price REAL
);
CREATE INDEX IF NOT EXISTS idx_changes_run ON changes(run_id);

-- One row per alert sent. The primary key makes "same item at the same price" impossible
-- to alert twice; if the price changes, it's a new deal and may be alerted again.
CREATE TABLE IF NOT EXISTS alerts (
    product_id  INTEGER NOT NULL,
    price       REAL NOT NULL,
    tier        TEXT NOT NULL,
    alerted_at  TEXT NOT NULL,
    PRIMARY KEY (product_id, price)
);
"""

SNAPSHOT_COLUMNS = ["product_id", "name", "brand", "category", "section", "price", "was_price",
                    "has_price_range", "is_sold_out", "is_promo", "deal_ends", "url", "fetched_at"]


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row      # rows behave like dicts: row["price"]
    conn.executescript(SCHEMA)          # creates the tables the first time only
    return conn


def save_run(conn: sqlite3.Connection, mode: str, products: list[dict], started_at: str) -> dict:
    """Store one run and its changes. Returns a summary of what changed.

    Everything happens in one transaction: if anything fails, nothing is half-saved.
    """
    with conn:
        previous_run_id = latest_run_id(conn, mode)

        cur = conn.execute("INSERT INTO runs (started_at, mode, item_count) VALUES (?, ?, ?)",
                           (started_at, mode, len(products)))
        run_id = cur.lastrowid

        placeholders = ", ".join("?" for _ in SNAPSHOT_COLUMNS)
        conn.executemany(
            f"INSERT INTO snapshots (run_id, {', '.join(SNAPSHOT_COLUMNS)}) VALUES (?, {placeholders})",
            [(run_id, *[p[c] for c in SNAPSHOT_COLUMNS]) for p in products],
        )

        changes = _detect_changes(conn, previous_run_id, products)
        conn.executemany(
            "INSERT INTO changes VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(run_id, c["product_id"], c["change_type"], c["old_price"], c["new_price"],
              c["old_was_price"], c["new_was_price"]) for c in changes],
        )

    return {"run_id": run_id, "first_run": previous_run_id is None, "changes": changes}


def latest_run_id(conn, mode: str) -> int | None:
    row = conn.execute("SELECT MAX(run_id) FROM runs WHERE mode = ?", (mode,)).fetchone()
    return row[0]


def _detect_changes(conn, previous_run_id: int | None, products: list[dict]) -> list[dict]:
    """Compare this run's products with the previous run's snapshot (same mode)."""
    if previous_run_id is None:
        return []  # first run: nothing to compare with, so we don't mark 1,000+ items as 'new'

    before = {row["product_id"]: row for row in
              conn.execute("SELECT * FROM snapshots WHERE run_id = ?", (previous_run_id,))}
    # Every product seen in any earlier run. Clearance only shows 600 items at a time and the
    # selection rotates, so an item missing last run isn't necessarily new: it may be 'returned'.
    ever_seen = {row[0] for row in conn.execute(
        "SELECT DISTINCT product_id FROM snapshots WHERE run_id <= ?", (previous_run_id,))}
    now = {p["product_id"]: p for p in products}
    changes = []

    for pid, p in now.items():
        old = before.get(pid)
        if old is None:
            changes.append(_change(pid, "returned" if pid in ever_seen else "new", None, p))
        elif old["price"] != p["price"] or old["was_price"] != p["was_price"]:
            changes.append(_change(pid, "price_change", old, p))

    for pid, old in before.items():
        if pid not in now:
            changes.append(_change(pid, "removed", old, None))
    return changes


def _change(pid, change_type, old, new) -> dict:
    return {
        "product_id": pid,
        "change_type": change_type,
        "name": (new or old)["name"],
        "old_price": old["price"] if old else None,
        "new_price": new["price"] if new else None,
        "old_was_price": old["was_price"] if old else None,
        "new_was_price": new["was_price"] if new else None,
    }


def prune_fast_runs(conn, keep_days: float, now_iso: str) -> int:
    """Delete fast-check runs older than keep_days. Full runs are always kept.

    A fast check every 5 minutes adds ~358 rows each time (~100,000 a day). Full runs every
    30 minutes already keep the long-term price history, so old fast checks are just duplicates.
    Returns how many runs were deleted.
    """
    cutoff = (datetime.fromisoformat(now_iso) - timedelta(days=keep_days)).isoformat(timespec="seconds")
    old = [r[0] for r in conn.execute(
        "SELECT run_id FROM runs WHERE mode = 'fast' AND started_at < ?", (cutoff,))]
    # Always keep the newest fast run: the next fast check compares against it.
    newest = latest_run_id(conn, "fast")
    old = [r for r in old if r != newest]
    with conn:
        for table in ("snapshots", "changes", "runs"):
            conn.executemany(f"DELETE FROM {table} WHERE run_id = ?", [(r,) for r in old])
    return len(old)


def not_yet_alerted(conn, deals: list[dict]) -> list[dict]:
    """Keep only deals we haven't already alerted about at this exact price."""
    sent = {(r[0], r[1]) for r in conn.execute("SELECT product_id, price FROM alerts")}
    return [d for d in deals if (d["product_id"], d["price"]) not in sent]


def record_alert(conn, deal: dict, alerted_at: str):
    """Remember that we alerted about this deal. Call it only after the alert was sent."""
    with conn:
        conn.execute("INSERT OR IGNORE INTO alerts VALUES (?, ?, ?, ?)",
                     (deal["product_id"], deal["price"], deal["tier"], alerted_at))


def price_history(conn, product_id: int) -> list[dict]:
    """An item's price periods in time order, e.g. R500 (Mon-Tue) -> R400 (Wed) -> R500 (Thu).

    Consecutive snapshots with the same price are merged into one period. We do this in Python
    rather than with SQL GROUP BY, because GROUP BY would wrongly merge the two R500 periods.
    """
    rows = conn.execute(
        "SELECT price, was_price, fetched_at FROM snapshots WHERE product_id = ? ORDER BY run_id",
        (product_id,),
    )
    periods = []
    for row in rows:
        last = periods[-1] if periods else None
        if last and (last["price"], last["was_price"]) == (row["price"], row["was_price"]):
            last["last_seen"] = row["fetched_at"]
            last["times_seen"] += 1
        else:
            periods.append({"price": row["price"], "was_price": row["was_price"],
                            "first_seen": row["fetched_at"], "last_seen": row["fetched_at"],
                            "times_seen": 1})
    return periods
