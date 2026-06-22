"""One-off: re-run the full primary-field classifier over every paper, with
permission to create new fields. Intended for use after loosening the
classifier's bar for proposing new fields (see ingest/classifier.py) — older
papers that were shoehorned into a loosely-fitting existing field can move to
a better, possibly brand-new one. A paper's previous primary/secondary field
links are replaced entirely by the fresh decision (its old primary survives
as a secondary if the classifier still says it fits).

Examples:
  python scripts/reclassify_fields.py            # one batch of 50
  python scripts/reclassify_fields.py --all       # drain every paper
  python scripts/reclassify_fields.py --all --limit 100
  python scripts/reclassify_fields.py --all --sensitivity 0.2  # reluctant to add fields
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
        description="Reclassify every paper's primary field, allowing new fields."
    )
    parser.add_argument("--limit", type=int, default=50,
                        help="Max papers per batch (default 50).")
    parser.add_argument("--all", action="store_true",
                        help="Keep going until every paper has been reclassified.")
    parser.add_argument("--quiet", action="store_true",
                        help="Only print the batch/run summary, no per-paper lines.")
    parser.add_argument("--sensitivity", type=float, default=None,
                        help="Override config.NEW_FIELD_SENSITIVITY (0.0-1.0) for "
                             "this run: 0 = never create a new field, 1 = create "
                             "one liberally. Default: config.NEW_FIELD_SENSITIVITY "
                             f"({config.NEW_FIELD_SENSITIVITY}).")
    args = parser.parse_args()

    if args.sensitivity is not None and not 0.0 <= args.sensitivity <= 1.0:
        parser.error("--sensitivity must be between 0.0 and 1.0")

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    if not config.LLM_ENABLED:
        print("LLM features are disabled (LLM_ENABLED=false); nothing to reclassify.")
        sys.exit(1)

    init_db()
    after_id = 0
    total = 0
    new_fields_created = 0
    moved = 0

    def on_paper(info):
        nonlocal new_fields_created, moved
        title = (info["title"] or "")[:70]
        old, new = info["old_primary_slug"], info["new_field_slug"]
        if info["created_new"]:
            new_fields_created += 1
            tag = "NEW FIELD"
        elif old != new:
            moved += 1
            tag = "moved"
        else:
            tag = "unchanged"
        extra = f" (also: {', '.join(info['secondary_slugs'])})" if info["secondary_slugs"] else ""
        if not args.quiet:
            print(f"  [{tag:>9}] {title!r}: {old or '(none)'} -> {new}{extra}")

    while True:
        n, after_id = pipeline.reclassify_primary_fields(
            limit=args.limit, after_id=after_id, on_paper=on_paper,
            sensitivity=args.sensitivity,
        )
        total += n
        print(f"Reclassified {n} papers (cursor at paper_id {after_id}).")
        if not args.all or n == 0:
            break
    print(
        f"\nDone. Reclassified {total} papers: {moved} moved to a different "
        f"existing field, {new_fields_created} moved to a brand-new field, "
        f"{total - moved - new_fields_created} unchanged."
    )


if __name__ == "__main__":
    main()
