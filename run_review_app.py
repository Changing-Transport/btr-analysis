"""
Run the analyst review app locally.

    python run_review_app.py [--db data/btr_analysis.db] [--port 5000]

Then open http://127.0.0.1:5000
"""

import argparse

from btr_pipeline import db as dbmod
from review_app import create_app


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="data/btr_analysis.db")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()

    # Ensure the review columns exist before serving.
    conn = dbmod.connect(args.db)
    dbmod.init_schema(conn)
    conn.close()

    app = create_app(args.db)
    print(f"Review app on http://{args.host}:{args.port}  (db: {args.db})")
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
