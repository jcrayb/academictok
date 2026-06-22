"""Auth endpoints: verify a token and provision the user."""
from flask import Blueprint, g, jsonify

from auth import require_auth

bp = Blueprint("auth_routes", __name__)


@bp.route("/api/verifyUserId", methods=["POST"])
@require_auth
def verify_user_id():
    """Verify the token and provision the users row (done in require_auth)."""
    return jsonify({"valid": True, "uid": g.uid})
