"""
WSGI entry point for the hosted review app (gunicorn wsgi:app).

Configuration comes from the environment (Container Apps secrets/variables):
    BTR_DATABASE_URL     postgresql://... (required)
    FLASK_SECRET_KEY     random string (required when BTR_EASY_AUTH=1)
    BTR_EASY_AUTH        1 once Container Apps authentication is enabled
    AZURE_OPENAI_*       optional; lets the app generate NDC candidates for a
                         measure the moment it is confirmed transport-relevant

The schema is not created here: run `python pipeline.py init-db` (or copy-db)
with the admin role. The app connects with a role that has no DDL rights.
"""

import os

from review_app import create_app

if not os.environ.get("BTR_DATABASE_URL"):
    raise RuntimeError("BTR_DATABASE_URL is not set.")

app = create_app()
