"""
All SQL used by the review app.

Kept separate from the views so the storage backend can be swapped (Azure
PostgreSQL) without touching the UI, mirroring btr_pipeline/db.py.

Two review stages:
  1. relevance — analysts confirm/reject the `content_match` measures
     (filed under a non-transport sector but mentioning transport)
  2. matching  — analysts judge the top-k NDC quote candidates per BTR measure
"""

import sqlite3
from datetime import datetime, timezone


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ---------------------------------------------------------------------------
# Overview / progress
# ---------------------------------------------------------------------------

def progress(conn) -> dict:
    """Counts driving the landing page."""
    rel_total, rel_done = conn.execute(
        """SELECT COUNT(*),
                  SUM(CASE WHEN review_status IN ('confirmed','rejected') THEN 1 ELSE 0 END)
           FROM btr_measures WHERE relevance_source = 'content_match'"""
    ).fetchone()

    match_total = conn.execute(
        "SELECT COUNT(DISTINCT btr_id) FROM measure_matches"
    ).fetchone()[0]
    match_done = conn.execute(
        "SELECT COUNT(*) FROM btr_measures WHERE match_outcome IS NOT NULL"
    ).fetchone()[0]

    return {
        "relevance_total": rel_total or 0,
        "relevance_done": rel_done or 0,
        "match_total": match_total or 0,
        "match_done": match_done or 0,
        "confirmed": conn.execute(
            "SELECT COUNT(*) FROM measure_matches WHERE validated = 'confirmed'"
        ).fetchone()[0],
        "btr_only": conn.execute(
            "SELECT COUNT(*) FROM btr_measures WHERE match_outcome = 'btr_only'"
        ).fetchone()[0],
    }


def countries(conn, stage: str) -> list[sqlite3.Row]:
    """Countries with outstanding work in the given stage, with open counts."""
    if stage == "relevance":
        sql = """
            SELECT d.country, COUNT(*) AS open
            FROM btr_measures m JOIN documents d ON d.id = m.document_id
            WHERE m.review_status = 'pending'
            GROUP BY d.country ORDER BY d.country
        """
    else:
        sql = """
            SELECT d.country, COUNT(DISTINCT m.id) AS open
            FROM btr_measures m
            JOIN documents d ON d.id = m.document_id
            JOIN measure_matches mm ON mm.btr_id = m.id
            WHERE m.match_outcome IS NULL
            GROUP BY d.country ORDER BY d.country
        """
    return conn.execute(sql).fetchall()


# ---------------------------------------------------------------------------
# Stage 1: relevance screening
# ---------------------------------------------------------------------------

def next_relevance(conn, country: str | None = None) -> sqlite3.Row | None:
    sql = """
        SELECT m.*, d.country, d.source_file, d.doc_url, d.ghg_unit
        FROM btr_measures m JOIN documents d ON d.id = m.document_id
        WHERE m.review_status = 'pending'
    """
    params: list = []
    if country:
        sql += " AND d.country = ?"
        params.append(country)
    sql += " ORDER BY d.country, m.id LIMIT 1"
    return conn.execute(sql, params).fetchone()


def save_relevance(conn, measure_id: int, decision: str, analyst: str,
                   note: str | None = None) -> None:
    """decision is 'confirmed' (transport-relevant) or 'rejected'."""
    conn.execute(
        """UPDATE btr_measures
           SET review_status = ?, reviewed_by = ?, reviewed_at = ?, review_note = ?
           WHERE id = ?""",
        (decision, analyst, now(), note or None, measure_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Stage 2: match validation
# ---------------------------------------------------------------------------

def next_match(conn, country: str | None = None) -> sqlite3.Row | None:
    sql = """
        SELECT m.*, d.country, d.source_file, d.doc_url, d.ghg_unit
        FROM btr_measures m JOIN documents d ON d.id = m.document_id
        WHERE m.match_outcome IS NULL
          AND EXISTS (SELECT 1 FROM measure_matches mm WHERE mm.btr_id = m.id)
    """
    params: list = []
    if country:
        sql += " AND d.country = ?"
        params.append(country)
    sql += " ORDER BY d.country, m.id LIMIT 1"
    return conn.execute(sql, params).fetchone()


def measure(conn, measure_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT m.*, d.country, d.source_file, d.doc_url, d.ghg_unit
           FROM btr_measures m JOIN documents d ON d.id = m.document_id
           WHERE m.id = ?""",
        (measure_id,),
    ).fetchone()


def candidates(conn, measure_id: int) -> list[dict]:
    """
    Top-k NDC quote candidates for a BTR measure, each with the taxonomy
    classifications attached to that quote. A quote can carry several
    genuinely different classifications (~12% do), so they are returned as a
    list for the analyst to choose from when confirming.
    """
    rows = conn.execute(
        """SELECT mm.id AS match_id, mm.rank, mm.similarity_score,
                  mm.validated, mm.selected_classification_id,
                  q.id AS quote_id, q.quote, q.country AS quote_country,
                  q.doc_types, q.versions, q.doc_statuses, q.n_classifications
           FROM measure_matches mm
           JOIN ndc_quotes q ON q.id = mm.ndc_id
           WHERE mm.btr_id = ?
           ORDER BY mm.rank""",
        (measure_id,),
    ).fetchall()

    out = []
    for r in rows:
        classifications = conn.execute(
            """SELECT id, asi, category, purpose, instrument, modes,
                      version, doc_type, doc_status, page
               FROM ndc_measures WHERE quote_id = ?
               ORDER BY id""",
            (r["quote_id"],),
        ).fetchall()
        out.append({"row": r, "classifications": classifications})
    return out


def save_match(conn, measure_id: int, analyst: str,
               decisions: dict[int, str],
               classification_choice: dict[int, int | None],
               notes: dict[int, str | None]) -> str:
    """
    Persist one measure's match review.

    decisions               match_id -> 'confirmed' | 'rejected'
    classification_choice   match_id -> ndc_measures.id (or None)
    notes                   match_id -> free-text note

    Returns the resulting outcome: 'matched' or 'btr_only'.
    """
    stamp = now()
    for match_id, verdict in decisions.items():
        conn.execute(
            """UPDATE measure_matches
               SET validated = ?, validated_by = ?, validated_at = ?,
                   selected_classification_id = ?, validation_note = ?
               WHERE id = ? AND btr_id = ?""",
            (verdict, analyst, stamp,
             classification_choice.get(match_id) if verdict == "confirmed" else None,
             notes.get(match_id) or None,
             match_id, measure_id),
        )

    outcome = "matched" if "confirmed" in decisions.values() else "btr_only"
    conn.execute(
        """UPDATE btr_measures
           SET match_outcome = ?, matched_by = ?, matched_at = ?
           WHERE id = ?""",
        (outcome, analyst, stamp, measure_id),
    )
    conn.commit()
    return outcome


def reopen_match(conn, measure_id: int) -> None:
    """Clear a measure's match review so it returns to the queue."""
    conn.execute(
        """UPDATE measure_matches
           SET validated = NULL, validated_by = NULL, validated_at = NULL,
               selected_classification_id = NULL, validation_note = NULL
           WHERE btr_id = ?""",
        (measure_id,),
    )
    conn.execute(
        """UPDATE btr_measures
           SET match_outcome = NULL, matched_by = NULL, matched_at = NULL
           WHERE id = ?""",
        (measure_id,),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Browse / edit
# ---------------------------------------------------------------------------

# Measure fields an analyst may correct in the UI. Everything else is either
# provenance (source_row, file hash) or derived by the pipeline.
EDITABLE_FIELDS = [
    ("measure_name", "Measure name"),
    ("description", "Description"),
    ("objectives", "Objectives"),
    ("type_of_instrument", "Type of instrument"),
    ("status_raw", "Status (as reported)"),
    ("status_normalized", "Status (normalised)"),
    ("sectors", "Sector(s)"),
    ("gases", "Gases"),
    ("start_year", "Start year"),
    ("implementing_entity", "Implementing entity"),
    ("ghg_achieved", "GHG achieved"),
    ("ghg_expected", "GHG expected"),
]

STATUS_OPTIONS = ["", "planned", "adopted", "implemented", "expired"]

_BROWSE_BASE = """
    SELECT m.id, m.measure_name, m.sectors, m.status_normalized,
           m.relevance_source, m.review_status, m.match_outcome,
           m.reviewed_by, m.matched_by, m.edited_by,
           d.country,
           (SELECT COUNT(*) FROM measure_matches mm
            WHERE mm.btr_id = m.id) AS n_candidates,
           (SELECT COUNT(*) FROM measure_matches mm
            WHERE mm.btr_id = m.id AND mm.validated = 'confirmed') AS n_confirmed
    FROM btr_measures m JOIN documents d ON d.id = m.document_id
"""


def _browse_where(country: str | None, state: str | None,
                  search: str | None) -> tuple[str, list]:
    clauses: list[str] = []
    params: list = []

    if country:
        clauses.append("d.country = ?")
        params.append(country)

    # `state` filters on where the measure sits in the two-stage workflow
    if state == "relevance_pending":
        clauses.append("m.review_status = 'pending'")
    elif state == "relevance_done":
        clauses.append("m.review_status IN ('confirmed','rejected')")
    elif state == "match_pending":
        clauses.append("m.match_outcome IS NULL AND EXISTS "
                       "(SELECT 1 FROM measure_matches mm WHERE mm.btr_id = m.id)")
    elif state == "matched":
        clauses.append("m.match_outcome = 'matched'")
    elif state == "btr_only":
        clauses.append("m.match_outcome = 'btr_only'")
    elif state == "edited":
        clauses.append("m.edited_by IS NOT NULL")

    if search:
        clauses.append("(m.measure_name LIKE ? OR m.description LIKE ?)")
        like = f"%{search}%"
        params += [like, like]

    return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


def browse(conn, country=None, state=None, search=None,
           limit: int = 50, offset: int = 0) -> tuple[list[sqlite3.Row], int]:
    where, params = _browse_where(country, state, search)
    total = conn.execute(
        "SELECT COUNT(*) FROM btr_measures m JOIN documents d "
        "ON d.id = m.document_id" + where, params
    ).fetchone()[0]
    rows = conn.execute(
        _BROWSE_BASE + where + " ORDER BY d.country, m.id LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    return rows, total


def all_countries(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT country FROM documents ORDER BY country")]


def update_fields(conn, measure_id: int, values: dict, analyst: str) -> int:
    """Write edited measure fields. Returns the number of fields changed."""
    current = conn.execute(
        f"SELECT {', '.join(f for f, _ in EDITABLE_FIELDS)} "
        "FROM btr_measures WHERE id = ?", (measure_id,)
    ).fetchone()
    if current is None:
        return 0

    changed: dict[str, object] = {}
    for field, _label in EDITABLE_FIELDS:
        if field not in values:
            continue
        new = values[field]
        new = new.strip() if isinstance(new, str) else new
        if new == "":
            new = None
        if field in ("start_year",):
            new = int(new) if new not in (None, "") else None
        elif field in ("ghg_achieved", "ghg_expected"):
            new = float(new) if new not in (None, "") else None
        if new != current[field]:
            changed[field] = new

    if not changed:
        return 0

    assignments = ", ".join(f"{f} = ?" for f in changed)
    conn.execute(
        f"UPDATE btr_measures SET {assignments}, edited_by = ?, edited_at = ? "
        "WHERE id = ?",
        [*changed.values(), analyst, now(), measure_id],
    )
    conn.commit()
    return len(changed)


def reset_relevance(conn, measure_id: int) -> None:
    """Return a content_match measure to the relevance queue."""
    conn.execute(
        """UPDATE btr_measures
           SET review_status = 'pending', reviewed_by = NULL,
               reviewed_at = NULL, review_note = NULL
           WHERE id = ? AND relevance_source = 'content_match'""",
        (measure_id,),
    )
    conn.commit()


def recent_decisions(conn, limit: int = 15) -> list[sqlite3.Row]:
    """Most recently reviewed measures, for the landing page / undo."""
    return conn.execute(
        """SELECT m.id, m.measure_name, m.match_outcome, m.matched_by, m.matched_at,
                  d.country,
                  (SELECT COUNT(*) FROM measure_matches mm
                   WHERE mm.btr_id = m.id AND mm.validated = 'confirmed') AS n_confirmed
           FROM btr_measures m JOIN documents d ON d.id = m.document_id
           WHERE m.match_outcome IS NOT NULL
           ORDER BY m.matched_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
