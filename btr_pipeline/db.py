"""
Data-access layer for btr_analysis.db.

All SQL lives here so the storage backend can be swapped (e.g. Azure PostgreSQL)
by porting a single module. Column types are kept portable: TEXT, INTEGER, REAL.

Tables:
    documents        — one row per ingested source document (country + doc_type unique)
    btr_measures     — one row per in-scope measure, FK to documents
    ndc_measures     — reserved for the future NDC import (matching step deferred)
    measure_matches  — reserved for the future semantic-matching step

View:
    measures_view    — btr_measures joined with its document metadata, for
                       consumers (analysis notebooks, the review UI in the
                       climate-policy-miner app) that want flat rows.
"""

import hashlib
import os
import sqlite3
from datetime import datetime, timezone

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS documents (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        country          TEXT NOT NULL,
        doc_type         TEXT NOT NULL DEFAULT 'BTR1',
        source_file      TEXT NOT NULL,
        file_hash        TEXT,
        doc_url          TEXT,
        table5_sheet     TEXT,
        header_row_index INTEGER,
        ghg_unit         TEXT,
        ingested_at      TEXT,
        UNIQUE (country, doc_type)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS btr_measures (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id         INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        measure_name        TEXT,
        description         TEXT,
        objectives          TEXT,
        type_of_instrument  TEXT,
        status_raw          TEXT,
        status_normalized   TEXT,
        sectors             TEXT,
        gases               TEXT,
        start_year          INTEGER,
        implementing_entity TEXT,
        ghg_achieved        REAL,
        ghg_expected        REAL,
        source_row          INTEGER,
        relevance_source    TEXT NOT NULL,
        matched_keywords    TEXT,
        review_status       TEXT NOT NULL DEFAULT 'auto',
        llm_relevant        TEXT,
        llm_reasoning       TEXT,
        embedding           TEXT
    );
    """,
    # Mirrors the "Mitigation" sheet of the GIZ-SLOCAT Tracker database export.
    # The per-mode / per-geography 'x' flag columns of the export are condensed
    # into comma-separated lists (modes, geography).
    """
    CREATE TABLE IF NOT EXISTS ndc_measures (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        tracker_document_id     TEXT,
        country_code            TEXT,
        country                 TEXT,
        region                  TEXT,
        doc_type                TEXT,
        document_name           TEXT,
        version                 TEXT,
        doc_status              TEXT,
        doc_date                TEXT,
        category                TEXT,
        purpose                 TEXT,
        instrument              TEXT,
        quote                   TEXT,
        asi                     TEXT,
        page                    TEXT,
        modes                   TEXT,
        geography               TEXT,
        activity_type           TEXT,
        status_of_measure       TEXT,
        parameter               TEXT,
        annex                   TEXT,
        income_group            TEXT,
        g20                     TEXT,
        detailed_document_name  TEXT,
        embedding               TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS measure_matches (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        btr_id           INTEGER REFERENCES btr_measures(id),
        ndc_id           INTEGER REFERENCES ndc_measures(id),
        similarity_score REAL,
        rank             INTEGER,
        validated        TEXT,
        validated_by     TEXT,
        validated_at     TEXT
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_measures_document ON btr_measures(document_id);",
    "CREATE INDEX IF NOT EXISTS idx_documents_country ON documents(country);",
]

MEASURES_VIEW = """
CREATE VIEW measures_view AS
SELECT
    m.*,
    d.country,
    d.doc_type,
    d.source_file,
    d.doc_url,
    d.ghg_unit
FROM btr_measures m
JOIN documents d ON d.id = m.document_id;
"""

# Column order used by insert_measures; keys of the measure dicts from extract.py
MEASURE_COLUMNS = [
    "measure_name", "description", "objectives", "type_of_instrument",
    "status_raw", "status_normalized", "sectors", "gases", "start_year",
    "implementing_entity", "ghg_achieved", "ghg_expected", "source_row",
    "relevance_source", "matched_keywords", "review_status",
]


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """
    Create all tables. If a legacy btr_measures table (pre-documents schema,
    no document_id column) is found, drop it — its content is fully
    regenerable from the cached xlsx files via `pipeline.py batch`.
    """
    existing_cols = [r[1] for r in conn.execute("PRAGMA table_info(btr_measures)")]
    if existing_cols and "document_id" not in existing_cols:
        conn.execute("DROP TABLE btr_measures")
        print("  (dropped legacy btr_measures table — re-run batch to repopulate)")

    ndc_cols = [r[1] for r in conn.execute("PRAGMA table_info(ndc_measures)")]
    if ndc_cols and "quote" not in ndc_cols:
        conn.execute("DROP TABLE ndc_measures")
        print("  (dropped legacy ndc_measures table — re-run import-ndc to repopulate)")

    mm_cols = [r[1] for r in conn.execute("PRAGMA table_info(measure_matches)")]
    if mm_cols and "validated_by" not in mm_cols:
        conn.execute("DROP TABLE measure_matches")
        print("  (dropped legacy measure_matches table — re-run match to repopulate)")

    for stmt in SCHEMA:
        conn.execute(stmt)

    conn.execute("DROP VIEW IF EXISTS measures_view")
    conn.execute(MEASURES_VIEW)
    conn.commit()


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def upsert_document(
    conn: sqlite3.Connection,
    country: str,
    source_file: str,
    doc_type: str = "BTR1",
    file_hash: str | None = None,
    doc_url: str | None = None,
    table5_sheet: str | None = None,
    header_row_index: int | None = None,
    ghg_unit: str | None = None,
) -> int:
    """
    Insert or update the document row for (country, doc_type) and return its id.
    Existing measures for the document are deleted, making re-ingestion of a
    single document idempotent.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    row = conn.execute(
        "SELECT id FROM documents WHERE country = ? AND doc_type = ?",
        (country, doc_type),
    ).fetchone()

    if row:
        doc_id = row[0]
        conn.execute(
            """UPDATE documents SET source_file = ?, file_hash = ?, doc_url = ?,
               table5_sheet = ?, header_row_index = ?, ghg_unit = ?, ingested_at = ?
               WHERE id = ?""",
            (source_file, file_hash, doc_url, table5_sheet,
             header_row_index, ghg_unit, now, doc_id),
        )
        conn.execute("DELETE FROM btr_measures WHERE document_id = ?", (doc_id,))
    else:
        cur = conn.execute(
            """INSERT INTO documents
               (country, doc_type, source_file, file_hash, doc_url,
                table5_sheet, header_row_index, ghg_unit, ingested_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (country, doc_type, source_file, file_hash, doc_url,
             table5_sheet, header_row_index, ghg_unit, now),
        )
        doc_id = cur.lastrowid

    return doc_id


def insert_measures(conn: sqlite3.Connection, document_id: int, measures: list[dict]) -> int:
    if not measures:
        return 0
    placeholders = ", ".join("?" for _ in MEASURE_COLUMNS)
    sql = (
        f"INSERT INTO btr_measures (document_id, {', '.join(MEASURE_COLUMNS)}) "
        f"VALUES (?, {placeholders})"
    )
    for m in measures:
        conn.execute(sql, (document_id, *[m.get(c) for c in MEASURE_COLUMNS]))
    return len(measures)


def get_stats(conn: sqlite3.Connection) -> dict:
    stats = {}
    stats["documents"] = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    stats["measures"] = conn.execute("SELECT COUNT(*) FROM btr_measures").fetchone()[0]
    stats["by_relevance"] = conn.execute(
        "SELECT relevance_source, COUNT(*) FROM btr_measures GROUP BY relevance_source"
    ).fetchall()
    stats["by_review"] = conn.execute(
        "SELECT review_status, COUNT(*) FROM btr_measures GROUP BY review_status"
    ).fetchall()
    stats["by_status"] = conn.execute(
        "SELECT COALESCE(status_normalized, '(unmapped)'), COUNT(*) "
        "FROM btr_measures GROUP BY status_normalized ORDER BY COUNT(*) DESC"
    ).fetchall()
    stats["countries"] = conn.execute(
        "SELECT COUNT(DISTINCT country) FROM documents "
        "WHERE id IN (SELECT DISTINCT document_id FROM btr_measures)"
    ).fetchone()[0]
    return stats
