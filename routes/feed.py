"""Feed endpoints: subscribed feed (auth), mixed discovery feed, seen tracking."""
import math

from flask import Blueprint, g, jsonify, request

import config
import models
from auth import optional_auth, require_auth
from db import get_conn

bp = Blueprint("feed", __name__)


@bp.route("/api/seen", methods=["POST"])
@require_auth
def mark_seen():
    """Record papers the user has scrolled past (batched from the client)."""
    body = request.get_json(silent=True) or {}
    raw = body.get("paper_ids") or []
    ids = [int(i) for i in raw if str(i).lstrip("-").isdigit()][:500]
    with get_conn() as conn:
        models.mark_papers_seen(conn, g.uid, ids)
    return jsonify({"ok": True, "count": len(ids)})


@bp.route("/api/feed", methods=["GET"])
@optional_auth
def feed():
    mode = request.args.get("mode", "mixed")
    if mode == "subscribed":
        return _subscribed_feed()
    return _mixed_feed()


@require_auth
def _subscribed_feed():
    """Summaries drawn only from the user's subscribed fields."""
    before_id = request.args.get("before_id", type=int)
    page_size = request.args.get("page_size", default=config.DEFAULT_PAGE_SIZE, type=int)
    with get_conn() as conn:
        field_ids = models.subscribed_field_ids(conn, g.uid)
        items = models.feed_for_fields(
            conn, field_ids, page_size, before_id, unseen_uid=g.uid
        )
    return jsonify({
        "mode": "subscribed",
        "items": items,
        "next_before_id": items[-1]["paper_id"] if items else None,
    })


def _mixed_feed():
    """Interleave subscribed-field items with discovery items from other fields.

    Uses separate keyset cursors per pool (sub_before_id / other_before_id) so
    pagination never skips or repeats items. When signed out, all items come
    from the discovery pool (i.e. the whole corpus).
    """
    page_size = request.args.get("page_size", default=config.DEFAULT_PAGE_SIZE, type=int)
    sub_before = request.args.get("sub_before_id", type=int)
    other_before = request.args.get("other_before_id", type=int)

    sub_n = math.ceil(page_size * config.MIXED_SUBSCRIBED_RATIO)
    other_n = page_size - sub_n

    # Signed-in users don't see papers they've already scrolled past.
    unseen_uid = g.uid or None
    with get_conn() as conn:
        sub_ids = models.subscribed_field_ids(conn, g.uid) if g.uid else []
        if not sub_ids:
            # No subscriptions (or signed out): everything is discovery.
            other_items = models.feed_all(
                conn, page_size, other_before, unseen_uid=unseen_uid
            )
            sub_items = []
        else:
            sub_items = models.feed_for_fields(
                conn, sub_ids, sub_n, sub_before, unseen_uid=unseen_uid
            )
            other_items = models.feed_all(
                conn, other_n, other_before, exclude_field_ids=sub_ids,
                unseen_uid=unseen_uid,
            )

    merged = _interleave(sub_items, other_items)
    return jsonify({
        "mode": "mixed",
        "items": merged,
        "next_sub_before_id": sub_items[-1]["paper_id"] if sub_items else sub_before,
        "next_other_before_id": other_items[-1]["paper_id"] if other_items else other_before,
        "exhausted": not merged,
    })


def _interleave(primary, secondary):
    """Interleave two lists, keeping rough proportional spacing."""
    out = []
    i = j = 0
    total = len(primary) + len(secondary)
    if total == 0:
        return out
    # Walk positions, preferring whichever pool is "behind" its share.
    while i < len(primary) or j < len(secondary):
        take_primary = (
            j >= len(secondary)
            or (i < len(primary)
                and i / max(len(primary), 1) <= j / max(len(secondary), 1))
        )
        if take_primary and i < len(primary):
            out.append(primary[i]); i += 1
        elif j < len(secondary):
            out.append(secondary[j]); j += 1
        elif i < len(primary):
            out.append(primary[i]); i += 1
    return out
