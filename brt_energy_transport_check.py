"""
BRT Energy-Transport Check
===========================
For countries whose Table5 had NO transport entries in the main analysis,
this script finds all rows where the Sector column contains an energy keyword
and checks whether the row's Name + Description text contains transport-related
keywords.

Output is a candidate list for manual review — keyword matching is heuristic
and will have false positives / miss paraphrases.

Input:  btr_transport_results.xlsx  (from brt_transport_analysis.py)
        downloaded_xlsx/            (cached xlsx files)
Output: brt_energy_transport_check.xlsx
"""

import os
import re

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment

# ---------------------------------------------------------------------------
# Configuration — mirrors brt_transport_analysis.py
# ---------------------------------------------------------------------------

RESULTS_FILE = "btr_transport_results.xlsx"
CACHE_DIR = "downloaded_xlsx"
OUTPUT_FILE = "brt_energy_transport_check.xlsx"

COUNTRY_FILENAME_MAP = {
    "Lao People's Democratic Republic": "Lao_PDR",
    "Venezuela (Bolivarian Republic of)": "Venezuela",
    "United Kingdom of Great Britain and Northern Ireland": "United_Kingdom",
}

ENERGY_KEYWORDS = [
    "energy",
    "énergie",      # French
    "energía",      # Spanish
    "energia",      # Portuguese / Italian
    "energetik",    # Russian stem
    "энергетик",    # Russian
    "能源",         # Chinese
]

# Broad transport-in-energy keywords for Name + Description scanning.
# All lowercased; substring matching is used.
TRANSPORT_IN_ENERGY_KEYWORDS = [
    # Generic transport
    "transport", "transportation", "transporte", "транспорт",
    # Vehicles
    "vehicle", "véhicule", "vehículo", "veículo", "fahrzeug",
    "electric vehicle", "ev ", "e-vehicle", "bev", "phev",
    "electromobil", "elektromobil",
    # Charging / fuelling
    "charging station", "charging infrastructure", "recharging",
    "hydrogen refuel", "h2 refuel",
    # Road / rail / aviation / maritime
    "road transport", "road freight", "road passenger",
    "rail", "railway", "train", "tram", "metro", "subway",
    "aviation", "aircraft", "airline", "airport",
    "maritime", "shipping", "vessel", "port",
    # Modes
    "bus", "coach", "truck", "lorry", "car ", "automobile",
    "motorcycle", "bicycle", "cycling", "e-bike",
    "fleet", "fuel efficiency", "fuel economy", "fuel switch",
    # Mobility
    "mobility", "movilidad", "mobilité",
    # Logistics / freight
    "freight", "logistics", "cargo",
]


def log(msg: str) -> None:
    print(msg, flush=True)


def _cached_path(country: str) -> str:
    stem = COUNTRY_FILENAME_MAP.get(country) or re.sub(r"[^\w\-]", "_", country)
    return os.path.join(CACHE_DIR, stem + ".xlsx")


def _is_table5(name: str) -> bool:
    n = name.strip().lower().replace(" ", "")
    for prefix in ("table5", "tableau5", "tabela5"):
        if n == prefix or n.startswith(prefix):
            return True
    if n in ("ctf-five", "ctffive"):
        return True
    return False


def _find_column(sheet, *keywords, max_rows: int = 10) -> tuple[int, str]:
    """
    Scan the first max_rows rows for a header cell whose lowercased text
    contains any of the supplied keywords. Returns (col_index, header_text).
    Returns (-1, "") if not found.
    """
    for row in sheet.iter_rows(max_row=max_rows, values_only=True):
        for col_idx, cell in enumerate(row):
            if cell is not None:
                val = str(cell).lower()
                if any(kw in val for kw in keywords):
                    return col_idx, str(cell)
    return -1, ""


def _has_energy_keyword(cell_value) -> bool:
    val = str(cell_value).lower()
    return any(kw in val for kw in ENERGY_KEYWORDS)


def _transport_matches(text: str) -> list[str]:
    t = text.lower()
    return [kw for kw in TRANSPORT_IN_ENERGY_KEYWORDS if kw in t]


def load_non_transport_countries() -> list[tuple[str, str]]:
    """
    Read brt_transport_results.xlsx and return (country, doc_url) pairs
    where Has Transport = 'No' and Table5 Found = 'Yes'.
    """
    wb = openpyxl.load_workbook(RESULTS_FILE, read_only=True, data_only=True)
    ws = wb["Results"]

    entries = []
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:
            continue  # skip header
        country, has_transport, _, table5_found, doc_url, *_ = row
        if has_transport == "No" and table5_found == "Yes":
            entries.append((str(country), str(doc_url)))

    wb.close()
    return entries


def resolve_path(country: str) -> str | None:
    """Return local xlsx path for a country, trying primary and fallback names."""
    primary = _cached_path(country)
    if os.path.exists(primary):
        return primary
    # Extra-files display names use spaces; filename uses underscores
    alt = os.path.join(CACHE_DIR, country.replace(" ", "_") + ".xlsx")
    return alt if os.path.exists(alt) else None


def write_output(all_flagged: list[dict], country_stats: list[dict]) -> None:
    wb = openpyxl.Workbook()

    # Flagged rows sheet
    ws = wb.active
    ws.title = "Flagged rows"
    headers = ["Country", "Measure name", "Description", "Sector value", "Matched keywords"]
    ws.append(headers)

    header_font = Font(bold=True)
    header_fill = PatternFill("solid", fgColor="FCE4D6")
    for cell in ws[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")

    for r in all_flagged:
        ws.append([r["country"], r["name"], r["description"], r["sector"], r["matched_keywords"]])

    for col in ws.columns:
        max_len = max((len(str(cell.value or "")) for cell in col), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 80)

    # Summary sheet
    ws2 = wb.create_sheet("Summary")
    ws2.append(["Country", "Energy rows checked", "Transport-flagged rows", "Flagged measure names"])
    for cell in ws2[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="FCE4D6")
        cell.alignment = Alignment(horizontal="center")

    for s in sorted(country_stats, key=lambda x: x["country"]):
        names = "; ".join(r["name"][:60] for r in all_flagged if r["country"] == s["country"])
        ws2.append([s["country"], s["energy_rows"], s["flagged"], names or "—"])

    for col in ws2.columns:
        max_len = max((len(str(cell.value or "")) for cell in col), default=10)
        ws2.column_dimensions[col[0].column_letter].width = min(max_len + 4, 80)

    wb.save(OUTPUT_FILE)
    log(f"\nResults saved to: {OUTPUT_FILE}")


def main():
    entries = load_non_transport_countries()
    log(f"Countries to analyse (no transport, Table5 found): {len(entries)}\n")

    all_flagged: list[dict] = []
    country_stats: list[dict] = []

    for country, _doc_url in entries:
        log(f"  {country}")

        local_path = resolve_path(country)
        if local_path is None:
            log(f"    File not found in cache — skipping")
            continue

        try:
            wb = openpyxl.load_workbook(local_path, read_only=True, data_only=True)
        except Exception as e:
            log(f"    Cannot open: {e}")
            continue

        sheet = None
        for name in wb.sheetnames:
            if _is_table5(name):
                sheet = wb[name]
                break

        if sheet is None:
            wb.close()
            continue

        sector_col, _ = _find_column(sheet, "sector", "secteur")
        if sector_col == -1:
            sector_col = 6  # fallback col G

        name_col, _ = _find_column(sheet, "name", "nom", "nombre", "наименование")
        desc_col, _ = _find_column(sheet, "description", "descripción", "описание")

        energy_total = 0
        flagged_rows: list[dict] = []

        for row in sheet.iter_rows(values_only=True):
            if all(cell is None for cell in row):
                continue
            if len(row) <= sector_col or row[sector_col] is None:
                continue

            sector_val = row[sector_col]
            if not _has_energy_keyword(sector_val):
                continue

            energy_total += 1

            row_name = (
                str(row[name_col]).strip()
                if name_col >= 0 and len(row) > name_col and row[name_col] is not None
                else ""
            )
            row_desc = (
                str(row[desc_col]).strip()
                if desc_col >= 0 and len(row) > desc_col and row[desc_col] is not None
                else ""
            )

            matches = _transport_matches(f"{row_name} {row_desc}")
            if matches:
                flagged_rows.append({
                    "country": country,
                    "name": row_name[:120],
                    "description": row_desc[:300],
                    "sector": str(sector_val)[:80],
                    "matched_keywords": ", ".join(sorted(set(matches))),
                })

        wb.close()
        log(f"    energy rows: {energy_total}, flagged: {len(flagged_rows)}")

        all_flagged.extend(flagged_rows)
        country_stats.append({
            "country": country,
            "energy_rows": energy_total,
            "flagged": len(flagged_rows),
        })

    log("\n--- Summary ---")
    with_flags = [s for s in country_stats if s["flagged"] > 0]
    log(f"Countries with at least one flagged energy row: {len(with_flags)} / {len(country_stats)}")
    for s in sorted(with_flags, key=lambda x: x["country"]):
        log(f"  {s['country']:45s}  ({s['flagged']} flagged rows)")

    write_output(all_flagged, country_stats)


if __name__ == "__main__":
    main()
