"""Firebase token verification, modeled on /home/jcrayb/github/app.

Token may arrive as an `Authorization: Bearer <idToken>` header (GET) or in a
JSON body (`{idToken, uid}`, POST). UIDs are regex-guarded as defense in depth.
"""
import functools
import logging
import re

from firebase_admin import auth as fb_auth
from flask import g, jsonify, request

import config
import models
from db import get_conn

logger = logging.getLogger(__name__)

# Reject anything that isn't a plain Firebase UID.
_UID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _verify_decoded(id_token, uid):
    """Verify a Firebase ID token. Returns the decoded claims dict or None."""
    if not id_token or not uid or not _UID_RE.match(uid):
        return None
    try:
        decoded = fb_auth.verify_id_token(id_token)
    except fb_auth.RevokedIdTokenError:
        return None
    except fb_auth.UserDisabledError:
        return None
    except Exception as e:  # noqa: BLE001 - never leak details to the client
        logger.warning("Token verification failed: %s", type(e).__name__)
        return None

    verified_uid = decoded.get("uid", "")
    if verified_uid != uid or not _UID_RE.match(verified_uid):
        return None
    return decoded


def verify_token(id_token, uid):
    """Verify a Firebase ID token. Returns the verified UID or None.

    Auto-provisions the user row on first valid login.
    """
    decoded = _verify_decoded(id_token, uid)
    if not decoded:
        return None
    with get_conn() as conn:
        models.ensure_user(conn, decoded["uid"])
    return decoded["uid"]


def is_admin(decoded) -> bool:
    """True if the token belongs to a verified admin email."""
    if not decoded or not decoded.get("email_verified", False):
        return False
    email = (decoded.get("email") or "").lower()
    return bool(email) and email in config.admin_emails()


def _extract_credentials():
    """Pull (id_token, uid) from header+query (GET) or JSON body (POST).

    POST helpers may send *both* an Authorization header and a JSON body that
    carries the uid; fall back to the body uid when it isn't in the query string.
    """
    body = request.get_json(silent=True) or {} if request.is_json else {}
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        id_token = auth_header.removeprefix("Bearer ")
        uid = request.args.get("uid", "") or body.get("uid", "")
        return id_token, uid
    return body.get("idToken", ""), body.get("uid", "")


def require_auth(fn):
    """Decorator: verify the caller and stash the UID on `flask.g.uid`."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        id_token, uid = _extract_credentials()
        verified = verify_token(id_token, uid)
        if not verified:
            return jsonify({"error": "unauthorized"}), 401
        g.uid = verified
        return fn(*args, **kwargs)

    return wrapper


def optional_auth(fn):
    """Decorator: set `flask.g.uid` if a valid token is present, else None."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        id_token, uid = _extract_credentials()
        g.uid = verify_token(id_token, uid) if id_token else None
        return fn(*args, **kwargs)

    return wrapper


def require_admin(fn):
    """Decorator: verify the caller AND require an admin email.

    401 if the token is invalid, 403 if valid but not an allow-listed admin.
    Stashes `flask.g.uid` and `flask.g.email`.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        id_token, uid = _extract_credentials()
        decoded = _verify_decoded(id_token, uid)
        
        if not decoded:
            return jsonify({"error": "unauthorized"}), 401
        if not is_admin(decoded):
            return jsonify({"error": "forbidden"}), 403
        with get_conn() as conn:
            models.ensure_user(conn, decoded["uid"])
        g.uid = decoded["uid"]
        g.email = decoded.get("email", "")
        return fn(*args, **kwargs)

    return wrapper
