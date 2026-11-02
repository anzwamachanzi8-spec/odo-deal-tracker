"""SQLite storage for price history, stored compactly: a product's details are saved only
when something about it changes, not again on every run.

Tables
  runs       one row per fetch (when, fast/full, how many items)
  snapshots  one row per *version* of a product: saved the first time we see it, and again
             only when its price, was-price, stock or other details change. run_id is the run
             where that version was first seen. Rows are never updated or deleted.
  presence   which runs each product appeared in, as streaks: "product 123 was in every full
             run from #40 to #95". One row per streak instead of one row per run.
  changes    what changed compared with the previous run of the same mode:
             'new' (never seen before), 'returned' (seen in an older run, missing last run),
             'removed' or 'price_change'
  alerts     every alert sent, so the same item at the same price is never alerted twice

Why not just save every product on every run? We did at first: it grew 19 MB in one day,
and 97% of the rows were exact repeats of the row before. Snapshots + presence hold the same
information: "what did run X see?" = the products present in run X, each with the latest
version saved at or before run X.

Why compare with the previous run of the *same mode*? A fast check only sees Today's Deals.
Comparing it with a full run would wrongly report every Extra Time and Clearance item as removed.
"""

import json
import sqlite3
from pathlib import Path

FORMAT_VERSION = 2  # 1 = the old one-row-per-product-per-run format (see scripts/migrate_db.py)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,      -- ISO time in UTC
    mode        TEXT NOT NULL,      -- 'fast' or 'full'
    item_count  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    run_id          INTEGER NOT NULL REFERENCES runs(run_id),  -- run where this version was first seen
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
CREATE INDEX IF NOT EXISTS idx_snapshots_product ON snapshots(product_id, run_id);

CREATE TABLE IF NOT EXISTS presence (
    product_id    INTEGER NOT NULL,
    mode          TEXT NOT NULL,    -- 'fast' or 'full': each mode has its own streaks
    first_run_id  INTEGER NOT NULL,
    last_run_id   INTEGER NOT NULL,
    PRIMARY KEY (product_id, mode, first_run_id)
);
CREATE INDEX IF NOT EXISTS idx_presence_last ON presence(mode, last_run_id);

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
# A new version is saved when any of these differ from the last saved version.
TRACKED_COLUMNS = [c for c in SNAPSHOT_COLUMNS if c not in ("product_id", "fetched_at")]


class OldDatabaseFormat(Exception):
    """The database still uses the old format; convert it with scripts/migrate_db.py."""


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row      # rows behave like dicts: row["price"]
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    has_runs = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'runs'").fetchone()
    if version < FORMAT_VERSION and has_runs and conn.execute("SELECT 1 FROM runs LIMIT 1").fetchone():
        conn.close()
        raise OldDatabaseFormat(f"{db_path} uses the old storage format. Convert it first:\n"
                                f"  python scripts/migrate_db.py")
    conn.executescript(SCHEMA)          # creates the tables the first time only
    conn.execute(f"PRAGMA user_version = {FORMAT_VERSION}")
    return conn


def save_run(conn: sqlite3.Connection, mode: str, products: list[dict], started_at: str) -> dict:
    """Store one run and its changes. Returns a summary of what changed.

    Everything happens in one transaction: if anything fails, nothing is half-saved.
    """
    with conn:
        run = record_run(conn, mode, products, started_at)
        changes = _detect_changes(run["previous_run_id"], run["before"], run["known"], products)
        conn.executemany(
            "INSERT INTO changes VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(run["run_id"], c["product_id"], c["change_type"], c["old_price"], c["new_price"],
              c["old_was_price"], c["new_was_price"]) for c in changes],
        )

    return {"run_id": run["run_id"], "first_run": run["previous_run_id"] is None,
            "changes": changes, "new_versions": run["new_versions"]}


def record_run(conn, mode: str, products: list[dict], started_at: str, run_id: int | None = None) -> dict:
    """Add a run, the product versions that are new or changed, and extend the presence streaks.

    Used by save_run, and by scripts/migrate_db.py (which passes the old run_id to keep it).
    Doesn't commit: the caller decides the transaction.
    """
    previous_run_id = latest_run_id(conn, mode)
    before = {r["product_id"]: r for r in products_in_run(conn, previous_run_id)} if previous_run_id else {}
    ids = [p["product_id"] for p in products]
    known = _latest_versions(conn, ids)  # last saved version of each product, from any mode

    cur = conn.execute("INSERT INTO runs (run_id, started_at, mode, item_count) VALUES (?, ?, ?, ?)",
                       (run_id, started_at, mode, len(products)))
    run_id = cur.lastrowid

    changed = [p for p in products
               if p["product_id"] not in known or _values(known[p["product_id"]]) != _values(p)]
    placeholders = ", ".join("?" for _ in SNAPSHOT_COLUMNS)
    conn.executemany(
        f"INSERT INTO snapshots (run_id, {', '.join(SNAPSHOT_COLUMNS)}) VALUES (?, {placeholders})",
        [(run_id, *[p[c] for c in SNAPSHOT_COLUMNS]) for p in changed],
    )

    # Products that were also in the previous run of this mode continue their streak;
    # the others start a new one.
    continuing = [pid for pid in ids if pid in before]
    conn.execute("""UPDATE presence SET last_run_id = ?
                    WHERE mode = ? AND last_run_id = ? AND product_id IN (SELECT value FROM json_each(?))""",
                 (run_id, mode, previous_run_id, json.dumps(continuing)))
    conn.executemany("INSERT INTO presence VALUES (?, ?, ?, ?)",
                     [(pid, mode, run_id, run_id) for pid in ids if pid not in before])

    return {"run_id": run_id, "previous_run_id": previous_run_id, "before": before,
            "known": known, "new_versions": len(changed)}


def _values(item) -> tuple:
    """The tracked values, with True/False as 1/0 so a fresh product compares equal to a stored row."""
    return tuple(int(item[c]) if isinstance(item[c], bool) else item[c] for c in TRACKED_COLUMNS)


def _latest_versions(conn, product_ids: list[int]) -> dict:
    """{product_id: its most recently saved version} for the given products."""
    rows = conn.execute("""
        SELECT s.* FROM snapshots s
        JOIN (SELECT product_id, MAX(run_id) AS run_id FROM snapshots
              WHERE product_id IN (SELECT value FROM json_each(?)) GROUP BY product_id) latest
          ON s.product_id = latest.product_id AND s.run_id = latest.run_id""",
                        (json.dumps(product_ids),))
    return {r["product_id"]: r for r in rows}


def products_in_run(conn, run_id: int) -> list[sqlite3.Row]:
    """Everything one run saw: each product present in that run, as its version at that time."""
    return conn.execute("""
        SELECT s.* FROM presence p
        JOIN runs r ON r.run_id = :run AND p.mode = r.mode
        JOIN snapshots s ON s.product_id = p.product_id
         AND s.run_id = (SELECT MAX(run_id) FROM snapshots
                         WHERE product_id = p.product_id AND run_id <= :run)
        WHERE p.first_run_id <= :run AND p.last_run_id >= :run""", {"run": run_id}).fetchall()


def latest_run_id(conn, mode: str) -> int | None:
    row = conn.execute("SELECT MAX(run_id) FROM runs WHERE mode = ?", (mode,)).fetchone()
    return row[0]


def _detect_changes(previous_run_id: int | None, before: dict, known: dict, products: list[dict]) -> list[dict]:
    """Compare this run's products with the previous run's (same mode).

    known holds every product saved in any earlier run. Clearance only shows 600 items at a
    time and the selection rotates, so an item missing last run isn't necessarily new: it may
    be 'returned'.
    """
    if previous_run_id is None:
        return []  # first run: nothing to compare with, so we don't mark 1,000+ items as 'new'

    now = {p["product_id"]: p for p in products}
    changes = []
    for pid, p in now.items():
        old = before.get(pid)
        if old is None:
            changes.append(_change(pid, "returned" if pid in known else "new", None, p))
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


def not_yet_alerted(conn, deals: list[dict]) -> list[dict]:
    """Keep only deals we haven't already alerted about at this exact price."""
    sent = {(r[0], r[1]) for r in conn.execute("SELECT product_id, price FROM alerts")}
    return [d for d in deals if (d["product_id"], d["price"]) not in sent]


def record_alert(conn, deal: dict, alerted_at: str):
    """Remember that we alerted about this deal. Call it only after the alert was sent."""
    with conn:
        conn.execute("INSERT OR IGNORE INTO alerts VALUES (?, ?, ?, ?)",
                     (deal["product_id"], deal["price"], deal["tier"], alerted_at))


def content_counts(conn) -> tuple:
    """Row counts of the tables that hold real data. If these didn't change during a run,
    the run found nothing new (the cloud version then skips saving the database)."""
    return tuple(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                 for t in ("snapshots", "changes", "alerts"))


def price_history(conn, product_id: int) -> list[dict]:
    """An item's price periods in time order, e.g. R500 (Mon-Tue) -> R400 (Wed) -> R500 (Thu).

    We list every run the item was seen in, give each the version in effect at that time, and
    merge consecutive runs with the same price into one period. We do this in Python rather than
    with SQL GROUP BY, because GROUP BY would wrongly merge the two R500 periods.
    """
    versions = conn.execute(
        "SELECT run_id, price, was_price FROM snapshots WHERE product_id = ? ORDER BY run_id",
        (product_id,)).fetchall()
    sightings = conn.execute("""
        SELECT r.run_id, r.started_at FROM presence p
        JOIN runs r ON r.mode = p.mode AND r.run_id BETWEEN p.first_run_id AND p.last_run_id
        WHERE p.product_id = ? ORDER BY r.run_id""", (product_id,))

    periods, v = [], -1
    for run_id, seen_at in sightings:
        while v + 1 < len(versions) and versions[v + 1]["run_id"] <= run_id:
            v += 1  # move to the version that was current in this run
        price, was = versions[v]["price"], versions[v]["was_price"]
        last = periods[-1] if periods else None
        if last and (last["price"], last["was_price"]) == (price, was):
            last["last_seen"] = seen_at
            last["times_seen"] += 1
        else:
            periods.append({"price": price, "was_price": was, "first_seen": seen_at,
                            "last_seen": seen_at, "times_seen": 1})
    return periods
