"""
BTR review app — analyst UI for validating transport relevance and BTR↔NDC matches.

Structured as a Flask blueprint behind a small app factory so it can either run
standalone (see run_review_app.py) or be mounted into the climate-policy-miner
Flask app later:

    from review_app import bp as review_bp
    app.register_blueprint(review_bp, url_prefix="/btr-review")
    app.config["BTR_DB_PATH"] = "postgresql://..."   # or a SQLite path
"""

import os

from flask import Flask

from .views import bp

DEFAULT_DB = os.path.join("data", "btr_analysis.db")


def create_app(db_path: str | None = None) -> Flask:
    """
    db_path: SQLite file or postgresql:// URL. Falls back to BTR_DATABASE_URL,
    then BTR_DB_PATH, then the tracked snapshot.

    BTR_EASY_AUTH=1 takes the analyst's identity from the sign-in of Azure
    Container Apps authentication instead of a typed name. Only set it when
    that authentication is enabled and requires sign-in for every request.
    """
    app = Flask(__name__)
    app.config["BTR_DB_PATH"] = (db_path or os.environ.get("BTR_DATABASE_URL")
                                 or os.environ.get("BTR_DB_PATH", DEFAULT_DB))
    app.config["EASY_AUTH"] = os.environ.get("BTR_EASY_AUTH", "") in ("1", "true", "yes")

    secret = os.environ.get("FLASK_SECRET_KEY")
    if not secret and app.config["EASY_AUTH"]:
        raise RuntimeError("FLASK_SECRET_KEY must be set for a hosted deployment.")
    # Local default: the session only carries the typed analyst name.
    app.secret_key = secret or "btr-review-pilot"
    app.register_blueprint(bp)
    return app


__all__ = ["bp", "create_app"]
