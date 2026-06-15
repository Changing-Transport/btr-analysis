"""
extract_btr_measures.py
=======================
For every .xlsx file in downloaded_xlsx/ that contains a Table 5 sheet:
  1. Identify the header row (sector-anchor heuristic, same as survey_headers.py).
  2. Build a column map by fuzzy-matching each header cell to a canonical field.
  3. Read the sub-row immediately below the header to resolve GHG sub-columns
     (files split GHG into "Achieved" / "Expected" sub-columns under a merged header).
  4. Iterate over data rows; keep only rows whose sector cell contains a transport
     keyword (same logic as brt_transport_analysis.py).
  5. Normalise and insert each measure into the `btr_measures` table of btr_analysis.db.

Output: btr_analysis.db  (SQLite, table btr_measures)

Run: python extract_btr_measures.py [--all]
     --all  include rows from ALL sectors, not just transport-relevant ones
            (useful for a completeness check; default is transport-only)
"""

import argparse
import os
import re
import sqlite3
import sys

import openpyxl

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CACHE_DIR = "downloaded_xlsx"
DB_PATH = "btr_analysis.db"

# How many rows to scan when looking for the header row
MAX_HEADER_SEARCH_ROWS = 15

# How many rows below the identified header row to scan for GHG sub-labels
# (some files have a sub-header row immediately after the main header)
GHG_SUBLABEL_LOOKAHEAD = 2

# Transport keywords (substring match, case-insensitive), aligned with
# brt_transport_analysis.TRANSPORT_KEYWORDS
TRANSPORT_KEYWORDS = [
    "transport",
    "transporte",
    "транспорт",
    "交通",
    "运输",
]

# Countries whose whole Table 5 is transport-specific (no sector filter needed).
# Normalised to lowercase + underscores to match filename stems.
TRANSPORT_ALL_ROWS_COUNTRIES = {
    "lao pdr",
    "lao_pdr",
    "lao people s democratic republic",
    "lao people's democratic republic",
}

# ---------------------------------------------------------------------------
# Fuzzy header-to-field matching
# ---------------------------------------------------------------------------
# Each entry maps a canonical field name to a list of substrings (lowercased).
# The FIRST header cell that contains ANY of the substrings is used.
# Priority: patterns are tried in ORDER listed here, so put more specific
# patterns before broader ones.
#
# Disambiguation notes:
#   "gas" would match "gases affected" AND "GHG emission reductions" — so we
#   check for "ghg emission reductions" FIRST (as its own field) and exclude
#   it from the gases matcher via a negative guard.
#
#   "type" alone would match many things; we require "instrument".

FIELD_PATTERNS: list[tuple[str, list[str]]] = [
    # --- Unambiguous fields ---
    ("description",         ["description", "descrip"]),
    ("objectives",          ["objective", "objectif"]),
    ("type_of_instrument",  ["instrument"]),               # "type of instrument", "type d'instrument"
    ("status",              ["status", "statut"]),
    ("sectors",             ["sector", "secteur"]),
    # "ghg_reductions" must come BEFORE "gases" so "ghg emission" doesn't slip into gases
    ("ghg_reductions",      ["estimates of ghg", "ghg emission reduction",
                              "estimations des réductions", "estimated ghg emission",
                              "ghg emission savings"]),
    ("gases",               ["gases affected", "ghg affected", "gaz affect",
                              "affected gas"]),
    ("start_year",          ["start year", "année de début", "implementation start year",
                              "anno de"]),
    ("implementing_entity", ["implementing entity", "entité", "mise en oeuvre",
                              "mise en œuvre", "entite"]),
    # measure_name is last because "name" is a short substring that can match
    # unexpected things (e.g. "Surname", "Filename").  By this point the more
    # specific fields are already claimed.
    ("measure_name",        ["name", "nom"]),
]

# Header cells that should always be skipped (row-number columns, scenario labels,
# lookup columns that duplicate data, or openpyxl artefacts)
SKIP_PATTERNS = [
    "no.",          # Row-number column (e.g. "No.", "Non.")
    "non.",         # French row-number column
    "wem scenario", # Poland-specific scenario column
    "spalte",       # German openpyxl artefact ("Spalte1" = unnamed column)
    "mitigation co-benefit",  # Supplementary column, not in canonical schema
    "narrative information",  # Supplementary narrative column
]


def _normalise_header(cell_value) -> str:
    """Lowercase, collapse whitespace, strip footnote letters/numbers appended to labels."""
    s = str(cell_value).strip().lower()
    # Remove trailing footnote references like "(j, k)", "j, k", "(g)", "g", "h, 1"
    # These often appear right-glued to the label without space.
    s = re.sub(r"[\(\[]\s*[a-z,\s\d]+\s*[\)\]]", "", s)
    s = re.sub(r"\b[a-z](?:,\s*[a-z\d])*\s*$", "", s)
    return " ".join(s.split())


def _is_skip(normalised: str) -> bool:
    return any(pat in normalised for pat in SKIP_PATTERNS)


def _match_field(normalised: str) -> str | None:
    """Return the canonical field name for a normalised header cell, or None."""
    if _is_skip(normalised):
        return None
    for field, patterns in FIELD_PATTERNS:
        if any(pat in normalised for pat in patterns):
            return field
    return None


# ---------------------------------------------------------------------------
# Column-map builder
# ---------------------------------------------------------------------------

def build_column_map(header_row: list) -> dict[str, list[int]]:
    """
    Given a list of raw cell values (one per column, 0-based), return a dict
    mapping canonical field name → list of column indices.

    Most fields map to a single index; ghg_reductions may map to several
    (one per sub-column in files that split GHG into multiple years/scenarios).
    """
    col_map: dict[str, list[int]] = {}
    for col_idx, cell in enumerate(header_row):
        if cell is None:
            continue
        norm = _normalise_header(cell)
        if not norm:
            continue
        field = _match_field(norm)
        if field:
            col_map.setdefault(field, []).append(col_idx)
    return col_map


# ---------------------------------------------------------------------------
# GHG sub-column resolution
# ---------------------------------------------------------------------------

def resolve_ghg_columns(
    sheet_rows: list[list],
    header_row_idx: int,
    ghg_col_indices: list[int],
) -> tuple[int | None, int | None]:
    """
    Look at up to GHG_SUBLABEL_LOOKAHEAD rows after the header row to find
    sub-column labels for "Achieved" and "Expected" (or year-labelled variants).

    Returns (achieved_col_idx, expected_col_idx), both 0-based or None.

    Logic:
      - A sub-column is labelled "achieved" if its label contains "achiev".
      - A sub-column is labelled "expected" if:
          * it contains "expect", OR
          * it is year-labelled and the year is >= 2029 (treat as 2030 target).
      - If no achieved sub-column exists, achieved_col = None.
      - If no expected sub-column exists but exactly one GHG column exists,
        that column serves as expected.
      - If multiple expected columns, prefer "2030" or the latest year found.
    """
    if not ghg_col_indices:
        return None, None

    # Read sub-label rows
    sub_labels: dict[int, str] = {}  # col_idx → lowercased label
    for offset in range(1, GHG_SUBLABEL_LOOKAHEAD + 1):
        row_idx = header_row_idx + offset
        if row_idx >= len(sheet_rows):
            break
        row = sheet_rows[row_idx]
        for col_idx in ghg_col_indices:
            if col_idx < len(row) and row[col_idx] is not None:
                val = str(row[col_idx]).strip().lower()
                if val:
                    sub_labels[col_idx] = val

    if not sub_labels:
        # No sub-labels found — single column: treat as expected
        if len(ghg_col_indices) == 1:
            return None, ghg_col_indices[0]
        # Multiple columns without labels: take first as achieved, second as expected
        return ghg_col_indices[0], ghg_col_indices[1]

    achieved_col: int | None = None
    expected_candidates: list[tuple[int, int]] = []  # (year_priority, col_idx)

    for col_idx in ghg_col_indices:
        label = sub_labels.get(col_idx, "")
        if "achiev" in label:
            if achieved_col is None:
                achieved_col = col_idx
        elif "expect" in label:
            # Extract year from label if present, else use 0
            years = re.findall(r"\b(20\d\d)\b", label)
            year = int(years[0]) if years else 0
            expected_candidates.append((year, col_idx))
        else:
            # Year-only label (e.g. "2021", "2030")
            years = re.findall(r"\b(20\d\d)\b", label)
            if years:
                year = int(years[0])
                expected_candidates.append((year, col_idx))

    expected_col: int | None = None
    if expected_candidates:
        # Prefer 2030, then nearest to 2030, then latest year
        expected_candidates.sort(key=lambda t: abs(t[0] - 2030))
        expected_col = expected_candidates[0][1]

    # Final fallback: if nothing matched the expected pattern but GHG cols exist
    if expected_col is None and ghg_col_indices:
        remaining = [c for c in ghg_col_indices if c != achieved_col]
        if remaining:
            expected_col = remaining[0]

    return achieved_col, expected_col


# ---------------------------------------------------------------------------
# Table-5 sheet detection (mirrors brt_transport_analysis._is_table5)
# ---------------------------------------------------------------------------

def _is_table5(name: str) -> bool:
    n = name.strip().lower().replace(" ", "")
    for prefix in ("table5", "tableau5", "tabela5"):
        if n == prefix or n.startswith(prefix):
            return True
    return n in ("ctf-five", "ctffive")


# ---------------------------------------------------------------------------
# Header-row identification (mirrors survey_headers._identify_header_row)
# ---------------------------------------------------------------------------

def _identify_header_row(sheet_rows: list[list]) -> tuple[int, list]:
    """
    Return (row_index, row_values) for the most likely header row within the
    first MAX_HEADER_SEARCH_ROWS rows.

    Strategy: first row containing "sector" or "secteur" wins; otherwise fall
    back to the first non-empty row.
    """
    first_nonempty_idx: int | None = None
    first_nonempty_row: list | None = None

    for row_idx, row in enumerate(sheet_rows[:MAX_HEADER_SEARCH_ROWS]):
        if all(cell is None for cell in row):
            continue
        if first_nonempty_idx is None:
            first_nonempty_idx = row_idx
            first_nonempty_row = row
        for cell in row:
            if cell is not None:
                val = str(cell).lower()
                if "sector" in val or "secteur" in val:
                    return row_idx, row

    if first_nonempty_idx is not None:
        return first_nonempty_idx, first_nonempty_row  # type: ignore[return-value]
    return 0, []


# ---------------------------------------------------------------------------
# Transport-keyword check
# ---------------------------------------------------------------------------

def _has_transport_keyword(cell_value) -> bool:
    val = str(cell_value).lower()
    return any(kw in val for kw in TRANSPORT_KEYWORDS)


# ---------------------------------------------------------------------------
# Value extraction helpers
# ---------------------------------------------------------------------------

def _cell_text(row: list, col_idx: int | None) -> str:
    """Return stripped string value of a cell, or empty string."""
    if col_idx is None or col_idx >= len(row):
        return ""
    val = row[col_idx]
    if val is None:
        return ""
    return str(val).strip()


def _parse_ghg(raw: str) -> float | None:
    """
    Parse a GHG value string to a float (kt CO2 eq).

    Handles:
      - Pure numbers: "126.02"
      - Numbers with trailing flags in parentheses: "126.02(0)", "1e-28(1)"
      - IPCC notation: "NE" (not estimated), "IE" (included elsewhere),
        "NO" (not occurring), "C" (confidential), "NA" (not applicable)
      - Empty strings
    """
    if not raw:
        return None
    # Strip trailing bracketed flags like "(0)", "(1)"
    cleaned = re.sub(r"\s*\(\d+\)\s*$", "", raw).strip()
    # Known non-numeric IPCC codes → None
    if cleaned.upper() in ("NE", "IE", "NO", "C", "NA", ""):
        return None
    try:
        return float(cleaned)
    except ValueError:
        # Try extracting the first number-like substring
        m = re.search(r"-?\d[\d.,]*(?:e[+-]?\d+)?", cleaned, re.IGNORECASE)
        if m:
            try:
                return float(m.group().replace(",", ""))
            except ValueError:
                pass
    return None


def _parse_year(raw: str) -> int | None:
    """Extract a 4-digit year from a cell value, or return None."""
    if not raw:
        return None
    m = re.search(r"\b(19\d\d|20\d\d)\b", raw)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# SQLite schema
# ---------------------------------------------------------------------------

CREATE_BTR_MEASURES = """
CREATE TABLE IF NOT EXISTS btr_measures (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    country             TEXT,
    measure_name        TEXT,
    description         TEXT,
    objectives          TEXT,
    type_of_instrument  TEXT,
    status              TEXT,
    sectors             TEXT,
    gases               TEXT,
    start_year          INTEGER,
    implementing_entity TEXT,
    ghg_achieved_kt     REAL,
    ghg_expected_kt     REAL,
    source_file         TEXT,
    source_row          INTEGER,
    embedding           TEXT    -- JSON-serialized float list, populated by embed_and_match.py
);
"""

CREATE_NDC_MEASURES = """
CREATE TABLE IF NOT EXISTS ndc_measures (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    country         TEXT,
    measure_name    TEXT,
    description     TEXT,
    taxonomy_class  TEXT,
    conditionality  TEXT,
    source          TEXT,   -- 'excel_export'
    embedding       TEXT
);
"""

CREATE_MEASURE_MATCHES = """
CREATE TABLE IF NOT EXISTS measure_matches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    btr_id          INTEGER REFERENCES btr_measures(id),
    ndc_id          INTEGER REFERENCES ndc_measures(id),
    similarity_score REAL,
    rank            INTEGER,  -- 1, 2, or 3 (top-3 per BTR measure)
    validated       TEXT      -- NULL / 'confirmed' / 'rejected'
);
"""


def init_db(db_path: str) -> sqlite3.Connection:
    """Create (or open) the SQLite database and ensure all tables exist."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")  # safe for concurrent readers
    conn.execute(CREATE_BTR_MEASURES)
    conn.execute(CREATE_NDC_MEASURES)
    conn.execute(CREATE_MEASURE_MATCHES)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Per-file extraction
# ---------------------------------------------------------------------------

def extract_file(
    xlsx_path: str,
    country: str,
    conn: sqlite3.Connection,
    transport_only: bool = True,
) -> dict:
    """
    Parse one xlsx file and insert transport-relevant Table 5 rows into btr_measures.

    Returns a summary dict with keys:
        sheet_found, rows_inserted, transport_rows_skipped, error
    """
    result = {
        "sheet_found": False,
        "rows_inserted": 0,
        "non_transport_skipped": 0,
        "error": "",
    }

    # ---- Open workbook ----
    try:
        wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    except Exception as exc:
        result["error"] = f"Cannot open: {exc}"
        return result

    # ---- Find Table 5 sheet ----
    sheet_name = None
    for name in wb.sheetnames:
        if _is_table5(name):
            sheet_name = name
            break

    if sheet_name is None:
        result["error"] = (
            f"No Table5 sheet. Available: {', '.join(wb.sheetnames)}"
        )
        wb.close()
        return result

    result["sheet_found"] = True
    sheet = wb[sheet_name]

    # ---- Load all rows into memory (needed for sub-label look-ahead) ----
    # read_only mode returns an iterator; materialise it so we can index by row.
    all_rows: list[list] = [list(row) for row in sheet.iter_rows(values_only=True)]
    wb.close()

    # ---- Identify header row ----
    header_row_idx, header_values = _identify_header_row(all_rows)
    if not header_values:
        result["error"] = "Sheet appears empty within search range"
        return result

    # ---- Build column map ----
    col_map = build_column_map(header_values)

    # Check we found at least the key fields
    missing_critical = [f for f in ("measure_name", "sectors") if f not in col_map]
    if missing_critical:
        result["error"] = (
            f"Could not locate columns for: {missing_critical}. "
            f"Header cells found: "
            f"{[str(v)[:40] for v in header_values if v is not None]}"
        )
        return result

    # ---- Resolve GHG sub-columns ----
    ghg_cols = col_map.get("ghg_reductions", [])
    ghg_achieved_col, ghg_expected_col = resolve_ghg_columns(
        all_rows, header_row_idx, ghg_cols
    )

    # Single GHG column index helpers
    sectors_col = col_map["sectors"][0]
    name_col = col_map.get("measure_name", [None])[0]
    desc_col = col_map.get("description", [None])[0]
    obj_col = col_map.get("objectives", [None])[0]
    type_col = col_map.get("type_of_instrument", [None])[0]
    status_col = col_map.get("status", [None])[0]
    gases_col = col_map.get("gases", [None])[0]
    year_col = col_map.get("start_year", [None])[0]
    entity_col = col_map.get("implementing_entity", [None])[0]

    # Determine from which row data actually starts.
    # Data begins after the header row AND after any sub-label rows.
    # We treat any row where the name/sector cell is still header-like (no
    # transport keyword and very short or looks like a year/label) as metadata.
    data_start_row = header_row_idx + 1
    # Skip sub-label rows: if the sectors cell in the next row does NOT look
    # like a sector value (i.e. it's short and numeric or matches a year), skip.
    for offset in range(1, GHG_SUBLABEL_LOOKAHEAD + 1):
        candidate_idx = header_row_idx + offset
        if candidate_idx >= len(all_rows):
            break
        candidate_row = all_rows[candidate_idx]
        sector_val = _cell_text(candidate_row, sectors_col)
        # If the sector cell is empty or looks like a sub-label (short, no transport
        # keyword, no alphabetic content beyond a year), skip this row.
        if (
            not sector_val
            or re.fullmatch(r"[\d\s\(\)\-/]+", sector_val)  # pure numbers/punctuation
            or (len(sector_val) < 20 and not any(c.isalpha() for c in sector_val))
        ):
            data_start_row = candidate_idx + 1
        else:
            break  # Looks like a real data row; stop advancing

    # Determine whether this is a transport-sector-specific file
    country_norm = country.strip().lower()
    is_transport_only_country = country_norm in TRANSPORT_ALL_ROWS_COUNTRIES

    # ---- Iterate data rows ----
    fname = os.path.basename(xlsx_path)
    insert_sql = """
        INSERT INTO btr_measures
            (country, measure_name, description, objectives, type_of_instrument,
             status, sectors, gases, start_year, implementing_entity,
             ghg_achieved_kt, ghg_expected_kt, source_file, source_row)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    for row_idx in range(data_start_row, len(all_rows)):
        row = all_rows[row_idx]

        # Skip completely empty rows
        if all(cell is None for cell in row):
            continue

        sectors_val = _cell_text(row, sectors_col)

        # Transport filter
        if transport_only and not is_transport_only_country:
            if not _has_transport_keyword(sectors_val):
                result["non_transport_skipped"] += 1
                continue

        # Skip rows that look like section headers rather than data rows
        # (e.g. "Cross-Sector" label rows in Korea/Hungary with no name)
        measure_name_val = _cell_text(row, name_col)
        if not measure_name_val:
            continue

        # Parse GHG values
        ghg_achieved = _parse_ghg(_cell_text(row, ghg_achieved_col))
        ghg_expected = _parse_ghg(_cell_text(row, ghg_expected_col))

        conn.execute(insert_sql, (
            country,
            measure_name_val,
            _cell_text(row, desc_col),
            _cell_text(row, obj_col),
            _cell_text(row, type_col),
            _cell_text(row, status_col),
            sectors_val,
            _cell_text(row, gases_col),
            _parse_year(_cell_text(row, year_col)),
            _cell_text(row, entity_col),
            ghg_achieved,
            ghg_expected,
            fname,
            row_idx,   # 0-based row index within the sheet
        ))
        result["rows_inserted"] += 1

    conn.commit()
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--all",
        action="store_true",
        help="Extract all sectors, not just transport",
    )
    args = parser.parse_args()
    transport_only = not args.all

    if not os.path.isdir(CACHE_DIR):
        print(f"ERROR: cache directory '{CACHE_DIR}' not found.", file=sys.stderr)
        sys.exit(1)

    xlsx_files = sorted(
        f for f in os.listdir(CACHE_DIR) if f.lower().endswith(".xlsx")
    )
    if not xlsx_files:
        print(f"No .xlsx files found in '{CACHE_DIR}'.", file=sys.stderr)
        sys.exit(1)

    mode_label = "transport rows only" if transport_only else "ALL sectors"
    print(f"Extracting BTR measures ({mode_label}) from {len(xlsx_files)} files...")
    print(f"Output database: {DB_PATH}\n")

    conn = init_db(DB_PATH)

    # Clear existing BTR measures so re-runs are idempotent
    deleted = conn.execute("DELETE FROM btr_measures").rowcount
    conn.commit()
    if deleted:
        print(f"  (cleared {deleted} existing rows from btr_measures)\n")

    total_inserted = 0
    errors: list[str] = []

    for i, fname in enumerate(xlsx_files, 1):
        path = os.path.join(CACHE_DIR, fname)
        # Derive country name the same way as survey_headers.py
        country = fname.replace(".xlsx", "").replace("_", " ")

        res = extract_file(path, country, conn, transport_only=transport_only)

        status_tag = "ERR " if res["error"] else "OK  "
        print(
            f"  [{i:3d}/{len(xlsx_files)}] {status_tag} {fname}"
            f"  inserted={res['rows_inserted']}"
            f"  skipped={res['non_transport_skipped']}"
        )
        if res["error"]:
            print(f"             -> {res['error']}")
            errors.append(f"{fname}: {res['error']}")

        total_inserted += res["rows_inserted"]

    conn.close()

    print(f"\nDone.")
    print(f"  Total transport measures inserted: {total_inserted}")
    if errors:
        print(f"\n  {len(errors)} file(s) had errors:")
        for e in errors:
            print(f"    {e}")

    # Quick verification read-back
    conn2 = sqlite3.connect(DB_PATH)
    rows = conn2.execute(
        "SELECT country, COUNT(*) c FROM btr_measures GROUP BY country ORDER BY c DESC"
    ).fetchall()
    conn2.close()
    print(f"\n  Countries with transport measures in DB ({len(rows)} total):")
    for country, count in rows:
        print(f"    {country:<45s} {count:>3d} measures")


if __name__ == "__main__":
    main()
