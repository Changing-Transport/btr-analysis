"""
BTR extraction pipeline — command-line entry point.

Subcommands:
    batch     Ingest every .xlsx in the cache directory (one-time batch run).
              Re-running is idempotent: each document's measures are replaced.
    ingest    Ingest a single document — the reproducible path for new BTRs:
                  python pipeline.py ingest --file downloaded_xlsx/Chile.xlsx
    classify  LLM confirmation of 'pending' content-match measures via
              Azure OpenAI (requires credentials; see --help).
    stats     Print database summary.

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
from btr_pipeline.extract import extract_measures

DEFAULT_DB = "btr_analysis.db"
DEFAULT_CACHE = "downloaded_xlsx"
RESULTS_XLSX = "btr_transport_results.xlsx"

# UNFCCC listing name -> cache filename stem (same map as brt_transport_analysis.py)
COUNTRY_FILENAME_MAP = {
    "Lao People's Democratic Republic": "Lao_PDR",
    "Venezuela (Bolivarian Republic of)": "Venezuela",
    "United Kingdom of Great Britain and Northern Ireland": "United_Kingdom",
}


def log(msg: str) -> None:
    print(msg, flush=True)


def country_from_filename(fname: str) -> str:
    return os.path.basename(fname).replace(".xlsx", "").replace("_", " ")


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
) -> dict:
    """Extract one file and write document + measures to the DB."""
    res = extract_measures(xlsx_path, country)
    if res["error"]:
        return res

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
    )
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

    for i, fname in enumerate(files, 1):
        path = os.path.join(args.cache, fname)
        country = country_from_filename(fname)
        stem = fname[:-len(".xlsx")]

        res = ingest_one(conn, path, country, doc_url=doc_urls.get(stem))

        if res["error"]:
            log(f"  [{i:3d}/{len(files)}] ERR  {fname}")
            log(f"             -> {res['error']}")
            errors.append(f"{fname}: {res['error']}")
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


def cmd_ingest(args) -> None:
    if not os.path.exists(args.file):
        log(f"ERROR: file not found: {args.file}")
        sys.exit(1)

    country = args.country or country_from_filename(args.file)
    conn = dbmod.connect(args.db)
    dbmod.init_schema(conn)

    log(f"Ingesting {args.file}  (country: {country}, type: {args.doc_type})")
    res = ingest_one(conn, args.file, country, doc_type=args.doc_type, doc_url=args.url)
    conn.close()

    if res["error"]:
        log(f"ERROR: {res['error']}")
        sys.exit(1)

    by_src: dict[str, int] = {}
    for m in res["measures"]:
        by_src[m["relevance_source"]] = by_src.get(m["relevance_source"], 0) + 1
    log(f"  sheet: {res['sheet']}  |  GHG unit: {res['ghg_unit'] or 'not detected'}")
    log(f"  measures stored: {len(res['measures'])}  ({by_src})")
    log(f"  non-transport rows skipped: {res['skipped_rows']}")


def cmd_classify(args) -> None:
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


def cmd_stats(args) -> None:
    if not os.path.exists(args.db):
        log(f"Database not found: {args.db}")
        sys.exit(1)
    conn = dbmod.connect(args.db)
    s = dbmod.get_stats(conn)
    conn.close()

    log(f"Database: {args.db}")
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


# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite database path")
    sub = parser.add_subparsers(dest="command", required=True)

    p_batch = sub.add_parser("batch", help="Ingest all cached xlsx files")
    p_batch.add_argument("--cache", default=DEFAULT_CACHE, help="Cache directory")
    p_batch.set_defaults(func=cmd_batch)

    p_ingest = sub.add_parser("ingest", help="Ingest a single xlsx file")
    p_ingest.add_argument("--file", required=True, help="Path to the xlsx file")
    p_ingest.add_argument("--country", help="Country name (default: from filename)")
    p_ingest.add_argument("--doc-type", default="BTR1", help="Document type (default BTR1)")
    p_ingest.add_argument("--url", help="UNFCCC document page URL")
    p_ingest.set_defaults(func=cmd_ingest)

    p_classify = sub.add_parser("classify", help="LLM confirmation of pending measures")
    p_classify.add_argument("--limit", type=int, default=500, help="Max measures per run")
    p_classify.set_defaults(func=cmd_classify)

    p_stats = sub.add_parser("stats", help="Print database summary")
    p_stats.set_defaults(func=cmd_stats)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
