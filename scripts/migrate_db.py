"""Convert a database from the old format (every product saved on every run) to the compact one.

    python scripts/migrate_db.py

It replays the old runs in order through the new storage code, keeping the same run numbers,
changes and alerts. The old file is kept as odo_deals.old.db, so nothing is lost if
something goes wrong. Stop the scheduled task first, so no run writes to the file meanwhile.
"""

import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from odo_tracker import storage  # noqa: E402
from odo_tracker.config import load_config  # noqa: E402


def migrate(old_path: Path, new_path: Path):
    old = sqlite3.connect(f"file:{old_path}?mode=ro", uri=True)
    old.row_factory = sqlite3.Row
    if old.execute("PRAGMA user_version").fetchone()[0] >= storage.FORMAT_VERSION:
        sys.exit(f"{old_path} is already in the new format.")

    new = storage.connect(new_path)
    runs = old.execute("SELECT * FROM runs ORDER BY run_id").fetchall()
    snapshots = defaultdict(list)
    for row in old.execute("SELECT * FROM snapshots ORDER BY run_id, rowid"):
        snapshots[row["run_id"]].append(dict(row))

    with new:  # one transaction: all or nothing
        for run in runs:
            storage.record_run(new, run["mode"], snapshots[run["run_id"]], run["started_at"], run_id=run["run_id"])
        new.executemany("INSERT INTO changes VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [tuple(r) for r in old.execute("SELECT * FROM changes")])
        new.executemany("INSERT INTO alerts VALUES (?, ?, ?, ?)",
                        [tuple(r) for r in old.execute("SELECT * FROM alerts")])
        # Make the next run number continue after the last old one.
        new.execute("UPDATE sqlite_sequence SET seq = (SELECT MAX(run_id) FROM runs) WHERE name = 'runs'")

    # Check: every old run must look exactly the same when read back from the new format.
    for run in runs:
        before = {r["product_id"]: storage._values(r) for r in snapshots[run["run_id"]]}
        after = {r["product_id"]: storage._values(r) for r in storage.products_in_run(new, run["run_id"])}
        if before != after:
            sys.exit(f"Check failed for run #{run['run_id']}; the old database is untouched.")

    old_rows = sum(len(v) for v in snapshots.values())
    new_rows = new.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    new.execute("VACUUM")
    new.close()
    old.close()
    print(f"Converted {len(runs)} runs: {old_rows:,} snapshot rows -> {new_rows:,} versions. "
          f"All runs check out.")


def main():
    config = load_config()
    db = config["paths"]["data_dir"] / config["paths"]["database"]
    backup = db.with_name(db.stem + ".old.db")
    tmp = db.with_name(db.stem + ".new.db")
    tmp.unlink(missing_ok=True)
    migrate(db, tmp)
    print(f"Size: {db.stat().st_size / 1e6:.1f} MB -> {tmp.stat().st_size / 1e6:.1f} MB")
    db.replace(backup)
    tmp.replace(db)
    print(f"Done. The old file is kept as {backup.name}; delete it once you're happy.")


if __name__ == "__main__":
    main()
