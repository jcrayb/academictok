"""SQLite connection helpers and schema initialization."""
import datetime as dt
import sqlite3
from contextlib import contextmanager

import config


def now_iso() -> str:
    """Current UTC timestamp as an ISO-8601 string."""
    return dt.datetime.now(dt.timezone.utc).isoformat()

SCHEMA = """
-- A research field ("subreddit").
CREATE TABLE IF NOT EXISTS fields (
    id          INTEGER PRIMARY KEY,
    slug        TEXT UNIQUE NOT NULL,
    name        TEXT NOT NULL,
    description TEXT,
    query       TEXT,
    keywords    TEXT, -- comma-separated search keywords (LLM-generated), used by /api/fields/search
    is_seed     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

-- A paper (one row per unique Semantic Scholar paper).
CREATE TABLE IF NOT EXISTS papers (
    id             INTEGER PRIMARY KEY,
    s2_paper_id    TEXT UNIQUE NOT NULL,
    title          TEXT NOT NULL,
    abstract       TEXT,
    authors        TEXT,
    year           INTEGER,
    venue          TEXT,
    url            TEXT,
    citation_count INTEGER,
    pdf_url        TEXT,
    fetched_at     TEXT NOT NULL
);

-- Figures extracted from a paper's open-access PDF.
CREATE TABLE IF NOT EXISTS paper_images (
    id         INTEGER PRIMARY KEY,
    paper_id   INTEGER NOT NULL REFERENCES papers(id),
    filename   TEXT NOT NULL,
    page       INTEGER,
    width      INTEGER,
    height     INTEGER,
    caption    TEXT,
    ord        INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

-- LLM-generated summary post for a paper.
CREATE TABLE IF NOT EXISTS summaries (
    id         INTEGER PRIMARY KEY,
    paper_id   INTEGER NOT NULL UNIQUE REFERENCES papers(id),
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    model      TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Which field(s) a paper belongs to (LLM-assigned). A paper has exactly one
-- primary ("home") field plus any number of secondary fields it also fits;
-- is_primary = 1 marks the home field, which is what cards are labeled with.
CREATE TABLE IF NOT EXISTS paper_fields (
    paper_id   INTEGER NOT NULL REFERENCES papers(id),
    field_id   INTEGER NOT NULL REFERENCES fields(id),
    is_primary INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (paper_id, field_id)
);

-- Tracks processed queries / papers to avoid redundant work.
CREATE TABLE IF NOT EXISTS ingest_log (
    id          INTEGER PRIMARY KEY,
    query       TEXT NOT NULL,
    s2_paper_id TEXT,
    status      TEXT NOT NULL,
    detail      TEXT,
    created_at  TEXT NOT NULL
);

-- A user, keyed by Firebase UID. Provisioned on first verified login.
CREATE TABLE IF NOT EXISTS users (
    uid        TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    last_seen  TEXT
);

-- Per-user field subscriptions (server-side).
CREATE TABLE IF NOT EXISTS subscriptions (
    uid        TEXT NOT NULL REFERENCES users(uid),
    field_id   INTEGER NOT NULL REFERENCES fields(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (uid, field_id)
);

-- Papers a user has already scrolled past, so the main feed can prioritize new
-- content and field pages can optionally hide them.
CREATE TABLE IF NOT EXISTS seen_papers (
    uid       TEXT NOT NULL REFERENCES users(uid),
    paper_id  INTEGER NOT NULL REFERENCES papers(id),
    seen_at   TEXT NOT NULL,
    PRIMARY KEY (uid, paper_id)
);

CREATE INDEX IF NOT EXISTS idx_paper_fields_field ON paper_fields(field_id);
CREATE INDEX IF NOT EXISTS idx_papers_s2 ON papers(s2_paper_id);
CREATE INDEX IF NOT EXISTS idx_summaries_paper ON summaries(paper_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_uid ON subscriptions(uid);
CREATE INDEX IF NOT EXISTS idx_paper_images_paper ON paper_images(paper_id);
CREATE INDEX IF NOT EXISTS idx_seen_uid ON seen_papers(uid);
"""


def connect() -> sqlite3.Connection:
    """Open a connection with row access by name and FK enforcement."""
    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_conn():
    """Context-managed connection that commits on success, rolls back on error."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create tables/indexes if they don't exist, then run light migrations."""
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn) -> None:
    """Additive, idempotent column migrations for existing databases."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(summaries)")}
    if "long_body" not in cols:
        # Thorough, on-demand detailed summary (nullable; backfilled lazily).
        conn.execute("ALTER TABLE summaries ADD COLUMN long_body TEXT")
    if "long_source" not in cols:
        # Provenance of long_body: 'abstract' or 'fulltext'. Lets the on-view
        # backfill upgrade an abstract-only summary to a full-text-grounded one
        # the first time the open-access PDF is reachable. NULL = pre-migration.
        conn.execute("ALTER TABLE summaries ADD COLUMN long_source TEXT")
    if "needs_full_summary" not in cols:
        # 1 = the short summary was written by the fast discovery model and is
        # queued for re-summarization by the full model (scripts/upgrade_summaries).
        conn.execute(
            "ALTER TABLE summaries ADD COLUMN needs_full_summary INTEGER NOT NULL DEFAULT 0"
        )
    if "enriched_at" not in cols:
        # When the detailed summary + figures were last attempted. Lets the
        # scheduled precompute pass try each paper once (so papers with no
        # reachable PDF / no figures don't get retried forever). NULL = never.
        conn.execute("ALTER TABLE summaries ADD COLUMN enriched_at TEXT")
    if "updated_at" not in cols:
        # Bumped on every in-place edit (re-summarize, enrich, long-summary
        # backfill), so scripts/sync_export.py can pick up changes to a summary
        # that was already synced, not just brand-new ones.
        conn.execute("ALTER TABLE summaries ADD COLUMN updated_at TEXT")
        conn.execute("UPDATE summaries SET updated_at = created_at WHERE updated_at IS NULL")

    paper_cols = {r["name"] for r in conn.execute("PRAGMA table_info(papers)")}
    if "pdf_url" not in paper_cols:
        # Open-access PDF URL from Semantic Scholar (nullable).
        conn.execute("ALTER TABLE papers ADD COLUMN pdf_url TEXT")

    pf_cols = {r["name"] for r in conn.execute("PRAGMA table_info(paper_fields)")}
    if "is_primary" not in pf_cols:
        # A paper can now belong to several fields (one primary + secondaries).
        # Cross-field feeds label each card with its primary field, so backfill
        # every existing paper's single link as primary to keep feeds populated.
        conn.execute(
            "ALTER TABLE paper_fields ADD COLUMN is_primary INTEGER NOT NULL DEFAULT 0"
        )
        conn.execute(
            """UPDATE paper_fields SET is_primary = 1
               WHERE rowid IN (SELECT MIN(rowid) FROM paper_fields GROUP BY paper_id)"""
        )

    field_cols = {r["name"] for r in conn.execute("PRAGMA table_info(fields)")}
    if "discover_offset" not in field_cols:
        # How far on-the-fly field discovery has paged into the Semantic Scholar
        # results, so successive infinite-scroll calls keep finding fresh papers.
        conn.execute(
            "ALTER TABLE fields ADD COLUMN discover_offset INTEGER NOT NULL DEFAULT 0"
        )
    if "keywords" not in field_cols:
        # Comma-separated search keywords (LLM-generated for new fields; backfilled
        # for existing ones via scripts/backfill_field_keywords.py). Powers the
        # field search bar.
        conn.execute("ALTER TABLE fields ADD COLUMN keywords TEXT")
    if "updated_at" not in field_cols:
        # Bumped on every in-place edit (keywords, discover_offset), so
        # scripts/sync_export.py can pick up changes to fields that were already
        # synced, not just brand-new ones.
        conn.execute("ALTER TABLE fields ADD COLUMN updated_at TEXT")
        conn.execute("UPDATE fields SET updated_at = created_at WHERE updated_at IS NULL")

    pf2_cols = {r["name"] for r in conn.execute("PRAGMA table_info(paper_fields)")}
    if "created_at" not in pf2_cols:
        # Lets scripts/sync_export.py pick up secondary-field links added to an
        # already-synced paper, not just links created alongside a new paper.
        conn.execute("ALTER TABLE paper_fields ADD COLUMN created_at TEXT")
        conn.execute(
            """UPDATE paper_fields SET created_at = (
                   SELECT fetched_at FROM papers WHERE papers.id = paper_fields.paper_id
               ) WHERE created_at IS NULL"""
        )


if __name__ == "__main__":
    init_db()
    print(f"Initialized database at {config.DATABASE_PATH}")
