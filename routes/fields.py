"""Public field listing and per-field / paper detail endpoints."""
import threading

from flask import Blueprint, g, jsonify, request

import config
import models
from auth import optional_auth
from db import get_conn

bp = Blueprint("fields", __name__)

_FIELD_SORTS = ("citation", "year", "random")


@bp.route("/api/fields", methods=["GET"])
def list_fields():
    q = request.args.get("q", "").strip()
    with get_conn() as conn:
        fields = models.search_fields(conn, q) if q else models.list_fields(conn)
    return jsonify({"fields": fields})


@bp.route("/api/fields/<slug>", methods=["GET"])
@optional_auth
def field_feed(slug):
    """Browse a field's stored papers, sorted by citation / year / random
    (default), optionally hiding papers the signed-in user has already seen.
    Offset-paginated so arbitrary sorts work.
    """
    page_size = request.args.get("page_size", default=config.DEFAULT_PAGE_SIZE, type=int)
    offset = request.args.get("offset", default=0, type=int)
    sort = request.args.get("sort", default="random")
    if sort not in _FIELD_SORTS:
        sort = "random"
    seed = request.args.get("seed", default=0, type=int)
    unseen = request.args.get("unseen", default="0").lower() in ("1", "true")
    with get_conn() as conn:
        field = models.get_field_by_slug(conn, slug)
        if not field:
            return jsonify({"error": "not found"}), 404
        unseen_uid = g.uid if (unseen and g.uid) else None
        items = models.feed_for_field_sorted(
            conn, field["id"], page_size, offset=offset, sort=sort,
            seed=seed, unseen_uid=unseen_uid,
        )
    return jsonify({
        "field": {"slug": field["slug"], "name": field["name"]},
        "items": items,
        "next_offset": offset + len(items),
        "page_size": page_size,
    })


@bp.route("/api/fields/<slug>/discover", methods=["GET"])
def field_discover(slug):
    """Fast, on-the-fly ingestion of a few new papers for a field (infinite
    scroll). Short summary only — no PDF, figures, detailed summary, or
    classification. Returns the new items in feed shape plus an exhausted flag.

    With the LLM disabled there's no way to summarize/classify new papers, so
    this falls back to reporting "exhausted" immediately — infinite scroll then
    just stops once the field's stored papers run out.
    """
    if not config.LLM_ENABLED:
        return jsonify({"items": [], "exhausted": True})

    from ingest import pipeline  # lazy: pulls in the summarizer/S2 stack

    limit = request.args.get("limit", default=config.DISCOVER_BATCH, type=int)
    limit = max(1, min(limit, 30))  # bound the per-scroll cost
    new_ids, exhausted = pipeline.discover_field(slug, limit=limit)
    items = []
    if new_ids:
        with get_conn() as conn:
            items = models.feed_for_paper_ids(conn, new_ids)
    return jsonify({"items": items, "exhausted": exhausted})


# Papers currently being enriched in a background thread, so concurrent views /
# polls don't spawn duplicate generation for the same paper.
_enrich_lock = threading.Lock()
_enriching: set = set()


def _enrich_async(paper_id, want_long, want_imgs):
    """Generate a paper's detailed summary + figures off the request thread."""
    from ingest import pipeline  # lazy: pulls in the summarizer/S2 stack

    with _enrich_lock:
        if paper_id in _enriching:
            return
        _enriching.add(paper_id)

    def run():
        try:
            pipeline.enrich_paper(paper_id, want_long=want_long, want_imgs=want_imgs)
        finally:
            with _enrich_lock:
                _enriching.discard(paper_id)

    threading.Thread(target=run, daemon=True, name=f"enrich-{paper_id}").start()


@bp.route("/api/papers/<s2_paper_id>", methods=["GET"])
def paper_detail(s2_paper_id):
    """Return a paper immediately (short summary). The expensive detailed summary
    and figures are generated in the background; the client polls for them.

    The detailed summary is generated lazily on first view; abstract→full-text
    upgrades and figure backfill are left to the scheduled precompute pass
    (scripts/upgrade_summaries.py) so a view never blocks on a PDF + LLM call.
    ``?enrich=0`` (used by the client's polls) reads state without re-triggering.

    Fallback: with the LLM disabled, the paper is served as-is (short summary
    only) and ``enriching`` is always false — there's no enrichment pass to
    wait for, so the client never polls.
    """
    with get_conn() as conn:
        paper = models.get_paper_detail(conn, s2_paper_id)
    if not paper:
        return jsonify({"error": "not found"}), 404

    has_pdf = config.ENABLE_PDF_INGEST and bool(paper.get("pdf_url"))
    # first-time detailed summary only
    want_long = config.LLM_ENABLED and not paper.get("long_body")
    want_imgs = (config.LLM_ENABLED and not paper.get("images")
                 and has_pdf and config.ENABLE_FIGURE_EXTRACT)
    enrich = request.args.get("enrich", "1") != "0"
    if enrich and (want_long or want_imgs):
        _enrich_async(paper["paper_id"], want_long, want_imgs)
    # The client polls while the detailed summary isn't ready yet.
    paper["enriching"] = bool(want_long)
    return jsonify(paper)
