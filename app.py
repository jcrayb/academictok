"""AcademicTok Flask API (port 8080)."""
import logging

import firebase_admin
from firebase_admin import credentials
from flask import Flask, jsonify
from flask_cors import CORS

import config
from db import init_db
from seeds import seed_fields

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def create_app():
    app = Flask(__name__)

    # Firebase Admin: used for verify_id_token. Initialize once.
    if not firebase_admin._apps:
        cred = credentials.Certificate(config.FIREBASE_CRED_PATH)
        firebase_admin.initialize_app(cred)

    # CORS fails closed: only origins in CORS_ORIGINS are allowed.
    CORS(app, origins=config.CORS_ORIGINS)
    if not config.CORS_ORIGINS:
        logger.warning("CORS_ORIGINS is empty — no cross-origin requests allowed.")
    #print(config.CORS_ORIGINS)
    # Ensure schema + seed fields exist on boot.
    init_db()
    seed_fields()

    from routes.admin import bp as admin_bp
    from routes.auth_routes import bp as auth_bp
    from routes.collections import bp as collections_bp
    from routes.feed import bp as feed_bp
    from routes.fields import bp as fields_bp
    from routes.media import bp as media_bp
    from routes.subscriptions import bp as subs_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(fields_bp)
    app.register_blueprint(subs_bp)
    app.register_blueprint(feed_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(media_bp)
    app.register_blueprint(collections_bp)

    @app.route("/api/health")
    def health():
        return jsonify({"status": "ok"})

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=config.PORT, debug=config.DEBUG)
