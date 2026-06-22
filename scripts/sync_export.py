"""Export everything changed since a given watermark into a small delta DB.

Used to push ingestion done on an Ollama-enabled desktop into a production
DB that doesn't run an LLM (see scripts/ollama_sync_run.sh for the full
loop: ingest -> export -> ship to server -> apply -> bump watermark).

Stateless on purpose: the caller (ollama_sync_run.sh) owns the watermark
file and only advances it after the delta has been successfully applied on
the remote end, so a failed transfer/apply never loses a delta.

Each table is filtered on whichever of its own columns reflects last-write
time (``updated_at`` for tables that get edited in place, an insert-only
timestamp otherwise), so both brand-new rows and edits to already-synced
rows are picked up.

Only ingestion-pipeline tables are exported. ``users``/``subscriptions``/
``seen_papers`` are live, per-server user activity and must never be
overwritten by a desktop sync.

Example:
  python scripts/sync_export.py --since 2026-06-20T00:00:00+00:00 --out delta.db
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import db  # noqa: E402
from db import SCHEMA, now_iso  # noqa: E402

# (table, column whose value reflects last write), in FK-safe order.
_TABLES = [
    ("fields", "updated_at"),
    ("papers", "fetched_at"),
    ("summaries", "updated_at"),
    ("paper_images", "created_at"),
    ("paper_fields", "created_at"),
    ("ingest_log", "created_at"),
]


def export_delta(db_path, out_path, since):
    """Write every row changed after ``since`` into a fresh delta DB at
    ``out_path``. Returns the per-table row counts."""
    if os.path.exists(out_path):
        os.remove(out_path)
    src = sqlite3.connect(db_path)
    src.row_factory = sqlite3.Row
    delta = sqlite3.connect(out_path)
    delta.row_factory = sqlite3.Row
    delta.executescript(SCHEMA)
    db._migrate(delta)  # delta needs the same columns as src (updated_at, etc.)
    delta.commit()

    counts = {}
    for table, ts_col in _TABLES:
        rows = src.execute(
            f"SELECT * FROM {table} WHERE {ts_col} > ?", (since,)
        ).fetchall()
        if rows:
            cols = rows[0].keys()
            placeholders = ",".join("?" * len(cols))
            delta.executemany(
                f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({placeholders})",
                [tuple(r[c] for c in cols) for r in rows],
            )
        counts[table] = len(rows)
    delta.commit()
    delta.close()
    src.close()
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=config.DATABASE_PATH,
                         help="Source DB to export from (default: DATABASE_PATH).")
    parser.add_argument("--out", required=True, help="Path to write the delta DB.")
    parser.add_argument("--since", required=True,
                         help="ISO-8601 watermark; rows changed after this are exported.")
    args = parser.parse_args()

    # Captured before querying, so anything written during/after this run is
    # safely re-picked-up by the next sync once the caller advances past it.
    new_watermark = now_iso()
    counts = export_delta(args.db, args.out, args.since)
    total = sum(counts.values())
    if total == 0:
        print(f"Nothing changed since {args.since}.", file=sys.stderr)
    else:
        print(f"Exported {total} row(s) changed since {args.since}: {counts}", file=sys.stderr)
    # The only thing meant for the caller to parse programmatically.
    print(new_watermark)


if __name__ == "__main__":
    main()
