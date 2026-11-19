"""
BTR extraction pipeline — command-line entry point.

Subcommands:
    batch       Ingest every .xlsx in the cache directory (one-time batch run).
                Re-running is idempotent: each document's measures are replaced.
    ingest      Ingest a single document — the reproducible path for new BTRs:
                    python pipeline.py ingest --file downloaded_xlsx/Chile.xlsx
    import-ndc  Import NDC/LTS measures from the GIZ-SLOCAT Tracker database
                export (Mitigation sheet) into ndc_measures.
    classify    LLM confirmation of 'pending' content-match measures via
                Azure OpenAI (requires credentials; see --help).
    stats       Print database summary.
    init-db     Create or migrate the schema (SQLite or PostgreSQL).
    copy-db     Copy a SQLite database into PostgreSQL and verify the copy.
    create-app-role
                Create the PostgreSQL login the hosted review app uses
                (row read/write in schema btr, no schema changes).
    export-decisions
                Save one reviewer's decisions to a file in decisions/.
    import-decisions
                Merge decision files into a database (see SHARING_DECISIONS.md).

Database: `--db` takes a SQLite path or a postgresql:// URL and goes before the
subcommand. Without it, BTR_DATABASE_URL (environment or .env) is used, else
the tracked snapshot data/btr_analysis.db.

Environment variables for `classify` (same names as the climate-policy-miner app):
    AZURE_OPENAI_ENDPOINT
    AZURE_OPENAI_API_KEY
    AZURE_OPENAI_API_VERSION
    AZURE_OPENAI_CHAT_DEPLOYMENT_NAME
"""

import argparse
import os
import re
import sys

from btr_pipeline import db as dbmod
from btr_pipeline.countries import to_tracker_name
from btr_pipeline.env import load_env_file
from btr_pipeline.extract import extract_measures

DEFAULT_DB = dbmod.DEFAULT_DB
WORKING_DB = os.path.join("data", "btr_analysis.local.db")
DECISIONS_DIR = "decisions"
DEFAULT_CACHE = os.path.join("data", "downloaded_xlsx")
RESULTS_XLSX = os.path.join("data", "btr_transport_results.xlsx")

# UNFCCC listing name -> cache filename stem (same map as brt_transport_analysis.py)
COUNTRY_FILENAME_MAP = {
    "Lao People's Democratic Republic": "Lao_PDR",
    "Venezuela (Bolivarian Republic of)": "Venezuela",
    "United Kingdom of Great Britain and Northern Ireland": "United_Kingdom",
}


def log(msg: str) -> None:
    print(msg, flush=True)




def country_from_filename(fname: str) -> str:
    """Derive the country from a cache filename, in Tracker naming convention."""
    raw = os.path.basename(fname).replace(".xlsx", "").replace("_", " ")
    return to_tracker_name(raw)


def load_doc_urls(results_path: str) -> dict[str, str]:
    """
    Build filename-stem -> document-page-URL mapping from the screening output
    (btr_transport_results.xlsx), so `documents.doc_url` can be populated.
    """
    if not os.path.exists(results_path):
        return {}
    try:
        import openpyxl
        wb = openpyxl.load_workbook(results_path, read_only=True, data_only=True)
        ws = wb["Results"]
        mapping: dict[str, str] = {}
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i == 0 or row[0] is None:
                continue
            country, _, _, _, doc_url, *_ = row
            url = str(doc_url or "").strip()
            if not url.startswith("http"):
                continue
            stem = COUNTRY_FILENAME_MAP.get(str(country)) or re.sub(r"[^\w\-]", "_", str(country))
            mapping[stem] = url
        wb.close()
        return mapping
    except Exception as exc:
        log(f"  (could not read doc URLs from {results_path}: {exc})")
        return {}


def ingest_one(
    conn,
    xlsx_path: str,
    country: str,
    doc_type: str = "BTR1",
    doc_url: str | None = None,
    force: bool = False,
) -> dict:
    """
    Extract one file and write document + measures to the DB.

    If the document already carries analyst review decisions, the result dict
    has a 'skipped' key explaining why nothing was written (pass force=True to
    overwrite and discard that review work).
    """
    res = extract_measures(xlsx_path, country)
    if res["error"]:
        return res

    try:
        doc_id = dbmod.upsert_document(
            conn,
            country=country,
            source_file=os.path.basename(xlsx_path),
            doc_type=doc_type,
            file_hash=dbmod.file_sha256(xlsx_path),
            doc_url=doc_url,
            table5_sheet=res["sheet"],
            header_row_index=res["header_row_index"],
            ghg_unit=res["ghg_unit"],
            force=force,
        )
    except dbmod.ReviewedDocumentError as exc:
        res["skipped"] = str(exc)
        return res

    dbmod.insert_measures(conn, doc_id, res["measures"])
    conn.commit()
    return res


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_batch(args) -> None:
    if not os.path.isdir(args.cache):
        log(f"ERROR: cache directory '{args.cache}' not found.")
        sys.exit(1)

    files = sorted(f for f in os.listdir(args.cache) if f.lower().endswith(".xlsx"))
    if not files:
        log(f"No .xlsx files in '{args.cache}'.")
        sys.exit(1)

    doc_urls = load_doc_urls(RESULTS_XLSX)
    conn = dbmod.connect(args.db)
    dbmod.init_schema(conn)

    log(f"Batch ingestion: {len(files)} files -> {args.db}\n")

    totals = {"sector_label": 0, "content_match": 0, "sector_specific_file": 0}
    errors: list[str] = []
    skipped: list[str] = []

    for i, fname in enumerate(files, 1):
        path = os.path.join(args.cache, fname)
        country = country_from_filename(fname)
        stem = fname[:-len(".xlsx")]

        res = ingest_one(conn, path, country, doc_url=doc_urls.get(stem),
                         force=args.force)

        if res["error"]:
            log(f"  [{i:3d}/{len(files)}] ERR  {fname}")
            log(f"             -> {res['error']}")
            errors.append(f"{fname}: {res['error']}")
            continue

        if res.get("skipped"):
            log(f"  [{i:3d}/{len(files)}] SKIP {fname}")
            log(f"             -> {res['skipped']}")
            skipped.append(f"{fname}: {res['skipped']}")
            continue

        counts: dict[str, int] = {}
        for m in res["measures"]:
            counts[m["relevance_source"]] = counts.get(m["relevance_source"], 0) + 1
            totals[m["relevance_source"]] = totals.get(m["relevance_source"], 0) + 1

        detail = ", ".join(f"{k}={v}" for k, v in counts.items()) or "no transport content"
        log(f"  [{i:3d}/{len(files)}] OK   {fname}  ({detail})")

    conn.close()

    log(f"\nDone. Measures by relevance source:")
    for k, v in totals.items():
        log(f"  {k:<22s} {v}")
    if errors:
        log(f"\n{len(errors)} file(s) had errors:")
        for e in errors:
            log(f"  {e}")
    if skipped:
        log(f"\n{len(skipped)} document(s) skipped to protect analyst review:")
        for s in skipped:
            log(f"  {s}")
        log("  Re-run with --force to overwrite them.")


def cmd_ingest(args) -> None:
    if not os.path.exists(args.file):
        log(f"ERROR: file not found: {args.file}")
        sys.exit(1)

    country = to_tracker_name(args.country) if args.country else country_from_filename(args.file)
    conn = dbmod.connect(args.db)
    dbmod.init_schema(conn)

    log(f"Ingesting {args.file}  (country: {country}, type: {args.doc_type})")
    res = ingest_one(conn, args.file, country, doc_type=args.doc_type,
                     doc_url=args.url, force=args.force)
    conn.close()

    if res["error"]:
        log(f"ERROR: {res['error']}")
        sys.exit(1)

    if res.get("skipped"):
        log(f"SKIPPED: {res['skipped']}")
        sys.exit(1)

    by_src: dict[str, int] = {}
    for m in res["measures"]:
        by_src[m["relevance_source"]] = by_src.get(m["relevance_source"], 0) + 1
    log(f"  sheet: {res['sheet']}  |  GHG unit: {res['ghg_unit'] or 'not detected'}")
    log(f"  measures stored: {len(res['measures'])}  ({by_src})")
    log(f"  non-transport rows skipped: {res['skipped_rows']}")


def cmd_import_ndc(args) -> None:
    if not os.path.exists(args.file):
        log(f"ERROR: file not found: {args.file}")
        sys.exit(1)

    from btr_pipeline.ndc_import import import_ndc_measures

    conn = dbmod.connect(args.db)
    dbmod.init_schema(conn)

    log(f"Importing NDC measures from {args.file} (sheet 'Mitigation')...")
    try:
        res = import_ndc_measures(conn, args.file)
    except ValueError as exc:
        log(f"ERROR: {exc}")
        conn.close()
        sys.exit(1)
    conn.close()

    log(f"  imported: {res['rows']} measures from {res['countries']} countries")
    if res["skipped"]:
        log(f"  skipped (no country/quote): {res['skipped']}")


def cmd_embed(args) -> None:
    load_env_file()
    from btr_pipeline.match import embed_missing

    conn = dbmod.connect(args.db)
    dbmod.init_schema(conn)
    try:
        embed_missing(conn, log=log)
    except RuntimeError as exc:
        log(f"ERROR: {exc}")
        sys.exit(1)
    finally:
        conn.close()


def cmd_match(args) -> None:
    load_env_file()
    from btr_pipeline.match import match_missing, run_matching

    conn = dbmod.connect(args.db)
    dbmod.init_schema(conn)
    if args.missing_only:
        match_missing(conn, log=log, include_archived=not args.active_only)
    else:
        run_matching(conn, log=log, include_archived=not args.active_only)
    conn.close()


def cmd_classify(args) -> None:
    load_env_file()
    required = [
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_API_VERSION",
        "AZURE_OPENAI_CHAT_DEPLOYMENT_NAME",
    ]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        log("Azure OpenAI credentials are not configured. Set these environment")
        log("variables (same names as the climate-policy-miner app) and re-run:")
        for v in missing:
            log(f"  {v}")
        sys.exit(1)

    try:
        from openai import AzureOpenAI
    except ImportError:
        log("The 'openai' package is not installed. Run: pip install openai")
        sys.exit(1)

    client = AzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        api_version=os.environ["AZURE_OPENAI_API_VERSION"],
    )
    deployment = os.environ["AZURE_OPENAI_CHAT_DEPLOYMENT_NAME"]

    conn = dbmod.connect(args.db)
    rows = conn.execute(
        """SELECT m.id, d.country, m.measure_name, m.description, m.objectives, m.sectors
           FROM btr_measures m JOIN documents d ON d.id = m.document_id
           WHERE m.review_status = 'pending' AND m.llm_relevant IS NULL
           ORDER BY m.id
           LIMIT ?""",
        (args.limit,),
    ).fetchall()

    if not rows:
        log("No pending measures without an LLM verdict — nothing to classify.")
        conn.close()
        return

    log(f"Classifying {len(rows)} pending measures with deployment '{deployment}'...\n")

    system_prompt = (
        "You assess climate policy measures from Biennial Transparency Reports. "
        "The measure below was classified under a non-transport sector (usually "
        "energy) but its text mentions transport-related terms. Decide whether "
        "the measure directly affects the transport sector (vehicles, modal "
        "shift, transport fuels/charging, freight, mobility infrastructure). "
        "Answer with exactly one line: 'yes: <one-sentence reason>' or "
        "'no: <one-sentence reason>'."
    )

    for mid, country, name, desc, obj, sectors in rows:
        user_prompt = (
            f"Country: {country}\nSector label: {sectors}\nMeasure: {name}\n"
            f"Description: {desc or '-'}\nObjectives: {obj or '-'}"
        )
        try:
            resp = client.chat.completions.create(
                model=deployment,
                temperature=0,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            answer = (resp.choices[0].message.content or "").strip()
        except Exception as exc:
            log(f"  [id {mid}] API error: {exc}")
            continue

        verdict = "yes" if answer.lower().startswith("yes") else "no"
        reasoning = answer.split(":", 1)[1].strip() if ":" in answer else answer
        conn.execute(
            "UPDATE btr_measures SET llm_relevant = ?, llm_reasoning = ? WHERE id = ?",
            (verdict, reasoning, mid),
        )
        conn.commit()
        log(f"  [id {mid}] {verdict:<3s} {country} | {str(name)[:60]}")

    conn.close()
    log("\nDone. Verdicts stored in llm_relevant / llm_reasoning; review_status "
        "stays 'pending' until an analyst confirms or rejects.")


def cmd_export(args) -> None:
    if not dbmod.is_postgres_url(args.db) and not os.path.exists(args.db):
        log(f"Database not found: {args.db}")
        sys.exit(1)

    from btr_pipeline.export import export_excel

    conn = dbmod.connect(args.db)
    log(f"Exporting -> {args.out}")
    export_excel(conn, args.out, log=log)
    conn.close()


def cmd_stats(args) -> None:
    if not dbmod.is_postgres_url(args.db) and not os.path.exists(args.db):
        log(f"Database not found: {args.db}")
        sys.exit(1)
    conn = dbmod.connect(args.db)
    s = dbmod.get_stats(conn)
    conn.close()

    log(f"  documents: {s['documents']}   measures: {s['measures']}   "
        f"countries with measures: {s['countries']}")
    log("  by relevance source:")
    for k, v in s["by_relevance"]:
        log(f"    {k:<22s} {v}")
    log("  by review status:")
    for k, v in s["by_review"]:
        log(f"    {k:<22s} {v}")
    log("  by normalised status:")
    for k, v in s["by_status"]:
        log(f"    {k:<22s} {v}")


def cmd_init_db(args) -> None:
    conn = dbmod.connect(args.db)
    dbmod.init_schema(conn)
    conn.close()
    log("Schema is up to date.")


def cmd_copy_db(args) -> None:
    from btr_pipeline.copydb import copy_database

    if not dbmod.is_postgres_url(args.db):
        log("ERROR: copy-db needs a PostgreSQL target: set BTR_DATABASE_URL "
            "or pass --db postgresql://...")
        sys.exit(1)
    if not os.path.exists(args.source):
        log(f"ERROR: source {args.source} not found")
        sys.exit(1)

    log(f"Copying {args.source} -> {dbmod.describe_target(args.db)}")
    src = dbmod.connect(args.source)
    dst = dbmod.connect(args.db)
    try:
        report = copy_database(src, dst, replace=args.replace, log=log)
    except (RuntimeError, ValueError) as exc:
        log(f"ERROR: {exc}")
        sys.exit(1)
    finally:
        src.close()
        dst.close()
    if not report["ok"]:
        log("ERROR: verification failed — do not use the target.")
        sys.exit(1)
    log("Copy verified: all tables identical.")


def cmd_create_app_role(args) -> None:
    import getpass

    from btr_pipeline import pg

    if not dbmod.is_postgres_url(args.db):
        log("ERROR: create-app-role needs a PostgreSQL database (--db postgresql://...)")
        sys.exit(1)
    password = os.environ.get("BTR_APP_DB_PASSWORD") or getpass.getpass(
        f"Password for role {args.role}: ")
    if len(password) < 16:
        log("ERROR: use a password of at least 16 characters")
        sys.exit(1)
    conn = dbmod.connect(args.db)
    created = pg.create_app_role(conn, args.role, password)
    conn.close()
    log(f"Role {args.role} {'created' if created else 'updated'} "
        f"with read/write access to schema {pg.SCHEMA_NAME}.")


def cmd_export_decisions(args) -> None:
    import json

    from btr_pipeline.decisions import export_decisions

    conn = dbmod.connect(args.db, rows_by_name=True)
    dbmod.init_schema(conn)
    data = export_decisions(conn, reviewer=args.by)
    conn.close()

    n = data["counts"]
    if not any(n.values()):
        log(f"No decisions by '{args.by}' in this database. Check the spelling: "
            f"it must be the name exactly as typed in the review app.")
        sys.exit(1)

    out = args.out
    if not out:
        os.makedirs(DECISIONS_DIR, exist_ok=True)
        safe = "".join(ch if ch.isalnum() else "-" for ch in args.by).strip("-")
        # One file per reviewer, overwritten on each export: it always holds
        # all of that reviewer's decisions, and git keeps the history.
        out = os.path.join(DECISIONS_DIR, f"{safe}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)

    log(f"Saved {out}")
    log(f"  relevance decisions: {n['relevance']}")
    log(f"  match reviews:       {n['matches']}")
    log(f"  field corrections:   {n['edits']}")
    log("Next: share this file (see SHARING_DECISIONS.md).")


def cmd_import_decisions(args) -> None:
    import glob
    import json

    from btr_pipeline.decisions import import_decisions

    files: list[str] = []
    for path in args.files or [DECISIONS_DIR]:
        if os.path.isdir(path):
            files += sorted(glob.glob(os.path.join(path, "*.json")))
        elif os.path.exists(path):
            files.append(path)
        else:
            log(f"Not found: {path}")
    if not files:
        log("No decision files to import.")
        sys.exit(1)

    conn = dbmod.connect(args.db, rows_by_name=True)
    dbmod.init_schema(conn)

    labels = {"applied": "added or updated", "unchanged": "already there",
              "outdated": "older than what is there (skipped)",
              "conflict": "CONFLICT (kept existing)", "not_found": "could not be placed"}
    conflicts, problems, need = [], [], []
    total_applied = 0
    for path in files:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        try:
            report = import_decisions(conn, data, dry_run=args.preview)
        except ValueError as exc:
            log(f"{path}: skipped, {exc}")
            continue
        total_applied += report.applied
        log(f"{os.path.basename(path)}")
        for kind, title in (("relevance", "relevance decisions"),
                            ("matches", "match reviews"),
                            ("edits", "field corrections")):
            parts = [f"{v} {labels[k]}" for k, v in report.counts[kind].items() if v]
            if parts:
                log(f"  {title}: " + ", ".join(parts))
        conflicts += report.conflicts
        problems += report.problems
        need += report.need_candidates
    conn.close()

    if conflicts:
        log(f"\n{len(conflicts)} conflict(s): two reviewers decided differently. "
            f"The decision already in the database was kept. Please agree on these "
            f"and correct them in the app (Browse):")
        for c in conflicts:
            log(f"  - {c}")
    if problems:
        log(f"\n{len(problems)} item(s) could not be applied:")
        for p_ in problems:
            log(f"  - {p_}")
    if need:
        log(f"\n{len(need)} measure(s) are now marked transport-relevant but have no "
            f"NDC candidates in this copy yet. Someone with Azure OpenAI access runs: "
            f"python pipeline.py --db <database> match --missing-only")
    if args.preview:
        log("\nPreview only: nothing was changed.")
    else:
        log(f"\nDone: {total_applied} decision(s) added or updated.")


# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--db", default=None,
                        help="SQLite path or postgresql:// URL "
                             "(default: BTR_DATABASE_URL, else data/btr_analysis.db)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_batch = sub.add_parser("batch", help="Ingest all cached xlsx files")
    p_batch.add_argument("--cache", default=DEFAULT_CACHE, help="Cache directory")
    p_batch.add_argument("--force", action="store_true",
                         help="Overwrite documents that carry analyst review work")
    p_batch.set_defaults(func=cmd_batch)

    p_ingest = sub.add_parser("ingest", help="Ingest a single xlsx file")
    p_ingest.add_argument("--file", required=True, help="Path to the xlsx file")
    p_ingest.add_argument("--country", help="Country name (default: from filename)")
    p_ingest.add_argument("--doc-type", default="BTR1", help="Document type (default BTR1)")
    p_ingest.add_argument("--url", help="UNFCCC document page URL")
    p_ingest.add_argument("--force", action="store_true",
                          help="Overwrite even if the document carries analyst review work")
    p_ingest.set_defaults(func=cmd_ingest)

    p_ndc = sub.add_parser("import-ndc", help="Import NDC measures from Tracker export")
    p_ndc.add_argument("--file", default="ndc_tracker_database.xlsx",
                       help="Path to the Tracker database xlsx")
    p_ndc.set_defaults(func=cmd_import_ndc)

    p_embed = sub.add_parser("embed", help="Embed BTR + NDC measures via Azure OpenAI")
    p_embed.set_defaults(func=cmd_embed)

    p_match = sub.add_parser("match", help="Semantic matching BTR -> NDC (top-3)")
    p_match.add_argument("--active-only", action="store_true",
                         help="Match only against Active NDC documents "
                              "(default: all generations incl. archived)")
    p_match.add_argument("--missing-only", action="store_true",
                         help="Only add candidates for relevant measures that "
                              "have none; leave existing matches untouched")
    p_match.set_defaults(func=cmd_match)

    p_classify = sub.add_parser("classify", help="LLM confirmation of pending measures")
    p_classify.add_argument("--limit", type=int, default=500, help="Max measures per run")
    p_classify.set_defaults(func=cmd_classify)

    p_export = sub.add_parser("export", help="Export the database to an Excel workbook")
    p_export.add_argument("--out", default="btr_analysis_export.xlsx",
                          help="Output xlsx path (default btr_analysis_export.xlsx)")
    p_export.set_defaults(func=cmd_export)

    p_stats = sub.add_parser("stats", help="Print database summary")
    p_stats.set_defaults(func=cmd_stats)

    p_init = sub.add_parser("init-db", help="Create or migrate the database schema")
    p_init.set_defaults(func=cmd_init_db)

    p_copy = sub.add_parser("copy-db",
                            help="Copy a SQLite database into PostgreSQL and verify")
    p_copy.add_argument("--from", dest="source", default=DEFAULT_DB,
                        help=f"SQLite source (default {DEFAULT_DB})")
    p_copy.add_argument("--replace", action="store_true",
                        help="Overwrite a target that already holds data")
    p_copy.set_defaults(func=cmd_copy_db)

    p_role = sub.add_parser("create-app-role",
                            help="Create the database login for the hosted app")
    p_role.add_argument("--role", default="btr_app", help="Role name (default btr_app)")
    p_role.set_defaults(func=cmd_create_app_role)

    p_exp = sub.add_parser("export-decisions",
                           help="Save one reviewer's decisions to a file")
    p_exp.add_argument("--by", required=True,
                       help="Reviewer name exactly as typed in the review app")
    p_exp.add_argument("--out", help="File to write (default decisions/<name>.json)")
    p_exp.set_defaults(func=cmd_export_decisions)

    p_imp = sub.add_parser("import-decisions",
                           help="Merge decision files into a database")
    p_imp.add_argument("files", nargs="*",
                       help=f"Decision files or folders (default: {DECISIONS_DIR}/)")
    p_imp.add_argument("--preview", action="store_true",
                       help="Show what would change without changing anything")
    p_imp.set_defaults(func=cmd_import_decisions)

    # .env may carry BTR_DATABASE_URL and the Azure OpenAI credentials
    load_env_file()
    args = parser.parse_args()
    # Sharing decisions is about each reviewer's own working copy: use it
    # unless a database was named explicitly.
    if (args.db is None and not os.environ.get("BTR_DATABASE_URL")
            and args.func in (cmd_export_decisions, cmd_import_decisions)
            and os.path.exists(WORKING_DB)):
        args.db = WORKING_DB
    args.db = dbmod.resolve_target(args.db)
    log(f"Database: {dbmod.describe_target(args.db)}")
    args.func(args)


if __name__ == "__main__":
    main()
