"""Orchestrates fetch -> summarize -> classify -> store."""
import logging
from concurrent.futures import ThreadPoolExecutor

import config
import models
from db import get_conn
from ingest import pdf_extract, semantic_scholar
from ingest.classifier import classify, classify_secondary, generate_keywords
from ingest.summarizer import caption_figure, detailed_summary, summarize

logger = logging.getLogger(__name__)


def ingest_query(query, limit=20, min_citations=0, on_progress=None, fast=False,
                  defer_enrich=False):
    """Fetch papers for a query and process each new one.

    ``fast=True`` skips the expensive part (PDF text/figures + detailed
    summary) while fetching, summarizing, and classifying every paper, so the
    whole batch gets its categories quickly instead of paying the PDF/detailed
    -summary cost serially per paper.

    By default (``defer_enrich=False``) that PDF/detailed-summary work then
    runs immediately afterwards, as a second pass over just this batch.
    ``defer_enrich=True`` skips that second pass entirely here — the papers are
    left for whoever calls ``enrich_pending``/``upgrade_summaries.py`` next, so
    a caller doing many queries in a row (e.g. bulk_ingest.sh) can ingest
    everything across the whole list first and run PDFs/detailed summaries
    once at the very end, instead of per query.

    Returns a dict of counts: {fetched, skipped, ingested, errors}.
    ``on_progress(counts, current)`` is called after each paper for live status.
    """
    papers = semantic_scholar.search_papers(
        query, limit=limit, min_citations=min_citations
    )
    counts = {"fetched": len(papers), "processed": 0, "skipped": 0,
              "ingested": 0, "errors": 0}
    if on_progress:
        on_progress(counts, None)

    new_paper_ids = []
    summaries = {}
    if fast:
        to_summarize = []
        for paper in papers:
            with get_conn() as conn:
                if not models.paper_exists(conn, paper["s2_paper_id"]):
                    to_summarize.append(paper)
        for paper, summary in _summarize_batch(to_summarize, config.OLLAMA_MODEL):
            summaries[paper["s2_paper_id"]] = summary

    for paper in papers:
        with get_conn() as conn:
            if models.paper_exists(conn, paper["s2_paper_id"]):
                counts["skipped"] += 1
                counts["processed"] += 1
                if on_progress:
                    on_progress(counts, paper["title"])
                continue
        try:
            if fast:
                summary = summaries.get(paper["s2_paper_id"])
                if summary is None:
                    raise RuntimeError("summarization failed")
                paper_id = _process_paper_fast(query, paper, summary)
                new_paper_ids.append(paper_id)
            else:
                _process_paper(query, paper)
            counts["ingested"] += 1
            logger.info("Ingested: %s", paper["title"][:70])
        except Exception as e:  # noqa: BLE001 - log and continue the batch
            counts["errors"] += 1
            logger.warning("Failed on '%s': %s", paper["title"][:50], e)
            with get_conn() as conn:
                models.log_ingest(
                    conn, query, paper["s2_paper_id"], "error", str(e)
                )
        counts["processed"] += 1
        if on_progress:
            on_progress(counts, paper["title"])

    if fast and not defer_enrich and new_paper_ids:
        if on_progress:
            on_progress(counts, "enriching detailed summaries…")
        for paper_id in new_paper_ids:
            try:
                enrich_paper(paper_id, want_long=True, want_imgs=True)
            except Exception as e:  # noqa: BLE001 - best-effort, keep going
                logger.warning("Fast-pass enrichment failed for paper %s: %s",
                               paper_id, e)
    return counts


def _extract_pdf(paper):
    """Best-effort: fetch + parse the open-access PDF. Returns (text, figures).

    The full-text and figure halves are toggled independently; when both are
    disabled the PDF isn't downloaded at all (the expensive part).
    """
    want_text = config.ENABLE_PDF_FULLTEXT
    want_figs = config.ENABLE_FIGURE_EXTRACT
    if (not config.ENABLE_PDF_INGEST or not paper.get("pdf_url")
            or not (want_text or want_figs)):
        return "", []
    try:
        pdf_bytes = pdf_extract.fetch_pdf(paper["pdf_url"])
        if not pdf_bytes:
            return "", []
        result = pdf_extract.extract(
            pdf_bytes, want_text=want_text, want_figures=want_figs
        )
        return result.get("text", ""), result.get("figures", [])
    except Exception as e:  # noqa: BLE001 - never let PDF issues drop a paper
        logger.warning("PDF processing failed for '%s': %s",
                       paper["title"][:50], e)
        return "", []


def _process_paper(query, paper):
    """Summarize, classify, and persist a single paper in one transaction."""
    summary = summarize(paper)

    # Read the open-access PDF (if any) to ground a richer summary + pull figures.
    full_text, figures = _extract_pdf(paper)

    # Thorough detailed summary shown when a post is opened. Best-effort: a
    # failure here shouldn't drop the paper (it can be backfilled on demand).
    # The query steers equation emphasis (math/applied-math papers get more);
    # long_source records whether full text grounded it, so the view path can
    # upgrade an abstract-only summary later.
    try:
        summary["long_body"] = detailed_summary(
            paper, full_text=full_text, topic_hint=query
        )
        summary["long_source"] = (
            "fulltext" if (full_text and full_text.strip()) else "abstract"
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Detailed summary failed for '%s': %s", paper["title"][:50], e)
        summary["long_body"] = None
        summary["long_source"] = None

    # Persist figures to disk and (optionally) caption them with a vision model.
    image_rows = pdf_extract.save_figures(paper["s2_paper_id"], figures) if figures else []
    for row, fig in zip(image_rows, figures):
        row["caption"] = caption_figure(fig["png"], paper["title"])

    # Classify against the current field list (read in its own connection).
    with get_conn() as conn:
        fields = models.fields_for_classifier(conn)
    decision = classify(paper, summary, fields)

    with get_conn() as conn:
        # Resolve the primary field, creating a new one if proposed.
        if "field_slug" in decision:
            field = models.get_field_by_slug(conn, decision["field_slug"])
            field_id = field["id"]
        else:
            nf = decision["new_field"]
            field_id = models.create_field(
                conn, nf["slug"], nf["name"], nf["description"], is_seed=0,
                keywords=nf.get("keywords"),
            )
            logger.info("Created new field: %s", nf["name"])

        paper_id = models.insert_paper(conn, paper)
        models.insert_summary(conn, paper_id, summary)
        models.link_paper_field(conn, paper_id, field_id, is_primary=1)
        # Secondary fields the paper also fits, so it surfaces on each of them.
        secondary_ids = []
        for sslug in decision.get("secondary_slugs", []):
            sf = models.get_field_by_slug(conn, sslug)
            if sf and sf["id"] != field_id:
                models.link_paper_field(conn, paper_id, sf["id"], is_primary=0)
                secondary_ids.append(sf["id"])
        for row in image_rows:
            models.insert_paper_image(conn, paper_id, row)
        models.log_ingest(
            conn, query, paper["s2_paper_id"], "classified",
            detail=f"field_id={field_id}, secondary={secondary_ids}, "
                   f"figures={len(image_rows)}")


def _process_paper_fast(query, paper, summary):
    """Cheap variant of ``_process_paper`` for ``ingest_query(..., fast=True)``:
    classify and persist using only the short summary, skipping PDF
    extraction/figures and the detailed summary entirely. The detailed summary
    is filled in afterwards by the fast-pass enrichment sweep (or, failing
    that, the regular scheduled enrichment pass). Returns the new paper_id.
    """
    summary["long_body"] = None
    summary["long_source"] = None

    with get_conn() as conn:
        fields = models.fields_for_classifier(conn)
    decision = classify(paper, summary, fields)

    with get_conn() as conn:
        if "field_slug" in decision:
            field = models.get_field_by_slug(conn, decision["field_slug"])
            field_id = field["id"]
        else:
            nf = decision["new_field"]
            field_id = models.create_field(
                conn, nf["slug"], nf["name"], nf["description"], is_seed=0,
                keywords=nf.get("keywords"),
            )
            logger.info("Created new field: %s", nf["name"])

        paper_id = models.insert_paper(conn, paper)
        models.insert_summary(conn, paper_id, summary)
        models.link_paper_field(conn, paper_id, field_id, is_primary=1)
        secondary_ids = []
        for sslug in decision.get("secondary_slugs", []):
            sf = models.get_field_by_slug(conn, sslug)
            if sf and sf["id"] != field_id:
                models.link_paper_field(conn, paper_id, sf["id"], is_primary=0)
                secondary_ids.append(sf["id"])
        models.log_ingest(
            conn, query, paper["s2_paper_id"], "classified",
            detail=f"field_id={field_id}, secondary={secondary_ids}, fast=1")
    return paper_id


# On-the-fly field discovery (infinite scroll). Kept deliberately cheap: a short
# summary and a direct field assignment — no PDF, figures, detailed summary, or
# LLM classification. Bounded so one scroll never fans out into many S2 calls.
MAX_DISCOVER_PAGES = 3
S2_MAX_OFFSET = 1000  # S2 relevance search rejects offsets beyond ~this.


def _summarize_batch(papers, model):
    """Summarize papers concurrently against Ollama (duds are dropped).

    Returns ``[(paper, summary), ...]``. Parallelism overlaps the per-paper LLM
    calls — the main cost of discovery — so a batch isn't a serial wait.
    """
    if not papers:
        return []

    def work(p):
        try:
            return (p, summarize(p, model=model))
        except Exception as e:  # noqa: BLE001 - skip the dud, keep the batch
            logger.warning("Discover summary failed for '%s': %s", p["title"][:50], e)
            return None

    workers = max(1, min(config.DISCOVER_CONCURRENCY, len(papers)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return [r for r in ex.map(work, papers) if r]


def discover_field(slug, limit=None):
    """Fetch a batch of *new* papers for a field and store them with only a short
    summary (the fast path for infinite scroll). Returns ``(new_paper_ids,
    exhausted)``; ``exhausted`` means Semantic Scholar has no more results.

    Speed: summaries use the small OLLAMA_FAST_MODEL (if set) and run in
    parallel; the full model re-summarizes them later via upgrade_pending_summaries.
    Pages forward from the field's stored discover offset, skipping papers we
    already have, bounded so one scroll never fans out into many S2 calls.

    Fallback: with the LLM disabled (``config.LLM_ENABLED=False``) there's no
    way to summarize/classify newly fetched papers, so this is a no-op that
    reports ``exhausted=True`` — infinite scroll then just stops at whatever
    is already stored in the DB instead of trying to live-fetch more.
    """
    if not config.LLM_ENABLED:
        return [], True
    limit = limit or config.DISCOVER_BATCH
    with get_conn() as conn:
        field = models.get_field_by_slug(conn, slug)
        if not field:
            return [], True
        field_id = field["id"]
        # Seed fields carry an explicit query; classifier-made fields fall back
        # to their display name, which is a serviceable search term.
        query = field.get("query") or field["name"]
        offset = field.get("discover_offset") or 0

    fast_model = config.OLLAMA_FAST_MODEL or None
    needs_upgrade = 1 if config.OLLAMA_FAST_MODEL else 0

    # Gather a batch of brand-new papers, paging past duplicates (bounded).
    new_papers, collected = [], set()
    exhausted = False
    pages = 0
    while len(new_papers) < limit and pages < MAX_DISCOVER_PAGES:
        if offset >= S2_MAX_OFFSET:
            exhausted = True
            break
        pages += 1
        try:
            papers = semantic_scholar.search_papers(query, limit=limit, offset=offset)
        except Exception as e:  # noqa: BLE001 - transient; let the client retry
            logger.warning("Discover search failed for '%s' (offset %d): %s",
                           slug, offset, e)
            break
        offset += limit
        if not papers:
            exhausted = True
            break
        for paper in papers:
            sid = paper["s2_paper_id"]
            if sid in collected:
                continue
            with get_conn() as conn:
                if not models.paper_exists(conn, sid):
                    collected.add(sid)
                    new_papers.append(paper)

    # Summarize the whole batch at once (parallel), then persist.
    new_ids = []
    for paper, summary in _summarize_batch(new_papers, fast_model):
        summary["needs_full_summary"] = needs_upgrade
        with get_conn() as conn:
            if models.paper_exists(conn, paper["s2_paper_id"]):
                continue  # raced with another request
            paper_id = models.insert_paper(conn, paper)
            models.insert_summary(conn, paper_id, summary)
            models.link_paper_field(conn, paper_id, field_id)
            models.log_ingest(conn, query, paper["s2_paper_id"], "discovered",
                              detail=f"field_id={field_id}, model={summary['model']}")
            new_ids.append(paper_id)

    # Remember how far we paged so the next scroll continues forward.
    with get_conn() as conn:
        models.set_field_discover_offset(conn, field_id, offset)
    return new_ids, exhausted


def upgrade_pending_summaries(limit=50):
    """Re-summarize fast-model discovery papers with the full OLLAMA_MODEL.

    Returns the number upgraded. Intended to run on a schedule
    (scripts/upgrade_summaries.py) so live-discovered cards become high quality.
    """
    with get_conn() as conn:
        rows = models.pending_full_summaries(conn, limit)
    upgraded = 0
    for row in rows:
        paper = {"title": row["title"], "abstract": row.get("abstract") or "",
                 "venue": row.get("venue") or "", "year": row.get("year")}
        try:
            s = summarize(paper, model=config.OLLAMA_MODEL)
        except Exception as e:  # noqa: BLE001 - leave the flag set, retry next run
            logger.warning("Summary upgrade failed for paper %s: %s", row["paper_id"], e)
            continue
        with get_conn() as conn:
            models.update_short_summary(
                conn, row["paper_id"], s["title"], s["body"], s["model"]
            )
        upgraded += 1
    return upgraded


def enrich_paper(paper_id, want_long=True, want_imgs=True):
    """Generate/upgrade a paper's detailed summary and/or pull its figures, then
    persist. Flask-free so it runs from a background thread (on first view) or the
    scheduled precompute pass. Best-effort: failures are logged, never raised.

    ``want_long`` writes a missing detailed summary, or upgrades an abstract-only
    one once full text is reachable; ``want_imgs`` pulls figures the first time.
    The PDF is fetched only for a requested, enabled half.
    """
    with get_conn() as conn:
        st = models.paper_enrichment_state(conn, paper_id)
    if not st:
        return

    raw = {"title": st["title"], "abstract": st["abstract"] or "",
           "venue": st["venue"] or "", "year": st["year"]}
    topic_hint = " ".join(st["field_names"])  # math/applied-math → more equations
    have_long = bool(st["long_body"])
    is_fulltext = st["long_source"] == "fulltext"
    want_long = want_long and ((not have_long) or not is_fulltext)
    want_imgs = want_imgs and not st["has_images"]

    fetch_text = want_long and config.ENABLE_PDF_FULLTEXT
    fetch_figs = want_imgs and config.ENABLE_FIGURE_EXTRACT
    full_text, figures = "", []
    if config.ENABLE_PDF_INGEST and st["pdf_url"] and (fetch_text or fetch_figs):
        try:
            pdf_bytes = pdf_extract.fetch_pdf(st["pdf_url"])
            if pdf_bytes:
                result = pdf_extract.extract(
                    pdf_bytes, want_text=fetch_text, want_figures=fetch_figs
                )
                full_text, figures = result.get("text", ""), result.get("figures", [])
        except Exception as e:  # noqa: BLE001
            logger.warning("Enrich PDF failed for paper %s: %s", paper_id, e)

    # Detailed summary: write when missing, or upgrade abstract-only once we have
    # full text. Skip the LLM call when neither applies.
    has_fulltext = bool(full_text and full_text.strip())
    if want_long and ((not have_long and (has_fulltext or raw["abstract"]))
                      or (not is_fulltext and has_fulltext)):
        try:
            long_body = detailed_summary(raw, full_text=full_text, topic_hint=topic_hint)
            source = "fulltext" if has_fulltext else "abstract"
            with get_conn() as conn:
                models.set_long_summary(conn, paper_id, long_body, source)
        except Exception as e:  # noqa: BLE001
            logger.warning("Enrich detailed summary failed for paper %s: %s", paper_id, e)

    if figures and not st["has_images"]:
        rows = pdf_extract.save_figures(st["s2_paper_id"], figures)
        for row, fig in zip(rows, figures):
            row["caption"] = caption_figure(fig["png"], raw["title"])
        with get_conn() as conn:
            for row in rows:
                models.insert_paper_image(conn, paper_id, row)

    # Mark attempted (even on failure) so the scheduled pass won't retry forever.
    with get_conn() as conn:
        models.mark_enriched(conn, paper_id)


def enrich_pending(limit=20):
    """Precompute detailed summaries (and figures) for papers that lack them, so
    opening a post is instant. Returns the number processed. Runs on a schedule.
    """
    with get_conn() as conn:
        ids = models.papers_needing_enrichment(conn, limit)
    for paper_id in ids:
        enrich_paper(paper_id, want_long=True, want_imgs=True)
    return len(ids)


def backfill_secondary_fields(limit=50, after_id=0, force=False):
    """Find secondary fields for papers. By default only papers that don't have
    any secondary fields yet (older papers ingested before secondary-field
    classification existed); ``force=True`` recomputes every paper's secondary
    fields from scratch, replacing whatever they currently have. Returns
    (processed_count, last_paper_id) so a long-running backfill can page
    forward via ``after_id``.
    """
    with get_conn() as conn:
        rows = models.papers_missing_secondary_fields(
            conn, limit, after_id=after_id, force=force
        )
        fields = models.fields_for_classifier(conn)

    last_id = after_id
    for row in rows:
        last_id = row["paper_id"]
        paper = {"title": row["title"]}
        summary = {"body": row["summary_body"]}
        try:
            secondary_slugs = classify_secondary(
                paper, summary, fields, row["primary_slug"], row["primary_name"]
            )
        except Exception as e:  # noqa: BLE001 - skip the dud, keep the batch
            logger.warning("Secondary backfill failed for paper %s: %s",
                           row["paper_id"], e)
            continue
        with get_conn() as conn:
            if force:
                # Recomputing from scratch: drop whatever secondary links exist
                # now, even if the new result is empty (the paper may no longer
                # fit fields it once did).
                models.clear_secondary_fields(conn, row["paper_id"])
            if not secondary_slugs:
                continue
            for sslug in secondary_slugs:
                sf = models.get_field_by_slug(conn, sslug)
                if sf:
                    models.link_paper_field(conn, row["paper_id"], sf["id"], is_primary=0)
            models.log_ingest(
                conn, "backfill_secondary_fields", None, "secondary_backfilled",
                detail=f"paper_id={row['paper_id']}, secondary={secondary_slugs}",
            )
    return len(rows), last_id


def reclassify_primary_fields(limit=50, after_id=0, on_paper=None, sensitivity=None):
    """Re-run the full primary-field classifier (with permission to create new
    fields) over every paper, replacing its primary AND secondary field links
    with a fresh decision. Useful after tuning the classifier's bar for
    creating new fields, so older papers that were shoehorned into a loosely-
    fitting existing field can move to a better (possibly new) one — secondary
    fields keep them discoverable under the old field too, when relevant.

    ``sensitivity`` overrides config.NEW_FIELD_SENSITIVITY for this run (0.0 =
    never create a new field, 1.0 = create one liberally).

    ``on_paper(info)`` is called after each paper with a dict: paper_id, title,
    old_primary_slug, new_field_slug, new_field_name, created_new,
    secondary_slugs — so a caller can print/log progress live.

    Returns (processed_count, last_paper_id) so a long-running pass can page
    forward via ``after_id``.
    """
    with get_conn() as conn:
        rows = models.papers_for_reclassify(conn, limit, after_id=after_id)

    last_id = after_id
    for row in rows:
        last_id = row["paper_id"]
        paper = {"title": row["title"]}
        summary = {"body": row["summary_body"]}
        with get_conn() as conn:
            # Re-read the field list each time: earlier papers in this same
            # pass may have just created fields later ones should reuse.
            fields = models.fields_for_classifier(conn)
        try:
            decision = classify(paper, summary, fields, model=config.OLLAMA_MODEL or None,
                                 sensitivity=sensitivity)
        except Exception as e:  # noqa: BLE001 - skip the dud, keep the batch
            logger.warning("Reclassify failed for paper %s: %s", row["paper_id"], e)
            continue

        with get_conn() as conn:
            created_new = False
            if "field_slug" in decision:
                field = models.get_field_by_slug(conn, decision["field_slug"])
                field_id, field_slug, field_name = field["id"], field["slug"], field["name"]
            else:
                created_new = True
                nf = decision["new_field"]
                field_id = models.create_field(
                    conn, nf["slug"], nf["name"], nf["description"], is_seed=0,
                    keywords=nf.get("keywords"),
                )
                field_slug, field_name = nf["slug"], nf["name"]
                logger.info("Created new field: %s", nf["name"])

            models.clear_paper_fields(conn, row["paper_id"])
            models.link_paper_field(conn, row["paper_id"], field_id, is_primary=1)
            secondary_ids, secondary_slugs = [], []
            for sslug in decision.get("secondary_slugs", []):
                sf = models.get_field_by_slug(conn, sslug)
                if sf and sf["id"] != field_id:
                    models.link_paper_field(conn, row["paper_id"], sf["id"], is_primary=0)
                    secondary_ids.append(sf["id"])
                    secondary_slugs.append(sslug)
            models.log_ingest(
                conn, "reclassify_primary_fields", None, "reclassified",
                detail=f"paper_id={row['paper_id']}, "
                       f"old_primary={row['primary_slug']}, new_field_id={field_id}, "
                       f"created_new={created_new}, secondary={secondary_ids}",
            )
        if on_paper:
            on_paper({
                "paper_id": row["paper_id"],
                "title": row["title"],
                "old_primary_slug": row["primary_slug"],
                "new_field_slug": field_slug,
                "new_field_name": field_name,
                "created_new": created_new,
                "secondary_slugs": secondary_slugs,
            })
    return len(rows), last_id


def backfill_field_keywords(limit=50, after_id=0, force=False):
    """Generate search keywords for fields. By default only fields that lack
    them (seed fields, older classifier-created fields); ``force=True``
    recomputes every field's keywords from scratch. Returns
    (updated_count, last_field_id) so a long-running backfill can page
    forward via ``after_id``.
    """
    with get_conn() as conn:
        rows = models.fields_missing_keywords(conn, limit, after_id=after_id, force=force)
    updated = 0
    last_id = after_id
    for row in rows:
        last_id = row["id"]
        try:
            keywords = generate_keywords(row["name"], row.get("description") or "")
        except Exception as e:  # noqa: BLE001 - skip the dud, keep the batch
            logger.warning("Keyword generation failed for field %s: %s",
                           row["slug"], e)
            continue
        with get_conn() as conn:
            models.set_field_keywords(conn, row["id"], keywords)
        updated += 1
    return updated, last_id


def ingest_seed_fields(limit=20, min_citations=0, only_slug=None, on_progress=None,
                        fast=False, defer_enrich=False):
    """Run ingestion for all seeded fields (or one specific slug).

    Progress is reported cumulatively across fields so a single status card can
    track a multi-field run. ``fast``/``defer_enrich`` are forwarded to
    ``ingest_query`` per field.
    """
    with get_conn() as conn:
        fields = [f for f in models.list_fields(conn)]
    totals = {"fetched": 0, "processed": 0, "skipped": 0, "ingested": 0, "errors": 0}

    def relay(counts, current, base):
        merged = {k: base[k] + counts.get(k, 0) for k in totals}
        if on_progress:
            on_progress(merged, current)

    for field in fields:
        if only_slug and field["slug"] != only_slug:
            continue
        with get_conn() as conn:
            full = models.get_field_by_slug(conn, field["slug"])
        query = full.get("query")
        if not query:
            continue
        logger.info("=== Ingesting field '%s' (query: %s) ===", field["name"], query)
        base = dict(totals)
        counts = ingest_query(
            query, limit=limit, min_citations=min_citations,
            on_progress=lambda c, cur: relay(c, cur, base), fast=fast,
            defer_enrich=defer_enrich,
        )
        for k in totals:
            totals[k] += counts.get(k, 0)
        logger.info("Field '%s' done: %s", field["name"], counts)
    return totals
