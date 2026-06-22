"""Auth-gated paper likes and saved collections."""
from flask import Blueprint, g, jsonify, request

from auth import require_auth
from db import get_conn
import models

bp = Blueprint("collections", __name__)


# ── Likes ────────────────────────────────────────────────────────────────────

@bp.route("/api/likes", methods=["GET"])
@require_auth
def get_likes():
    with get_conn() as conn:
        return jsonify({"items": models.list_liked_papers(conn, g.uid)})


@bp.route("/api/likes/ids", methods=["GET"])
@require_auth
def get_liked_ids():
    """Just the liked paper ids, so a page rendering many cards can mark each
    one liked/unliked without a request per card."""
    with get_conn() as conn:
        return jsonify({"paper_ids": models.liked_paper_ids(conn, g.uid)})


@bp.route("/api/likes", methods=["POST"])
@require_auth
def add_like():
    body = request.get_json(silent=True) or {}
    paper_id = body.get("paper_id")
    if not isinstance(paper_id, int):
        return jsonify({"error": "paper_id required"}), 400
    with get_conn() as conn:
        models.like_paper(conn, g.uid, paper_id)
    return jsonify({"liked": True})


@bp.route("/api/likes/<int:paper_id>", methods=["DELETE"])
@require_auth
def remove_like(paper_id):
    with get_conn() as conn:
        models.unlike_paper(conn, g.uid, paper_id)
    return jsonify({"liked": False})


# ── Collections ──────────────────────────────────────────────────────────────

@bp.route("/api/collections/saved-ids", methods=["GET"])
@require_auth
def get_saved_ids():
    """Ids of every paper saved in any collection, so a page rendering many
    cards can mark each one saved/unsaved without a request per card."""
    with get_conn() as conn:
        return jsonify({"paper_ids": models.saved_paper_ids(conn, g.uid)})


@bp.route("/api/collections/paper/<int:paper_id>", methods=["GET"])
@require_auth
def get_collections_for_paper(paper_id):
    """Which of this user's collections already contain this paper — powers
    the save popover's per-collection added/not-added state."""
    with get_conn() as conn:
        return jsonify({"collection_ids": models.collections_containing_paper(conn, g.uid, paper_id)})


@bp.route("/api/collections", methods=["GET"])
@require_auth
def get_collections():
    limit = request.args.get("limit", type=int)
    with get_conn() as conn:
        return jsonify({"collections": models.list_collections(conn, g.uid, limit=limit)})


@bp.route("/api/collections", methods=["POST"])
@require_auth
def add_collection():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    with get_conn() as conn:
        collection_id = models.create_collection(conn, g.uid, name)
        collections = models.list_collections(conn, g.uid)
    return jsonify({"collection_id": collection_id, "collections": collections})


@bp.route("/api/collections/<int:collection_id>", methods=["GET"])
@require_auth
def get_collection(collection_id):
    with get_conn() as conn:
        collection = models.get_collection(conn, g.uid, collection_id)
        if not collection:
            return jsonify({"error": "not found"}), 404
        items = models.list_collection_papers(conn, g.uid, collection_id)
    return jsonify({"collection": collection, "items": items})


@bp.route("/api/collections/<int:collection_id>/papers", methods=["POST"])
@require_auth
def add_collection_paper(collection_id):
    body = request.get_json(silent=True) or {}
    paper_id = body.get("paper_id")
    if not isinstance(paper_id, int):
        return jsonify({"error": "paper_id required"}), 400
    with get_conn() as conn:
        if not models.add_to_collection(conn, g.uid, collection_id, paper_id):
            return jsonify({"error": "not found"}), 404
    return jsonify({"added": True})


@bp.route("/api/collections/<int:collection_id>/papers/<int:paper_id>", methods=["DELETE"])
@require_auth
def remove_collection_paper(collection_id, paper_id):
    with get_conn() as conn:
        if not models.remove_from_collection(conn, g.uid, collection_id, paper_id):
            return jsonify({"error": "not found"}), 404
    return jsonify({"added": False})
