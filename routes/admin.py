"""Admin-only ingestion dashboard API.

All routes require an allow-listed admin email (see auth.require_admin).
Ingestion runs in a background thread so the request returns immediately; the
frontend polls the job status endpoint.
"""
import logging
import threading
import uuid

from flask import Blueprint, g, jsonify, request

import config
import models
from auth import require_admin
from db import get_conn, now_iso
from ingest import pipeline

logger = logging.getLogger(__name__)

bp = Blueprint("admin", __name__)

# In-memory job registry. Fine for the single-process dev server; a real
# deployment would use a durable queue.
_jobs = {}
_jobs_lock = threading.Lock()
_MAX_JOBS = 50

_LLM_DISABLED_MSG = (
    "LLM features are disabled on this server (LLM_ENABLED=false). Run "
    "ingestion/backfills on a machine with Ollama against the same database."
)


@bp.route("/api/admin/check", methods=["GET"])
@require_admin
def check():
    """Cheap endpoint the frontend uses to gate the dashboard UI."""
    return jsonify({"admin": True, "email": g.email, "llm_enabled": config.LLM_ENABLED})


@bp.route("/api/admin/ingest", methods=["POST"])
@require_admin
def start_ingest():
    if not config.LLM_ENABLED:
        return jsonify({"error": _LLM_DISABLED_MSG}), 400
    body = request.get_json(silent=True) or {}
    field = (body.get("field") or "").strip() or None
    query = (body.get("query") or "").strip() or None
    limit = _clamp(body.get("limit"), default=10, lo=1, hi=100)
    min_citations = _clamp(body.get("min_citations"), default=0, lo=0, hi=100000)

    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _prune_jobs()
        _jobs[job_id] = {
            "job_id": job_id,
            "kind": "ingest",
            "status": "running",
            "field": field,
            "query": query,
            "limit": limit,
            "min_citations": min_citations,
            "counts": None,
            "progress": {"fetched": 0, "processed": 0, "ingested": 0,
                         "skipped": 0, "errors": 0, "current": None},
            "error": None,
            "started_at": now_iso(),
            "finished_at": None,
            "by": g.email,
        }

    threading.Thread(
        target=_run_job, args=(job_id, field, query, limit, min_citations),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id, "status": "running"})


def _run_job(job_id, field, query, limit, min_citations):
    def on_progress(counts, current):
        _update(job_id, progress={**counts, "current": current})

    try:
        if query:
            counts = pipeline.ingest_query(
                query, limit=limit, min_citations=min_citations,
                on_progress=on_progress,
            )
        else:
            counts = pipeline.ingest_seed_fields(
                limit=limit, min_citations=min_citations, only_slug=field,
                on_progress=on_progress,
            )
        _update(job_id, status="done", counts=counts, finished_at=now_iso())
    except Exception as e:  # noqa: BLE001 - surface to the dashboard
        logger.exception("Ingestion job %s failed", job_id)
        _update(job_id, status="error", error=str(e), finished_at=now_iso())


# ── Utility scripts (secondary-field / keyword recompute, seen reset) ───────
# Same job registry/polling as ingestion, distinguished by "kind" so the
# dashboard can render each appropriately.

def _start_job(kind, extra=None):
    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _prune_jobs()
        _jobs[job_id] = {
            "job_id": job_id,
            "kind": kind,
            "status": "running",
            "counts": None,
            "progress": {"processed": 0},
            "error": None,
            "started_at": now_iso(),
            "finished_at": None,
            "by": g.email,
            **(extra or {}),
        }
    return job_id


@bp.route("/api/admin/utility/secondary-fields", methods=["POST"])
@require_admin
def utility_secondary_fields():
    """Recompute secondary fields for every paper (or just ones lacking them)."""
    if not config.LLM_ENABLED:
        return jsonify({"error": _LLM_DISABLED_MSG}), 400
    body = request.get_json(silent=True) or {}
    force = bool(body.get("force"))
    job_id = _start_job("secondary_fields", {"force": force})
    threading.Thread(
        target=_run_secondary_fields_job, args=(job_id, force), daemon=True,
    ).start()
    return jsonify({"job_id": job_id, "status": "running"})


def _run_secondary_fields_job(job_id, force):
    try:
        after_id, total = 0, 0
        while True:
            n, after_id = pipeline.backfill_secondary_fields(
                limit=50, after_id=after_id, force=force
            )
            total += n
            _update(job_id, progress={"processed": total})
            if n == 0:
                break
        _update(job_id, status="done", counts={"processed": total},
                finished_at=now_iso())
    except Exception as e:  # noqa: BLE001 - surface to the dashboard
        logger.exception("Secondary-field backfill job %s failed", job_id)
        _update(job_id, status="error", error=str(e), finished_at=now_iso())


@bp.route("/api/admin/utility/keywords", methods=["POST"])
@require_admin
def utility_keywords():
    """Recompute search keywords for every field (or just ones lacking them)."""
    if not config.LLM_ENABLED:
        return jsonify({"error": _LLM_DISABLED_MSG}), 400
    body = request.get_json(silent=True) or {}
    force = bool(body.get("force"))
    job_id = _start_job("keywords", {"force": force})
    threading.Thread(
        target=_run_keywords_job, args=(job_id, force), daemon=True,
    ).start()
    return jsonify({"job_id": job_id, "status": "running"})


def _run_keywords_job(job_id, force):
    try:
        after_id, total = 0, 0
        while True:
            n, after_id = pipeline.backfill_field_keywords(
                limit=50, after_id=after_id, force=force
            )
            total += n
            _update(job_id, progress={"processed": total})
            if n == 0:
                break
        _update(job_id, status="done", counts={"processed": total},
                finished_at=now_iso())
    except Exception as e:  # noqa: BLE001 - surface to the dashboard
        logger.exception("Keyword backfill job %s failed", job_id)
        _update(job_id, status="error", error=str(e), finished_at=now_iso())


@bp.route("/api/admin/utility/reset-seen", methods=["POST"])
@require_admin
def utility_reset_seen():
    """Clear seen-paper history for every user, so the feed treats all papers
    as unseen again. Fast (a single DELETE), so it runs synchronously."""
    with get_conn() as conn:
        deleted = models.reset_seen(conn)
    return jsonify({"deleted": deleted})


@bp.route("/api/admin/ingest/<job_id>", methods=["GET"])
@require_admin
def job_status(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify(job)


@bp.route("/api/admin/jobs", methods=["GET"])
@require_admin
def list_jobs():
    with _jobs_lock:
        jobs = sorted(_jobs.values(), key=lambda j: j["started_at"], reverse=True)
    return jsonify({"jobs": jobs})


@bp.route("/api/admin/log", methods=["GET"])
@require_admin
def ingest_log():
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT query, s2_paper_id, status, detail, created_at
               FROM ingest_log ORDER BY id DESC LIMIT 50"""
        ).fetchall()
    return jsonify({"log": [dict(r) for r in rows]})


@bp.route("/api/admin/fields", methods=["GET"])
@require_admin
def admin_fields():
    """Field list (with queries) for the dashboard's field picker."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT slug, name, query, is_seed FROM fields ORDER BY name"
        ).fetchall()
    return jsonify({"fields": [dict(r) for r in rows]})


# ── helpers ─────────────────────────────────────────────────────────────────

def _clamp(value, default, lo, hi):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(v, hi))


def _update(job_id, **fields):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(fields)


def _prune_jobs():
    """Drop oldest finished jobs once we exceed the cap (call under lock)."""
    if len(_jobs) < _MAX_JOBS:
        return
    finished = sorted(
        (j for j in _jobs.values() if j["status"] != "running"),
        key=lambda j: j["started_at"],
    )
    for job in finished[: len(_jobs) - _MAX_JOBS + 1]:
        _jobs.pop(job["job_id"], None)
