"""
survey_headers.py
=================
Scans every .xlsx file in downloaded_xlsx/ and, for each file, opens the Table 5
sheet and records the full header row as a CSV-friendly mapping of
column_letter -> cell_value.

The header row is identified as the **first non-empty row that contains at least
one cell with the substring "sector" or "secteur"** (the most reliably
multilingual anchor). If no such row is found within the first 15 rows, the
script falls back to the first non-empty row.

Output: header_survey.csv  (UTF-8 with BOM so Excel opens it correctly)

Columns in output CSV:
    country          – derived from the filename stem
    file             – basename of the xlsx file
    sheet            – name of the Table 5 sheet that was found
    header_row_index – 0-based row index of the identified header row
    A..Z / AA.. etc  – one column per spreadsheet column, named by Excel letter,
                       containing the raw header cell value (truncated to 120 chars)
    error            – populated only when the file could not be processed

Usage:
    python survey_headers.py
"""

import csv
import os
import re
import sys

import openpyxl

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CACHE_DIR = "downloaded_xlsx"
OUTPUT_CSV = "header_survey.csv"

# Search at most this many rows for the header row.
MAX_HEADER_SEARCH_ROWS = 15

# Truncate cell values to this length so the CSV stays readable.
MAX_CELL_LEN = 120


# ---------------------------------------------------------------------------
# Helpers copied / adapted from brt_transport_analysis.py
# ---------------------------------------------------------------------------

def _is_table5(name: str) -> bool:
    """
    Match Table 5 sheet names across English / French / Portuguese / Armenian.
    Mirrors the logic in brt_transport_analysis._is_table5().
    """
    n = name.strip().lower().replace(" ", "")
    for prefix in ("table5", "tableau5", "tabela5"):
        if n == prefix or n.startswith(prefix):
            return True
    # Armenia uses "CTF-Five"
    if n in ("ctf-five", "ctffive"):
        return True
    return False


def _col_letter(col_index: int) -> str:
    """
    Convert a 0-based column index to an Excel-style column letter (A, B, ... Z, AA, ...).
    Only handles up to column ZZ (702 columns) which is more than enough.
    """
    result = ""
    idx = col_index + 1  # make it 1-based for the algorithm
    while idx > 0:
        idx, remainder = divmod(idx - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _identify_header_row(sheet) -> tuple[int, list]:
    """
    Find the most likely header row in the sheet.

    Strategy:
    1. Scan up to MAX_HEADER_SEARCH_ROWS rows.
    2. Return the first row that contains "sector" or "secteur" (case-insensitive).
    3. If none found, fall back to the first non-empty row.

    Returns (row_index, row_values) where row_index is 0-based.
    """
    first_nonempty_idx: int | None = None
    first_nonempty_row: list | None = None

    for row_idx, row in enumerate(sheet.iter_rows(max_row=MAX_HEADER_SEARCH_ROWS,
                                                   values_only=True)):
        if all(cell is None for cell in row):
            continue  # skip entirely blank rows

        # Record the first non-empty row as a fallback
        if first_nonempty_idx is None:
            first_nonempty_idx = row_idx
            first_nonempty_row = list(row)

        # Check whether this row looks like the real header
        for cell in row:
            if cell is not None:
                val = str(cell).lower()
                if "sector" in val or "secteur" in val:
                    return row_idx, list(row)

    # No sector anchor found: fall back to first non-empty row
    if first_nonempty_idx is not None:
        return first_nonempty_idx, first_nonempty_row  # type: ignore[return-value]

    # Sheet appears entirely empty within search range
    return 0, []


# ---------------------------------------------------------------------------
# Core per-file function
# ---------------------------------------------------------------------------

def survey_file(xlsx_path: str) -> dict:
    """
    Open one xlsx file, locate Table 5, identify its header row, and return
    a dict suitable for writing as a CSV row.

    Keys returned:
        country, file, sheet, header_row_index, error
        + one key per column letter (e.g. "A", "B", ...) with the cell value
    """
    fname = os.path.basename(xlsx_path)
    # Derive a human-readable country name from the filename stem
    country = fname.replace(".xlsx", "").replace("_", " ")

    base_record = {"country": country, "file": fname, "sheet": "", "header_row_index": "", "error": ""}

    try:
        wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    except Exception as exc:
        base_record["error"] = f"Cannot open: {exc}"
        return base_record

    # Find the Table 5 sheet
    sheet_name = None
    for name in wb.sheetnames:
        if _is_table5(name):
            sheet_name = name
            break

    if sheet_name is None:
        base_record["error"] = f"No Table5 sheet found. Sheets: {', '.join(wb.sheetnames)}"
        wb.close()
        return base_record

    sheet = wb[sheet_name]
    base_record["sheet"] = sheet_name

    header_row_idx, header_values = _identify_header_row(sheet)
    base_record["header_row_index"] = header_row_idx
    wb.close()

    if not header_values:
        base_record["error"] = "Sheet appears empty within search range"
        return base_record

    # Add one key per column letter for every non-None cell in the header row
    for col_idx, cell_val in enumerate(header_values):
        if cell_val is not None:
            letter = _col_letter(col_idx)
            base_record[letter] = str(cell_val)[:MAX_CELL_LEN]

    return base_record


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not os.path.isdir(CACHE_DIR):
        print(f"ERROR: cache directory '{CACHE_DIR}' not found.", file=sys.stderr)
        sys.exit(1)

    xlsx_files = sorted(
        f for f in os.listdir(CACHE_DIR) if f.lower().endswith(".xlsx")
    )

    if not xlsx_files:
        print(f"No .xlsx files found in '{CACHE_DIR}'.", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(xlsx_files)} xlsx files — scanning Table 5 headers...")

    records: list[dict] = []
    for i, fname in enumerate(xlsx_files, 1):
        path = os.path.join(CACHE_DIR, fname)
        rec = survey_file(path)
        records.append(rec)

        # Progress indicator: show a dot every file, newline every 20
        status = "OK  " if not rec["error"] else "ERR "
        print(f"  [{i:3d}/{len(xlsx_files)}] {status} {fname}")
        if rec["error"]:
            print(f"           -> {rec['error']}")

    # -----------------------------------------------------------------------
    # Determine the full set of column-letter keys that appear across all records
    # (fixed columns first, then A–ZZ in alphabetical order)
    # -----------------------------------------------------------------------
    fixed_cols = ["country", "file", "sheet", "header_row_index", "error"]

    # Collect all dynamic column letters
    letter_keys: set[str] = set()
    for rec in records:
        for k in rec:
            if k not in fixed_cols:
                letter_keys.add(k)

    # Sort Excel column letters properly: single letters before doubles, alphabetically
    def sort_key(letter: str):
        # Sort by length first (so A < Z < AA), then alphabetically within length
        return (len(letter), letter)

    sorted_letters = sorted(letter_keys, key=sort_key)
    all_columns = fixed_cols + sorted_letters

    # -----------------------------------------------------------------------
    # Write CSV
    # -----------------------------------------------------------------------
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=all_columns, extrasaction="ignore")
        writer.writeheader()
        for rec in records:
            writer.writerow(rec)

    print(f"\nDone. Output written to: {OUTPUT_CSV}")
    print(f"  {len(records)} rows  |  {len(sorted_letters)} column-letter fields")
    errors = [r for r in records if r["error"]]
    if errors:
        print(f"\n  {len(errors)} files had errors:")
        for r in errors:
            print(f"    {r['file']}: {r['error']}")


if __name__ == "__main__":
    main()
