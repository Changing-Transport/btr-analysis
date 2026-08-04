"""
BTR review app — analyst UI for validating transport relevance and BTR↔NDC matches.

Structured as a Flask blueprint behind a small app factory so it can either run
standalone (see run_review_app.py) or be mounted into the climate-policy-miner
Flask app later:

    from review_app import bp as review_bp
    app.register_blueprint(review_bp, url_prefix="/btr-review")
    app.config["BTR_DB_PATH"] = "/path/to/btr_analysis.db"
"""

import os

from flask import Flask

from .views import bp

DEFAULT_DB = os.path.join("data", "btr_analysis.db")


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config["BTR_DB_PATH"] = db_path or os.environ.get("BTR_DB_PATH", DEFAULT_DB)
    # Pilot-only secret: the app holds no credentials, the session just carries
    # the analyst's name. Set FLASK_SECRET_KEY when deploying.
    app.secret_key = os.environ.get("FLASK_SECRET_KEY", "btr-review-pilot")
    app.register_blueprint(bp)
    return app


__all__ = ["bp", "create_app"]
