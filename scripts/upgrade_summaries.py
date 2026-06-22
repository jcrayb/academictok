"""Background quality passes for the local LLM, meant to run on a schedule.

Two phases, both keeping the live feed fast by doing the heavy work offline:
  1. Short summaries: re-generate the quick OLLAMA_FAST_MODEL summaries written by
     the infinite-scroll discovery feed with the full OLLAMA_MODEL.
  2. Detailed summaries: precompute the on-open detailed summary (+ figures) for
     papers that don't have one yet, so opening a post is instant.

Examples:
  python scripts/upgrade_summaries.py                 # both phases, up to 50 each
  python scripts/upgrade_summaries.py --all           # drain both backlogs
  python scripts/upgrade_summaries.py --detailed-only --all
"""
import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from db import init_db  # noqa: E402
from ingest import pipeline  # noqa: E402


def _drain(fn, limit, run_all, label):
    total = 0
    while True:
        n = fn(limit=limit)
        total += n
        if not run_all or n == 0:
            break
    print(f"{label}: {total}.")


def main():
    parser = argparse.ArgumentParser(description="Background LLM quality passes.")
    parser.add_argument("--limit", type=int, default=50,
                        help="Max items per phase per batch (default 50).")
    parser.add_argument("--all", action="store_true",
                        help="Keep going until the backlog(s) are empty.")
    parser.add_argument("--short-only", action="store_true",
                        help="Only upgrade fast-model short summaries.")
    parser.add_argument("--detailed-only", action="store_true",
                        help="Only precompute detailed summaries + figures.")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    if not config.LLM_ENABLED:
        print("LLM features are disabled (LLM_ENABLED=false); nothing to upgrade.")
        sys.exit(1)

    init_db()
    if not args.detailed_only:
        _drain(pipeline.upgrade_pending_summaries, args.limit, args.all,
               "Upgraded short summaries")
    if not args.short_only:
        _drain(pipeline.enrich_pending, args.limit, args.all,
               "Precomputed detailed summaries")


if __name__ == "__main__":
    main()
