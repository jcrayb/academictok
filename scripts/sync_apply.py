"""Apply a delta DB (produced by sync_export.py) onto a target DB.

Run this on the machine that owns the target DB (the production server),
against a delta file that's already been copied there. Idempotent: rows
carry their original ids/keys, so INSERT OR REPLACE makes re-applying the
same delta (or one that partially applied before a failure) safe.

Example:
  python scripts/sync_apply.py --delta /tmp/delta.db --db ./academictok.db
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from db import init_db  # noqa: E402

# Same FK-safe order as sync_export.py's _TABLES.
_TABLES = ["fields", "papers", "summaries", "paper_images", "paper_fields", "ingest_log"]


def apply_delta(db_path, delta_path):
    init_db()  # make sure the target has the current schema before attaching
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute("ATTACH DATABASE ? AS delta", (delta_path,))
    counts = {}
    try:
        for table in _TABLES:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
            col_list = ",".join(cols)
            cur = conn.execute(
                f"INSERT OR REPLACE INTO main.{table} ({col_list}) "
                f"SELECT {col_list} FROM delta.{table}"
            )
            counts[table] = cur.rowcount
        conn.commit()
    finally:
        conn.execute("DETACH DATABASE delta")
        conn.close()
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delta", required=True, help="Path to the delta DB to apply.")
    parser.add_argument("--db", default=config.DATABASE_PATH,
                         help="Target DB to apply onto (default: DATABASE_PATH).")
    args = parser.parse_args()

    if not os.path.exists(args.delta):
        print(f"No delta file at {args.delta}.", file=sys.stderr)
        sys.exit(1)

    counts = apply_delta(args.db, args.delta)
    print(f"Applied: {counts}")


if __name__ == "__main__":
    main()
