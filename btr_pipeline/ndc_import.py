"""
Import NDC/LTS mitigation measures from the GIZ-SLOCAT Tracker database export
(Excel, "Mitigation" sheet) into the ndc_measures table.

Source: Transport Data Commons —
https://ckan.tdc.prod.datopian.com/dataset/449f6dec-6520-4b0c-afa7-391b697feb1e

The sheet has one row per (document, quote) with:
  * document metadata (Document ID, Country, Type of Document, Version, Status, Date)
  * taxonomy (Category, Purpose, Instrument, Parameter, A-S-I, Activity type)
  * the quote text and page number
  * one column per transport mode / geography, marked 'x' where applicable —
    condensed here into comma-separated lists.
"""

from datetime import datetime, timedelta

import openpyxl

SHEET_NAME = "Mitigation"

# Header text (stripped, lowercased) -> ndc_measures column for 1:1 fields
DIRECT_FIELDS = {
    "document id": "tracker_document_id",
    "country code": "country_code",
    "country": "country",
    "region": "region",
    "type of document": "doc_type",
    "document name": "document_name",
    "version number": "version",
    "status": "doc_status",
    "date": "doc_date",
    "category": "category",
    "purpose": "purpose",
    "instrument": "instrument",
    "quote": "quote",
    "a-s-i": "asi",
    "page number": "page",
    "activity type": "activity_type",
    "status of measure": "status_of_measure",
    "parameter": "parameter",
    "annex i or non-annex i": "annex",
    "income group": "income_group",
    "g20": "g20",
    "detailed document name": "detailed_document_name",
}

# Flag-column groups condensed into comma-separated lists.
# Column headers between the group's first and last member (inclusive) that
# carry an 'x' contribute their header text.
MODE_HEADERS = {
    "mode: not defined", "informal transport", "active mobility", "walking",
    "cycling", "road", "two-/three-wheelers", "cars", "private cars", "taxis",
    "truck", "bus", "rail", "heavy rail", "high-speed rail", "transit rail",
    "water", "coastal shipping", "inland shipping", "international maritime",
    "aviation", "domestic aviation", "international aviation",
}
GEOGRAPHY_HEADERS = {
    "geography: not defined", "urban", "rural", "inter-city",
}

INSERT_COLUMNS = list(DIRECT_FIELDS.values()) + ["modes", "geography"]


def _norm_header(h) -> str:
    return str(h).strip().lower() if h is not None else ""


def _excel_date(value) -> str | None:
    """Convert an Excel serial number or datetime to an ISO date string."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    try:
        serial = float(value)
        if 1 < serial < 200000:
            return (datetime(1899, 12, 30) + timedelta(days=serial)).date().isoformat()
    except (ValueError, TypeError):
        pass
    return str(value).strip() or None


def _text(value) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def import_ndc_measures(conn, xlsx_path: str) -> dict:
    """
    Replace the ndc_measures table content with the Mitigation sheet rows.
    Returns {"rows": int, "countries": int, "skipped": int}.
    """
    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    if SHEET_NAME not in wb.sheetnames:
        wb.close()
        raise ValueError(f"Sheet '{SHEET_NAME}' not found. Sheets: {wb.sheetnames}")

    rows_iter = wb[SHEET_NAME].iter_rows(values_only=True)
    raw_header = list(next(rows_iter))
    header = [_norm_header(h) for h in raw_header]

    direct_cols: dict[str, int] = {}   # ndc column -> sheet index
    mode_cols: list[tuple[int, str]] = []
    geo_cols: list[tuple[int, str]] = []
    for idx, h in enumerate(header):
        if h in DIRECT_FIELDS:
            direct_cols.setdefault(DIRECT_FIELDS[h], idx)
        elif h in MODE_HEADERS:
            mode_cols.append((idx, str(raw_header[idx]).strip()))
        elif h in GEOGRAPHY_HEADERS:
            geo_cols.append((idx, str(raw_header[idx]).strip()))

    missing = [c for c in ("country", "quote") if c not in direct_cols]
    if missing:
        wb.close()
        raise ValueError(f"Required columns not found in sheet: {missing}")

    conn.execute("DELETE FROM ndc_measures")

    placeholders = ", ".join("?" for _ in INSERT_COLUMNS)
    sql = f"INSERT INTO ndc_measures ({', '.join(INSERT_COLUMNS)}) VALUES ({placeholders})"

    inserted = 0
    skipped = 0
    countries: set[str] = set()

    for row in rows_iter:
        if all(cell is None for cell in row):
            continue

        values: dict[str, str | None] = {}
        for col, idx in direct_cols.items():
            raw = row[idx] if idx < len(row) else None
            values[col] = _excel_date(raw) if col == "doc_date" else _text(raw)

        if not values.get("country") or not values.get("quote"):
            skipped += 1
            continue

        def flags(cols: list[tuple[int, str]]) -> str | None:
            hits = [
                label for idx, label in cols
                if idx < len(row) and _text(row[idx])
            ]
            return ", ".join(hits) or None

        values["modes"] = flags(mode_cols)
        values["geography"] = flags(geo_cols)

        conn.execute(sql, [values.get(c) for c in INSERT_COLUMNS])
        inserted += 1
        countries.add(values["country"])

    n_quotes = rebuild_quotes(conn)
    conn.commit()
    wb.close()
    return {"rows": inserted, "countries": len(countries),
            "skipped": skipped, "quotes": n_quotes}


def rebuild_quotes(conn) -> int:
    """
    Rebuild ndc_quotes (one row per distinct country+quote) from ndc_measures
    and link the classification rows via quote_id. Existing quote embeddings
    are preserved across re-imports (keyed on country+quote), so only new or
    changed quotes need re-embedding.
    """
    stash = {
        (c, q): e
        for c, q, e in conn.execute(
            "SELECT country, quote, embedding FROM ndc_quotes "
            "WHERE embedding IS NOT NULL"
        )
    }

    conn.execute("DELETE FROM ndc_quotes")
    conn.execute(
        """INSERT INTO ndc_quotes
               (country, quote, doc_types, versions, doc_statuses, n_classifications)
           SELECT country, quote,
                  GROUP_CONCAT(DISTINCT doc_type),
                  GROUP_CONCAT(DISTINCT version),
                  GROUP_CONCAT(DISTINCT doc_status),
                  COUNT(*)
           FROM ndc_measures
           WHERE quote IS NOT NULL
           GROUP BY country, quote"""
    )
    conn.execute(
        """UPDATE ndc_measures SET quote_id =
               (SELECT q.id FROM ndc_quotes q
                WHERE q.country = ndc_measures.country
                  AND q.quote = ndc_measures.quote)"""
    )
    for (country, quote), emb in stash.items():
        conn.execute(
            "UPDATE ndc_quotes SET embedding = ? WHERE country = ? AND quote = ?",
            (emb, country, quote),
        )

    return conn.execute("SELECT COUNT(*) FROM ndc_quotes").fetchone()[0]
