"""One-off backfill: find secondary fields for papers ingested before
secondary-field classification existed (i.e. papers that still only have a
primary field link).

Examples:
  python scripts/backfill_secondary_fields.py            # one batch of 50
  python scripts/backfill_secondary_fields.py --all      # drain the whole backlog
  python scripts/backfill_secondary_fields.py --all --limit 100
  python scripts/backfill_secondary_fields.py --all --force  # recompute every paper
"""
import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from db import init_db  # noqa: E402
from ingest import pipeline  # noqa: E402


def main():
    parser = argparse.ArgumentParser(
        description="Backfill secondary fields for older papers that lack them."
    )
    parser.add_argument("--limit", type=int, default=50,
                        help="Max papers per batch (default 50).")
    parser.add_argument("--all", action="store_true",
                        help="Keep going until every paper has been considered.")
    parser.add_argument("--force", action="store_true",
                        help="Recompute secondary fields for EVERY paper, not just "
                             "ones that lack them, replacing whatever they have.")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    if not config.LLM_ENABLED:
        print("LLM features are disabled (LLM_ENABLED=false); nothing to backfill.")
        sys.exit(1)

    init_db()
    after_id = 0
    total = 0
    while True:
        n, after_id = pipeline.backfill_secondary_fields(
            limit=args.limit, after_id=after_id, force=args.force
        )
        total += n
        print(f"Processed {n} papers (cursor at paper_id {after_id}).")
        if not args.all or n == 0:
            break
    print(f"\nDone. Considered {total} papers.")


if __name__ == "__main__":
    main()
