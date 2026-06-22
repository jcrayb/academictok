"""Serve figure images extracted from PDFs (public, read-only)."""
import os

from flask import Blueprint, abort, send_from_directory

import config

bp = Blueprint("media", __name__)


@bp.route("/media/figures/<path:filename>", methods=["GET"])
def figure(filename):
    figures_dir = os.path.abspath(os.path.join(config.MEDIA_DIR, "figures"))
    # send_from_directory rejects path traversal; 404 on anything missing.
    if not os.path.isdir(figures_dir):
        abort(404)
    return send_from_directory(figures_dir, filename, max_age=86400)
