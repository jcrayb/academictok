"""CLI entrypoint for ingestion.

Examples:
  python scripts/run_ingest.py                 # all seed fields
  python scripts/run_ingest.py --field queueing-theory --limit 10
  python scripts/run_ingest.py --query "graph neural networks" --limit 15
  python scripts/run_ingest.py --fast           # fetch+classify everything first,
                                                 # then backfill detailed summaries
  python scripts/run_ingest.py --fast --defer-enrich   # same, but skip the
                                                 # detailed-summary pass here entirely
                                                 # (e.g. bulk_ingest.sh runs it once
                                                 # at the end, across every query)
"""
import argparse
import logging
import os
import sys

# Allow running as `python scripts/run_ingest.py` from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from db import init_db  # noqa: E402
from ingest import pipeline  # noqa: E402
from seeds import seed_fields  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Ingest papers into AcademicTok.")
    parser.add_argument("--field", help="Only ingest this seed field slug.")
    parser.add_argument("--query", help="Ingest an ad-hoc query instead of seeds.")
    parser.add_argument("--limit", type=int, default=20,
                        help="Max papers per query (default 20).")
    parser.add_argument("--min-citations", type=int, default=0,
                        help="Drop papers below this citation count.")
    parser.add_argument("--fast", action="store_true",
                        help="Fetch, summarize, and classify every paper first "
                             "(skipping PDF/figures and the detailed summary), "
                             "then do a second pass filling in detailed summaries "
                             "once every paper already has its field(s).")
    parser.add_argument("--defer-enrich", action="store_true",
                        help="With --fast, skip the detailed-summary pass here "
                             "(leave it for a later enrich_pending/"
                             "upgrade_summaries.py run).")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    if not config.LLM_ENABLED:
        print("LLM features are disabled (LLM_ENABLED=false); ingestion needs "
              "the classifier. Run this on a machine with Ollama against the "
              "same database, or set LLM_ENABLED=true.")
        sys.exit(1)

    init_db()
    seed_fields()

    if args.query:
        totals = pipeline.ingest_query(
            args.query, limit=args.limit, min_citations=args.min_citations,
            fast=args.fast, defer_enrich=args.defer_enrich,
        )
    else:
        totals = pipeline.ingest_seed_fields(
            limit=args.limit, min_citations=args.min_citations,
            only_slug=args.field, fast=args.fast, defer_enrich=args.defer_enrich,
        )

    print(f"\nDone. {totals}")


if __name__ == "__main__":
    main()
