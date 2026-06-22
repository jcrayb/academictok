"""Auth-gated per-user subscription management."""
from flask import Blueprint, g, jsonify, request

from auth import require_auth
from db import get_conn
import models

bp = Blueprint("subscriptions", __name__)


@bp.route("/api/subscriptions", methods=["GET"])
@require_auth
def get_subscriptions():
    with get_conn() as conn:
        return jsonify({"subscriptions": models.list_subscriptions(conn, g.uid)})


@bp.route("/api/subscriptions", methods=["POST"])
@require_auth
def add_subscription():
    body = request.get_json(silent=True) or {}
    slug = (body.get("slug") or "").strip()
    if not slug:
        return jsonify({"error": "slug required"}), 400
    with get_conn() as conn:
        if not models.subscribe(conn, g.uid, slug):
            return jsonify({"error": "field not found"}), 404
        subs = models.list_subscriptions(conn, g.uid)
    return jsonify({"subscriptions": subs})


@bp.route("/api/subscriptions/<slug>", methods=["DELETE"])
@require_auth
def remove_subscription(slug):
    with get_conn() as conn:
        models.unsubscribe(conn, g.uid, slug)
        subs = models.list_subscriptions(conn, g.uid)
    return jsonify({"subscriptions": subs})
