"""
Data-access layer for btr_analysis.db.

All SQL lives here so the storage backend can be swapped (e.g. Azure PostgreSQL)
by porting a single module. Column types are kept portable: TEXT, INTEGER, REAL.

Tables:
    documents        — one row per ingested source document (country + doc_type unique)
    btr_measures     — one row per in-scope measure, FK to documents
    ndc_quotes       — one row per distinct (country, quote) from the Tracker;
                       carries the embedding and is the semantic-matching target
    ndc_measures     — Tracker classification rows (a quote can carry several
                       taxonomy classifications), FK to ndc_quotes
    measure_matches  — top-K matches, btr_measures x ndc_quotes

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
        reviewed_by         TEXT,
        reviewed_at         TEXT,
        review_note         TEXT,
        match_outcome       TEXT,
        matched_by          TEXT,
        matched_at          TEXT,
        edited_by           TEXT,
        edited_at           TEXT,
        embedding           TEXT
    );
    """,
    # One row per distinct quote text per country. The same quote often carries
    # several taxonomy classifications in the Tracker (rows in ndc_measures) and
    # can appear in several document generations — doc_types / versions /
    # doc_statuses aggregate those as comma-separated lists.
    """
    CREATE TABLE IF NOT EXISTS ndc_quotes (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        country           TEXT NOT NULL,
        quote             TEXT NOT NULL,
        doc_types         TEXT,
        versions          TEXT,
        doc_statuses      TEXT,
        n_classifications INTEGER,
        embedding         BLOB,
        UNIQUE (country, quote)
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
        quote_id                INTEGER REFERENCES ndc_quotes(id)
    );
    """,
    # ndc_id points at ndc_quotes: matching operates on deduplicated quotes,
    # not on individual classification rows.
    # ON DELETE CASCADE on both sides: a match is meaningless once either the
    # BTR measure (document re-ingested) or the NDC quote (Tracker re-imported)
    # is gone. Without it those deletes fail on a foreign-key violation.
    """
    CREATE TABLE IF NOT EXISTS measure_matches (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        btr_id           INTEGER REFERENCES btr_measures(id) ON DELETE CASCADE,
        ndc_id           INTEGER REFERENCES ndc_quotes(id) ON DELETE CASCADE,
        similarity_score REAL,
        rank             INTEGER,
        validated        TEXT,
        validated_by     TEXT,
        validated_at     TEXT,
        -- SET NULL, not CASCADE: re-importing the Tracker rebuilds the
        -- classification rows with new ids, and losing the analyst's taxonomy
        -- choice must not take the confirmed match with it.
        selected_classification_id INTEGER
            REFERENCES ndc_measures(id) ON DELETE SET NULL,
        validation_note  TEXT
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
        ndc_cols = []
        print("  (dropped legacy ndc_measures table — re-run import-ndc to repopulate)")
    # Migrate pre-ndc_quotes layout: classification rows used to carry the
    # embedding themselves; now ndc_quotes does.
    if ndc_cols and "quote_id" not in ndc_cols:
        conn.execute("ALTER TABLE ndc_measures ADD COLUMN quote_id INTEGER")
        print("  (added ndc_measures.quote_id — re-run import-ndc to populate)")
    if ndc_cols and "embedding" in ndc_cols:
        conn.execute("ALTER TABLE ndc_measures DROP COLUMN embedding")
        print("  (dropped ndc_measures.embedding — quote embeddings live in ndc_quotes)")

    mm_cols = [r[1] for r in conn.execute("PRAGMA table_info(measure_matches)")]
    if mm_cols and "validated_by" not in mm_cols:
        conn.execute("DROP TABLE measure_matches")
        mm_cols = []
        print("  (dropped legacy measure_matches table — re-run match to repopulate)")
    # measure_matches used to reference ndc_measures; now it references ndc_quotes
    if mm_cols:
        fks = list(conn.execute("PRAGMA foreign_key_list(measure_matches)"))
        # Legacy layout: ndc_id itself pointed at ndc_measures. Match on the
        # *source column*, not just the target table — selected_classification_id
        # legitimately references ndc_measures in the current schema.
        if any(r[3] == "ndc_id" and r[2] == "ndc_measures" for r in fks):
            conn.execute("DROP TABLE measure_matches")
            mm_cols = []
            print("  (dropped measure_matches with outdated FK — re-run match to repopulate)")
        elif fks and any(r[3] in ("btr_id", "ndc_id") and r[6] != "CASCADE"
                         for r in fks):
            # Older rows lack ON DELETE CASCADE, which makes re-ingesting a
            # document or re-importing the Tracker fail. Rebuild in place,
            # preserving analyst validations.
            _rebuild_measure_matches(conn)

    for stmt in SCHEMA:
        conn.execute(stmt)

    _add_missing_columns(conn)

    conn.execute("DROP VIEW IF EXISTS measures_view")
    conn.execute(MEASURES_VIEW)
    conn.commit()


# Columns added after the initial release. Existing databases are migrated
# in place with ALTER TABLE so review decisions and embeddings are preserved.
ADDED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "btr_measures": [
        ("reviewed_by", "TEXT"),
        ("reviewed_at", "TEXT"),
        ("review_note", "TEXT"),
        ("match_outcome", "TEXT"),
        ("matched_by", "TEXT"),
        ("matched_at", "TEXT"),
        ("edited_by", "TEXT"),
        ("edited_at", "TEXT"),
    ],
    "measure_matches": [
        ("selected_classification_id", "INTEGER"),
        ("validation_note", "TEXT"),
    ],
}


def _rebuild_measure_matches(conn: sqlite3.Connection) -> None:
    """
    Recreate measure_matches with ON DELETE CASCADE, carrying rows over.
    SQLite cannot alter a foreign key in place, so the table is rebuilt.
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(measure_matches)")]
    carried = [c for c in cols if c != "id"]
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("ALTER TABLE measure_matches RENAME TO measure_matches_old")
    for stmt in SCHEMA:
        if "CREATE TABLE IF NOT EXISTS measure_matches" in stmt:
            conn.execute(stmt)
            break
    conn.execute(
        f"INSERT INTO measure_matches (id, {', '.join(carried)}) "
        f"SELECT id, {', '.join(carried)} FROM measure_matches_old"
    )
    conn.execute("DROP TABLE measure_matches_old")
    conn.execute("PRAGMA foreign_keys = ON")
    n = conn.execute("SELECT COUNT(*) FROM measure_matches").fetchone()[0]
    print(f"  (rebuilt measure_matches with ON DELETE CASCADE — {n} rows preserved)")


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, columns in ADDED_COLUMNS.items():
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue  # table absent; CREATE TABLE above already has the columns
        for name, coltype in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {coltype}")
                print(f"  (added {table}.{name})")


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class ReviewedDocumentError(RuntimeError):
    """Raised when re-ingestion would discard existing analyst review work."""


def document_review_count(conn: sqlite3.Connection, doc_id: int) -> int:
    """Number of measures in a document carrying analyst review or edits."""
    return conn.execute(
        """SELECT COUNT(*) FROM btr_measures
           WHERE document_id = ?
             AND (reviewed_by IS NOT NULL OR matched_by IS NOT NULL
                  OR edited_by IS NOT NULL)""",
        (doc_id,),
    ).fetchone()[0]


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
    force: bool = False,
) -> int:
    """
    Insert or update the document row for (country, doc_type) and return its id.
    Existing measures for the document are deleted, making re-ingestion of a
    single document idempotent.

    Re-ingestion discards the document's measures and therefore any analyst
    review decisions attached to them. If the document carries review work,
    a ReviewedDocumentError is raised unless force=True.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    row = conn.execute(
        "SELECT id FROM documents WHERE country = ? AND doc_type = ?",
        (country, doc_type),
    ).fetchone()

    if row:
        doc_id = row[0]
        if not force:
            reviewed = document_review_count(conn, doc_id)
            if reviewed:
                raise ReviewedDocumentError(
                    f"{country}: {reviewed} measure(s) carry analyst review or "
                    f"edits; re-ingesting would discard them. Re-run with "
                    f"--force to overwrite."
                )
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
