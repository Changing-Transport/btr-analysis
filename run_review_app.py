"""
Run the analyst review app locally.

    python run_review_app.py [--db ...] [--port 5000]

Then open http://127.0.0.1:5000

By default the app works on `data/btr_analysis.local.db`, a private working
copy seeded from the version-controlled snapshot `data/btr_analysis.db` the
first time it runs. The snapshot is what git tracks and what colleagues get on
clone; the working copy is git-ignored, so pulling, switching branches or
merging can never overwrite review decisions.

Pass `--db data/btr_analysis.db` to work on the tracked snapshot directly (for
example to commit a round of review), but do not leave the app running while
performing git operations on it.
"""

import argparse
import os
import shutil

from btr_pipeline import db as dbmod
from review_app import create_app

SNAPSHOT_DB = os.path.join("data", "btr_analysis.db")
WORKING_DB = os.path.join("data", "btr_analysis.local.db")


def ensure_working_copy(path: str) -> None:
    """Seed the git-ignored working copy from the tracked snapshot once."""
    if os.path.exists(path) or path != WORKING_DB:
        return
    if not os.path.exists(SNAPSHOT_DB):
        raise SystemExit(
            f"Neither {path} nor the snapshot {SNAPSHOT_DB} exists. "
            f"Run the pipeline first, or clone the repository afresh."
        )
    shutil.copy2(SNAPSHOT_DB, path)
    print(f"Created your working copy {path} from {SNAPSHOT_DB}.")
    print("Decisions you make are stored there and are not tracked by git.")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=WORKING_DB,
                   help=f"database to use (default {WORKING_DB})")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()

    ensure_working_copy(args.db)

    # Ensure the review columns exist before serving.
    conn = dbmod.connect(args.db)
    dbmod.init_schema(conn)
    conn.close()

    app = create_app(args.db)
    print(f"Review app on http://{args.host}:{args.port}  (db: {args.db})")
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
