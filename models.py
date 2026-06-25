"""Lightweight data-access functions over SQLite.

Each function takes an open connection so callers control the transaction.
"""
import json

from db import now_iso


# ── Fields ──────────────────────────────────────────────────────────────────

def _field_row(r):
    d = dict(r)
    raw = d.pop("keywords", None)
    d["keywords"] = [k.strip() for k in raw.split(",") if k.strip()] if raw else []
    return d


def list_fields(conn):
    rows = conn.execute(
        """SELECT f.id, f.slug, f.name, f.description, f.keywords, f.is_seed,
                  COUNT(pf.paper_id) AS paper_count
           FROM fields f
           LEFT JOIN paper_fields pf ON pf.field_id = f.id
           GROUP BY f.id
           ORDER BY f.name"""
    ).fetchall()
    return [_field_row(r) for r in rows]


def search_fields(conn, query):
    """Fields whose name, description, or keywords match the query (substring,
    case-insensitive). Every query word must match somewhere (AND), so adding
    more words narrows the results rather than broadening them."""
    words = [w.strip() for w in query.lower().split() if w.strip()]
    if not words:
        return list_fields(conn)
    rows = conn.execute(
        """SELECT f.id, f.slug, f.name, f.description, f.keywords, f.is_seed,
                  COUNT(pf.paper_id) AS paper_count
           FROM fields f
           LEFT JOIN paper_fields pf ON pf.field_id = f.id
           GROUP BY f.id
           ORDER BY f.name"""
    ).fetchall()
    fields = [_field_row(r) for r in rows]
    matches = []
    for f in fields:
        haystack = " ".join([
            f["name"].lower(), (f["description"] or "").lower(),
            " ".join(k.lower() for k in f["keywords"]),
        ])
        if all(w in haystack for w in words):
            matches.append(f)
    return matches


def get_field_by_slug(conn, slug):
    row = conn.execute("SELECT * FROM fields WHERE slug = ?", (slug,)).fetchone()
    return dict(row) if row else None


def set_field_discover_offset(conn, field_id, offset):
    """Persist how far on-the-fly discovery has paged into S2 for this field."""
    conn.execute(
        "UPDATE fields SET discover_offset = ?, updated_at = ? WHERE id = ?",
        (offset, now_iso(), field_id),
    )


def fields_for_classifier(conn):
    """Field list (slug/name/description/keywords) for the LLM classifier.
    Keywords are included so the prompt can be narrowed to the most relevant
    fields when the full list would be too large (see classifier._select_relevant)."""
    rows = conn.execute(
        "SELECT slug, name, description, keywords FROM fields ORDER BY name"
    ).fetchall()
    return [_field_row(r) for r in rows]


def create_field(conn, slug, name, description, query=None, is_seed=0, keywords=None):
    """Insert a field if absent; return its id. ``keywords`` is a list of search
    terms, stored comma-separated."""
    kw = ",".join(keywords) if keywords else None
    ts = now_iso()
    conn.execute(
        """INSERT OR IGNORE INTO fields
           (slug, name, description, query, keywords, is_seed, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (slug, name, description, query, kw, is_seed, ts, ts),
    )
    row = conn.execute("SELECT id FROM fields WHERE slug = ?", (slug,)).fetchone()
    return row["id"]


def set_field_keywords(conn, field_id, keywords):
    conn.execute(
        "UPDATE fields SET keywords = ?, updated_at = ? WHERE id = ?",
        (",".join(keywords) if keywords else None, now_iso(), field_id),
    )


def fields_missing_keywords(conn, limit, after_id=0, force=False):
    """Fields to (re)generate keywords for, oldest-first with a backfill cursor
    via ``after_id`` (field id) so a forced full recompute can page through
    every field exactly once. By default only fields that lack keywords;
    ``force=True`` returns every field (used to recompute existing ones too).
    """
    clause = "" if force else "AND (keywords IS NULL OR keywords = '')"
    rows = conn.execute(
        f"""SELECT id, slug, name, description FROM fields
            WHERE id > ? {clause}
            ORDER BY id LIMIT ?""",
        (after_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ── Papers / summaries ──────────────────────────────────────────────────────

def paper_exists(conn, s2_paper_id):
    return conn.execute(
        "SELECT 1 FROM papers WHERE s2_paper_id = ?", (s2_paper_id,)
    ).fetchone() is not None


def insert_paper(conn, paper):
    cur = conn.execute(
        """INSERT INTO papers
           (s2_paper_id, title, abstract, authors, year, venue, url,
            citation_count, pdf_url, fetched_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            paper["s2_paper_id"], paper["title"], paper["abstract"],
            json.dumps(paper["authors"]), paper["year"], paper["venue"],
            paper["url"], paper["citation_count"], paper.get("pdf_url"), now_iso(),
        ),
    )
    return cur.lastrowid


def insert_paper_image(conn, paper_id, img):
    """Persist one extracted figure (img: filename, page, width, height, ord)."""
    conn.execute(
        """INSERT INTO paper_images
           (paper_id, filename, page, width, height, caption, ord, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (paper_id, img["filename"], img.get("page"), img.get("width"),
         img.get("height"), img.get("caption"), img.get("ord", 0), now_iso()),
    )


def get_paper_images(conn, paper_id):
    rows = conn.execute(
        """SELECT filename, page, width, height, caption, ord
           FROM paper_images WHERE paper_id = ? ORDER BY ord""",
        (paper_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def insert_summary(conn, paper_id, summary):
    ts = now_iso()
    conn.execute(
        """INSERT OR REPLACE INTO summaries
           (paper_id, title, body, long_body, long_source, model,
            needs_full_summary, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (paper_id, summary["title"], summary["body"], summary.get("long_body"),
         summary.get("long_source"), summary["model"],
         summary.get("needs_full_summary", 0), ts, ts),
    )


def pending_full_summaries(conn, limit):
    """Discovery summaries (fast model) awaiting re-generation by the full model."""
    rows = conn.execute(
        """SELECT s.paper_id, p.title, p.abstract, p.venue, p.year
           FROM summaries s JOIN papers p ON p.id = s.paper_id
           WHERE s.needs_full_summary = 1
           ORDER BY s.paper_id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def count_pending_full_summaries(conn):
    """How many discovery summaries are still awaiting the full-model upgrade."""
    return conn.execute(
        "SELECT COUNT(*) AS c FROM summaries WHERE needs_full_summary = 1"
    ).fetchone()["c"]


def update_short_summary(conn, paper_id, title, body, model):
    """Replace a paper's short summary and clear its upgrade flag."""
    conn.execute(
        """UPDATE summaries SET title = ?, body = ?, model = ?,
                  needs_full_summary = 0, updated_at = ? WHERE paper_id = ?""",
        (title, body, model, now_iso(), paper_id),
    )


def paper_enrichment_state(conn, paper_id):
    """Everything enrich_paper needs to (re)generate a detailed summary + figures."""
    p = conn.execute(
        "SELECT s2_paper_id, title, abstract, venue, year, pdf_url FROM papers WHERE id = ?",
        (paper_id,),
    ).fetchone()
    if not p:
        return None
    s = conn.execute(
        "SELECT long_body, long_source FROM summaries WHERE paper_id = ?", (paper_id,)
    ).fetchone()
    n_img = conn.execute(
        "SELECT COUNT(*) AS c FROM paper_images WHERE paper_id = ?", (paper_id,)
    ).fetchone()["c"]
    fields = conn.execute(
        """SELECT f.name FROM paper_fields pf JOIN fields f ON f.id = pf.field_id
           WHERE pf.paper_id = ?""",
        (paper_id,),
    ).fetchall()
    return {
        **dict(p),
        "long_body": s["long_body"] if s else None,
        "long_source": s["long_source"] if s else None,
        "has_images": n_img > 0,
        "field_names": [r["name"] for r in fields],
    }


def mark_enriched(conn, paper_id):
    """Stamp when a paper's detailed summary + figures were last attempted, so
    the scheduled precompute pass tries each paper at most once."""
    ts = now_iso()
    conn.execute(
        "UPDATE summaries SET enriched_at = ?, updated_at = ? WHERE paper_id = ?",
        (ts, ts, paper_id),
    )


def papers_needing_enrichment(conn, limit):
    """Paper ids the scheduled pass hasn't attempted yet that still have something
    to gain (a missing detailed summary, or a PDF that could yield full text /
    figures). First-time (no detailed summary) papers are prioritized."""
    rows = conn.execute(
        """SELECT p.id FROM papers p
           JOIN summaries s ON s.paper_id = p.id
           WHERE s.enriched_at IS NULL
             AND (s.long_body IS NULL OR p.pdf_url IS NOT NULL)
           ORDER BY (s.long_body IS NOT NULL), p.id DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [r["id"] for r in rows]


def count_papers_needing_enrichment(conn):
    """How many papers the scheduled enrichment pass still hasn't attempted."""
    return conn.execute(
        """SELECT COUNT(*) AS c FROM papers p
           JOIN summaries s ON s.paper_id = p.id
           WHERE s.enriched_at IS NULL
             AND (s.long_body IS NULL OR p.pdf_url IS NOT NULL)"""
    ).fetchone()["c"]


def set_long_summary(conn, paper_id, long_body, source=None):
    conn.execute(
        "UPDATE summaries SET long_body = ?, long_source = ?, updated_at = ? WHERE paper_id = ?",
        (long_body, source, now_iso(), paper_id),
    )


def get_paper_raw(conn, paper_id):
    """Raw paper fields needed to (re)generate a summary."""
    row = conn.execute(
        "SELECT id, title, abstract, venue, year FROM papers WHERE id = ?",
        (paper_id,),
    ).fetchone()
    return dict(row) if row else None


def link_paper_field(conn, paper_id, field_id, is_primary=0):
    """Link a paper to a field. ``is_primary=1`` marks its home field (the one
    cards are labeled with); secondary fields it also fits use the default 0."""
    conn.execute(
        """INSERT OR IGNORE INTO paper_fields (paper_id, field_id, is_primary, created_at)
           VALUES (?, ?, ?, ?)""",
        (paper_id, field_id, is_primary, now_iso()),
    )


def fields_for_paper(conn, paper_id):
    """All fields a paper belongs to, primary (home) field first."""
    rows = conn.execute(
        """SELECT f.slug, f.name, pf.is_primary FROM paper_fields pf
           JOIN fields f ON f.id = pf.field_id
           WHERE pf.paper_id = ? ORDER BY pf.is_primary DESC, f.name""",
        (paper_id,),
    ).fetchall()
    return [
        {"slug": r["slug"], "name": r["name"], "is_primary": bool(r["is_primary"])}
        for r in rows
    ]


def papers_missing_secondary_fields(conn, limit, after_id=0, force=False):
    """Papers with a primary field to (re)compute secondary fields for,
    oldest-first with a backfill cursor via ``after_id`` (paper_id) so a long
    run can resume. By default only papers with no secondary fields yet;
    ``force=True`` returns every paper (used to recompute existing ones too).
    """
    exclude = (
        "" if force else
        "AND p.id NOT IN (SELECT paper_id FROM paper_fields WHERE is_primary = 0)"
    )
    rows = conn.execute(
        f"""SELECT p.id AS paper_id, p.title, s.body AS summary_body,
                   f.slug AS primary_slug, f.name AS primary_name
            FROM papers p
            JOIN paper_fields pf ON pf.paper_id = p.id AND pf.is_primary = 1
            JOIN fields f ON f.id = pf.field_id
            JOIN summaries s ON s.paper_id = p.id
            WHERE p.id > ?
            {exclude}
            ORDER BY p.id
            LIMIT ?""",
        (after_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def clear_secondary_fields(conn, paper_id):
    """Remove a paper's existing secondary field links (keeps the primary),
    so they can be recomputed from scratch."""
    conn.execute(
        "DELETE FROM paper_fields WHERE paper_id = ? AND is_primary = 0",
        (paper_id,),
    )


def papers_for_reclassify(conn, limit, after_id=0, field_slugs=None):
    """Papers (oldest-first, paginated via ``after_id``) to run the full
    primary-field classifier on again, along with their current primary field
    for logging. Used by the field-reclassification script.

    ``field_slugs``, if given, restricts to papers whose current primary
    field's slug is in that list.
    """
    params = [after_id]
    field_filter = ""
    if field_slugs:
        placeholders = ", ".join("?" for _ in field_slugs)
        field_filter = f" AND f.slug IN ({placeholders})"
        params.extend(field_slugs)
    rows = conn.execute(
        f"""SELECT p.id AS paper_id, p.title, s.body AS summary_body,
                  f.slug AS primary_slug, f.name AS primary_name
           FROM papers p
           JOIN summaries s ON s.paper_id = p.id
           LEFT JOIN paper_fields pf ON pf.paper_id = p.id AND pf.is_primary = 1
           LEFT JOIN fields f ON f.id = pf.field_id
           WHERE p.id > ?{field_filter}
           ORDER BY p.id
           LIMIT ?""",
        (*params, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def clear_paper_fields(conn, paper_id):
    """Remove ALL of a paper's field links (primary and secondary), so a fresh
    primary + secondary assignment can be written in its place."""
    conn.execute("DELETE FROM paper_fields WHERE paper_id = ?", (paper_id,))


def log_ingest(conn, query, s2_paper_id, status, detail=""):
    conn.execute(
        """INSERT INTO ingest_log (query, s2_paper_id, status, detail, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (query, s2_paper_id, status, detail, now_iso()),
    )


# ── Feed queries ────────────────────────────────────────────────────────────

# One row per paper, labeled with its primary (home) field. A paper may belong
# to several fields, but joining only the primary keeps cross-field feeds from
# emitting the same paper once per field. Field-membership filtering is done
# separately (via a paper_fields subquery) so a paper still surfaces on any of
# its fields' pages.
_FEED_SELECT = """
SELECT p.id AS paper_id, p.s2_paper_id, p.title AS paper_title, p.authors,
       p.year, p.venue, p.url, p.citation_count,
       s.title AS summary_title, s.body AS summary_body,
       f.slug AS field_slug, f.name AS field_name
FROM papers p
JOIN summaries s ON s.paper_id = p.id
JOIN paper_fields pf ON pf.paper_id = p.id AND pf.is_primary = 1
JOIN fields f ON f.id = pf.field_id
"""

# Membership test reused by the field-filtered feeds: a paper "is in" a field
# if it's linked to it as primary OR secondary.
def _in_fields_clause(field_ids):
    placeholders = ",".join("?" * len(field_ids))
    return f"p.id IN (SELECT paper_id FROM paper_fields WHERE field_id IN ({placeholders}))"


def _feed_select_for_fields(field_ids):
    """Like ``_FEED_SELECT``, but labels each paper with whichever of the given
    fields it matched on (its primary field if that's one of them, else any
    matching secondary field) instead of always its primary field.

    Used by feeds scoped to a specific field set — the subscribed feed and a
    single field's browse page — so a card shows the field the user actually
    asked for (e.g. r/stochastic-optimization) even when that's only a
    secondary field for that paper, rather than always its unrelated primary.
    """
    placeholders = ",".join("?" * len(field_ids))
    return f"""
SELECT p.id AS paper_id, p.s2_paper_id, p.title AS paper_title, p.authors,
       p.year, p.venue, p.url, p.citation_count,
       s.title AS summary_title, s.body AS summary_body,
       f.slug AS field_slug, f.name AS field_name
FROM papers p
JOIN summaries s ON s.paper_id = p.id
JOIN (
    SELECT paper_id, field_id FROM (
        SELECT paper_id, field_id,
               ROW_NUMBER() OVER (
                   PARTITION BY paper_id ORDER BY is_primary DESC, field_id
               ) AS rn
        FROM paper_fields
        WHERE field_id IN ({placeholders})
    ) WHERE rn = 1
) pf ON pf.paper_id = p.id
JOIN fields f ON f.id = pf.field_id
"""


def _feed_row(r):
    d = dict(r)
    d["authors"] = json.loads(d["authors"]) if d.get("authors") else []
    d["field"] = {"slug": d.pop("field_slug"), "name": d.pop("field_name")}
    return d


def _unseen_join(unseen_uid, params):
    """Append the seen-papers anti-join when filtering to unseen papers.

    Returns (join_sql, where_extra). The uid param is prepended to ``params`` in
    place so it lines up with the join's placeholder (which precedes WHERE).
    """
    if not unseen_uid:
        return "", ""
    params.append(unseen_uid)
    return ("LEFT JOIN seen_papers sp ON sp.paper_id = p.id AND sp.uid = ?",
            " AND sp.paper_id IS NULL")


def feed_for_fields(conn, field_ids, limit, offset=0, sort="random", seed=0,
                     unseen_uid=None):
    """Offset-paginated feed for a set of field ids, sorted by citation / year /
    random (default) — same sort modes as a single field's browse page, so
    interleaving subscribed fields doesn't bias toward whichever field was
    ingested most recently (random shuffles them; citation/year order by
    paper, not by ingest batch).

    ``seed`` makes the random order reproducible across pages within a
    browsing session. When ``unseen_uid`` is given, papers that user has
    already seen are excluded.
    """
    if not field_ids:
        return []
    order = _FEED_SORTS.get(sort, _FEED_SORTS["random"])
    select = _feed_select_for_fields(field_ids)
    params = list(field_ids)
    join, unseen_where = _unseen_join(unseen_uid, params)
    where = f"WHERE 1=1{unseen_where}"
    if sort == "random":
        params.append(seed)  # lines up with the ? inside the ORDER BY expression
    params.extend([limit, offset])
    rows = conn.execute(
        f"{select} {join} {where} ORDER BY {order} LIMIT ? OFFSET ?", params
    ).fetchall()
    return [_feed_row(r) for r in rows]


def feed_for_paper_ids(conn, paper_ids):
    """Feed rows for specific paper ids (newest first). Used to return the papers
    that on-the-fly discovery just inserted, in the same shape as the feed."""
    if not paper_ids:
        return []
    placeholders = ",".join("?" * len(paper_ids))
    rows = conn.execute(
        f"{_FEED_SELECT} WHERE p.id IN ({placeholders}) ORDER BY p.id DESC",
        list(paper_ids),
    ).fetchall()
    return [_feed_row(r) for r in rows]


# Shared sort modes for any offset-paginated feed (a single field's browse
# page, the main discover feed, the main subscribed feed). "random" is a
# deterministic shuffle seeded per session (?) so OFFSET pagination stays
# stable as you scroll.
_FEED_SORTS = {
    "citation": "p.citation_count DESC, p.id DESC",
    "year": "p.year DESC, p.id DESC",
    "random": "((p.id + ?) * 2654435761) % 2147483647, p.id DESC",
}


def feed_for_field_sorted(conn, field_id, limit, offset=0, sort="random",
                          seed=0, unseen_uid=None):
    """Offset-paginated feed for one field, sorted by citation / year / random.

    ``unseen_uid`` excludes that user's already-seen papers. ``seed`` makes the
    random order reproducible across pages within a browsing session.
    """
    return feed_for_fields(conn, [field_id], limit, offset=offset, sort=sort,
                            seed=seed, unseen_uid=unseen_uid)


def feed_all(conn, limit, offset=0, sort="random", seed=0,
             exclude_field_ids=None, unseen_uid=None):
    """Offset-paginated feed across all fields, sorted by citation / year /
    random (default) — same sort modes as a field's browse page, so the
    discover pool doesn't congregate papers by ingest order (which a plain
    newest-first feed effectively does, since a whole field tends to get
    ingested in one batch). Optionally excludes some fields (for discovery mix).

    ``seed`` makes the random order reproducible across pages within a
    browsing session. When ``unseen_uid`` is given, papers that user has
    already seen are excluded.
    """
    order = _FEED_SORTS.get(sort, _FEED_SORTS["random"])
    params = []
    join, unseen_where = _unseen_join(unseen_uid, params)
    where = "WHERE 1=1" + unseen_where
    if exclude_field_ids:
        # Exclude papers in any excluded field (primary or secondary), so a
        # multi-field paper already shown via the subscribed pool isn't repeated.
        where += f" AND NOT {_in_fields_clause(exclude_field_ids)}"
        params.extend(exclude_field_ids)
    if sort == "random":
        params.append(seed)  # lines up with the ? inside the ORDER BY expression
    params.extend([limit, offset])
    rows = conn.execute(
        f"{_FEED_SELECT} {join} {where} ORDER BY {order} LIMIT ? OFFSET ?", params
    ).fetchall()
    return [_feed_row(r) for r in rows]


def get_paper_detail(conn, s2_paper_id):
    row = conn.execute(
        f"{_FEED_SELECT} WHERE p.s2_paper_id = ?", (s2_paper_id,)
    ).fetchone()
    if not row:
        return None
    base = _feed_row(row)
    base.pop("field", None)
    # A paper may map to multiple fields; collect them all (primary first).
    base["fields"] = fields_for_paper(conn, base["paper_id"])
    # Long detailed summary (may be null until backfilled). long_source records
    # whether it was grounded in full text, so the view path can upgrade it.
    long_row = conn.execute(
        "SELECT long_body, long_source FROM summaries WHERE paper_id = ?",
        (base["paper_id"],),
    ).fetchone()
    base["long_body"] = long_row["long_body"] if long_row else None
    base["long_source"] = long_row["long_source"] if long_row else None
    prow = conn.execute(
        "SELECT abstract, pdf_url FROM papers WHERE id = ?", (base["paper_id"],)
    ).fetchone()
    base["abstract"] = prow["abstract"]
    base["pdf_url"] = prow["pdf_url"]
    # Figures extracted from the PDF, with a web path for the frontend.
    base["images"] = [
        {**img, "url": f"/media/figures/{img['filename']}"}
        for img in get_paper_images(conn, base["paper_id"])
    ]
    return base


_SEARCH_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from",
    "has", "have", "if", "in", "into", "is", "it", "its", "of", "on", "or",
    "that", "the", "their", "this", "to", "via", "was", "were", "with",
}


def search_papers(conn, query, limit=40):
    """Papers whose title matches every significant word of ``query`` (substring,
    case-insensitive, AND'd together like ``search_fields``). Common words (the,
    a, an, ...) are skipped so they don't force a match. Returns feed items
    (same shape as a field feed), labeled with each paper's primary field so
    the client can link to it."""
    words = [w for w in query.lower().split() if w.strip() and w not in _SEARCH_STOPWORDS]
    if not words:
        return []
    where = " AND ".join("LOWER(p.title) LIKE ?" for _ in words)
    params = [f"%{w}%" for w in words] + [limit]
    rows = conn.execute(
        f"{_FEED_SELECT} WHERE {where} ORDER BY p.title LIMIT ?", params
    ).fetchall()
    return [_feed_row(r) for r in rows]


# ── Users / subscriptions ───────────────────────────────────────────────────

def ensure_user(conn, uid):
    conn.execute(
        "INSERT OR IGNORE INTO users (uid, created_at) VALUES (?, ?)",
        (uid, now_iso()),
    )
    conn.execute("UPDATE users SET last_seen = ? WHERE uid = ?", (now_iso(), uid))


def user_exists(conn, uid):
    return conn.execute(
        "SELECT 1 FROM users WHERE uid = ?", (uid,)
    ).fetchone() is not None


def mark_papers_seen(conn, uid, paper_ids):
    """Record that a user has scrolled past these papers (idempotent)."""
    if not paper_ids:
        return
    ts = now_iso()
    conn.executemany(
        "INSERT OR IGNORE INTO seen_papers (uid, paper_id, seen_at) VALUES (?, ?, ?)",
        [(uid, pid, ts) for pid in paper_ids],
    )


def reset_seen(conn, uid=None):
    """Clear seen-paper history so the feed treats papers as unseen again.
    Clears every user's history, or just one user's when ``uid`` is given.
    Returns the number of rows removed."""
    if uid:
        cur = conn.execute("DELETE FROM seen_papers WHERE uid = ?", (uid,))
    else:
        cur = conn.execute("DELETE FROM seen_papers")
    return cur.rowcount


def list_subscriptions(conn, uid):
    rows = conn.execute(
        """SELECT f.slug, f.name, f.description
           FROM subscriptions s JOIN fields f ON f.id = s.field_id
           WHERE s.uid = ? ORDER BY f.name""",
        (uid,),
    ).fetchall()
    return [dict(r) for r in rows]


def subscribed_field_ids(conn, uid):
    rows = conn.execute(
        "SELECT field_id FROM subscriptions WHERE uid = ?", (uid,)
    ).fetchall()
    return [r["field_id"] for r in rows]


def subscribe(conn, uid, slug):
    field = get_field_by_slug(conn, slug)
    if not field:
        return False
    conn.execute(
        """INSERT OR IGNORE INTO subscriptions (uid, field_id, created_at)
           VALUES (?, ?, ?)""",
        (uid, field["id"], now_iso()),
    )
    return True


def unsubscribe(conn, uid, slug):
    field = get_field_by_slug(conn, slug)
    if not field:
        return False
    conn.execute(
        "DELETE FROM subscriptions WHERE uid = ? AND field_id = ?",
        (uid, field["id"]),
    )
    return True


# ── Collections ──────────────────────────────────────────────────────────────

DEFAULT_COLLECTION_NAME = "Liked Papers"


def get_or_create_default_collection(conn, uid):
    """Every user has exactly one is_default=1 collection ("Liked Papers"),
    created lazily on first use. Liking a paper is just saving it here."""
    row = conn.execute(
        "SELECT id FROM collections WHERE uid = ? AND is_default = 1", (uid,)
    ).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO collections (uid, name, is_default, created_at) VALUES (?, ?, 1, ?)",
        (uid, DEFAULT_COLLECTION_NAME, now_iso()),
    )
    return cur.lastrowid


def create_collection(conn, uid, name):
    cur = conn.execute(
        "INSERT INTO collections (uid, name, created_at) VALUES (?, ?, ?)",
        (uid, name, now_iso()),
    )
    return cur.lastrowid


def list_collections(conn, uid, limit=None):
    """A user's collections, default ("Liked Papers") first then newest
    first, with how many papers each holds. ``limit``, if given, caps the
    result (e.g. for a navbar menu preview)."""
    get_or_create_default_collection(conn, uid)
    sql = """SELECT c.id, c.name, c.is_default, c.created_at,
                    COUNT(cp.paper_id) AS paper_count
              FROM collections c
              LEFT JOIN collection_papers cp ON cp.collection_id = c.id
              WHERE c.uid = ?
              GROUP BY c.id
              ORDER BY c.is_default DESC, c.created_at DESC"""
    params = [uid]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [{**dict(r), "is_default": bool(r["is_default"])} for r in rows]


def get_collection(conn, uid, collection_id):
    """A collection, only if it belongs to ``uid`` (ownership check)."""
    row = conn.execute(
        "SELECT id, uid, name, is_default, created_at FROM collections WHERE id = ? AND uid = ?",
        (collection_id, uid),
    ).fetchone()
    if not row:
        return None
    return {**dict(row), "is_default": bool(row["is_default"])}


def add_to_collection(conn, uid, collection_id, paper_id):
    if not get_collection(conn, uid, collection_id):
        return False
    conn.execute(
        """INSERT OR IGNORE INTO collection_papers (collection_id, paper_id, created_at)
           VALUES (?, ?, ?)""",
        (collection_id, paper_id, now_iso()),
    )
    return True


def remove_from_collection(conn, uid, collection_id, paper_id):
    if not get_collection(conn, uid, collection_id):
        return False
    conn.execute(
        "DELETE FROM collection_papers WHERE collection_id = ? AND paper_id = ?",
        (collection_id, paper_id),
    )
    return True


def delete_collection(conn, uid, collection_id):
    """Delete a user's collection and its saved-paper rows. Returns "deleted"
    on success, "not_found" if it isn't this user's collection, or "default"
    if it's the protected "Liked Papers" collection (which can't be deleted)."""
    collection = get_collection(conn, uid, collection_id)
    if not collection:
        return "not_found"
    if collection["is_default"]:
        return "default"
    conn.execute(
        "DELETE FROM collection_papers WHERE collection_id = ?", (collection_id,)
    )
    conn.execute(
        "DELETE FROM collections WHERE id = ? AND uid = ?", (collection_id, uid)
    )
    return "deleted"


def list_collection_papers(conn, uid, collection_id):
    """A collection's saved papers (feed-item shape), or None if the
    collection doesn't exist / isn't owned by ``uid``."""
    if not get_collection(conn, uid, collection_id):
        return None
    rows = conn.execute(
        f"""{_FEED_SELECT}
           WHERE p.id IN (SELECT paper_id FROM collection_papers WHERE collection_id = ?)
           ORDER BY p.id DESC""",
        (collection_id,),
    ).fetchall()
    return [_feed_row(r) for r in rows]


def collections_containing_paper(conn, uid, paper_id):
    """Ids of this user's collections that already contain ``paper_id`` —
    used to render the save popover's per-collection added/not-added state."""
    rows = conn.execute(
        """SELECT cp.collection_id FROM collection_papers cp
           JOIN collections c ON c.id = cp.collection_id
           WHERE c.uid = ? AND cp.paper_id = ?""",
        (uid, paper_id),
    ).fetchall()
    return [r["collection_id"] for r in rows]


def saved_paper_ids(conn, uid):
    """Ids of every paper saved in ANY of this user's collections (including
    the default "Liked Papers" one) — for the save/bookmark icon's filled state."""
    rows = conn.execute(
        """SELECT DISTINCT cp.paper_id FROM collection_papers cp
           JOIN collections c ON c.id = cp.collection_id
           WHERE c.uid = ?""",
        (uid,),
    ).fetchall()
    return [r["paper_id"] for r in rows]


# ── Likes ────────────────────────────────────────────────────────────────────
# A "like" is just membership in the default collection — see
# get_or_create_default_collection above.

def like_paper(conn, uid, paper_id):
    collection_id = get_or_create_default_collection(conn, uid)
    add_to_collection(conn, uid, collection_id, paper_id)


def unlike_paper(conn, uid, paper_id):
    collection_id = get_or_create_default_collection(conn, uid)
    remove_from_collection(conn, uid, collection_id, paper_id)


def liked_paper_ids(conn, uid):
    collection_id = get_or_create_default_collection(conn, uid)
    rows = conn.execute(
        "SELECT paper_id FROM collection_papers WHERE collection_id = ?", (collection_id,)
    ).fetchall()
    return [r["paper_id"] for r in rows]


def list_liked_papers(conn, uid):
    collection_id = get_or_create_default_collection(conn, uid)
    return list_collection_papers(conn, uid, collection_id) or []
