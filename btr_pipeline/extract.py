"""
Per-file extraction of Table 5 measures from a CTF-NDC xlsx.

Consolidates the parsing logic previously spread across
brt_transport_analysis.py, extract_btr_measures.py and
brt_energy_transport_check.py:

  * Table 5 sheet detection across naming conventions (Table 5, Tableau 5,
    Tabela 5, CTF-Five, suffix variants)
  * Header-row identification via the "sector" anchor
  * Fuzzy column mapping with footnote stripping
  * GHG sub-column resolution (Achieved / Expected under merged headers)
  * GHG unit detection (kt vs Mt CO2 eq) per document
  * Relevance screening:
      - sector cell contains a transport keyword       -> 'sector_label', auto
      - other-sector row whose text mentions transport -> 'content_match', pending
      - transport-specific submissions (e.g. Lao PDR)  -> 'sector_specific_file', auto
  * Status normalisation to planned / adopted / implemented / expired
"""

import os
import re

import openpyxl

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAX_HEADER_SEARCH_ROWS = 15
GHG_SUBLABEL_LOOKAHEAD = 2

# Sector-cell keywords marking a row as transport-classified (substring match).
TRANSPORT_SECTOR_KEYWORDS = [
    "transport",
    "transporte",
    "транспорт",
    "交通",
    "运输",
]

# Countries whose whole Table 5 is transport-specific (no sector filter).
# Normalised to lowercase; matched against the country name derived from the filename.
TRANSPORT_ALL_ROWS_COUNTRIES = {
    "lao pdr",
    "lao_pdr",
    "lao people s democratic republic",
    "lao people's democratic republic",
}

# Content keywords for rows classified under OTHER sectors (mostly energy) that
# may still affect transport (concept Annex A.2). ASCII keywords are matched on
# word boundaries to avoid substring noise ("bus" in "business", "port" in
# "support"); non-ASCII keywords are matched as plain substrings.
TRANSPORT_CONTENT_KEYWORDS = [
    # Generic
    "transport", "transportation", "transporte", "транспорт",
    "mobility", "movilidad", "mobilité", "mobilidade",
    # Vehicles / electromobility
    "vehicle", "vehicles", "véhicule", "vehículo", "veículo", "fahrzeug",
    "electric vehicle", "ev", "evs", "e-mobility", "electromobility",
    "electromovilidad", "électromobilité", "electromobil", "elektromobil",
    # Charging / fuelling
    "charging station", "charging stations", "charging infrastructure",
    "recharging", "refuelling station", "hydrogen refuelling",
    # Modes
    "rail", "railway", "railways", "train", "trains", "tram", "metro",
    "subway", "bus", "buses", "brt",
    "aviation", "aircraft", "airline", "airport",
    "maritime", "shipping", "vessel", "vessels", "ferry", "port", "ports",
    "road freight", "road passenger", "highway",
    "truck", "trucks", "lorry", "car", "cars", "automobile",
    "motorcycle", "moped", "bicycle", "bicycles", "cycling", "e-bike",
    "walking", "pedestrian",
    # Fleet / freight
    "fleet", "fuel economy", "freight", "logistics", "cargo", "modal shift",
]

# Status normalisation. Patterns are checked in list order; the first bucket
# with a matching substring wins. 'planned' is checked before 'implemented'
# so "implementation planned" resolves to planned.
STATUS_BUCKETS: list[tuple[str, list[str]]] = [
    ("expired", ["expired", "expiré", "discontinued", "ended", "abandon", "cancel"]),
    ("planned", ["plan", "planifi", "prévu", "previst", "запланир",
                 "envisag", "proposed", "under development", "in preparation",
                 "предлага", "not commenced"]),
    ("adopted", ["adopt", "adopté", "adoptad", "принят", "approved",
                 "aprobad", "aprovad", "enacted"]),
    ("implemented", ["implement", "mise en œuvre", "mise en oeuvre",
                     "implementad", "implementaç", "реализ", "выполн",
                     "complete", "concluí", "finaliz", "in force",
                     "operational", "ongoing", "under way", "underway"]),
]

# Fuzzy header-to-field patterns, tried in order (specific before broad).
FIELD_PATTERNS: list[tuple[str, list[str]]] = [
    ("description",         ["description", "descrip"]),
    ("objectives",          ["objective", "objectif"]),
    ("type_of_instrument",  ["instrument"]),
    ("status",              ["status", "statut"]),
    ("sectors",             ["sector", "secteur"]),
    ("ghg_reductions",      ["estimates of ghg", "ghg emission reduction",
                             "estimations des réductions", "estimated ghg emission",
                             "ghg emission savings"]),
    ("gases",               ["gases affected", "ghg affected", "gaz affect",
                             "affected gas"]),
    ("start_year",          ["start year", "année de début",
                             "implementation start year", "anno de"]),
    ("implementing_entity", ["implementing entity", "entité", "mise en oeuvre",
                             "mise en œuvre", "entite"]),
    ("measure_name",        ["name", "nom"]),
]

SKIP_PATTERNS = [
    "no.", "non.", "wem scenario", "spalte",
    "mitigation co-benefit", "narrative information",
]


# ---------------------------------------------------------------------------
# Keyword matching
# ---------------------------------------------------------------------------

def _compile_content_patterns(keywords: list[str]) -> list[tuple[str, re.Pattern | None]]:
    """ASCII keywords get a word-boundary regex; non-ASCII use substring match."""
    compiled = []
    for kw in keywords:
        if kw.isascii():
            compiled.append((kw, re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE)))
        else:
            compiled.append((kw, None))
    return compiled


_CONTENT_PATTERNS = _compile_content_patterns(TRANSPORT_CONTENT_KEYWORDS)


def sector_is_transport(sector_value) -> bool:
    val = str(sector_value).lower()
    return any(kw in val for kw in TRANSPORT_SECTOR_KEYWORDS)


def content_transport_matches(text: str) -> list[str]:
    """Return the transport keywords found in free text (word-boundary aware)."""
    t = text.lower()
    hits = []
    for kw, pattern in _CONTENT_PATTERNS:
        if pattern is not None:
            if pattern.search(t):
                hits.append(kw)
        elif kw in t:
            hits.append(kw)
    return hits


def normalize_status(raw: str) -> str | None:
    """Map a raw status string to planned / adopted / implemented / expired."""
    if not raw:
        return None
    s = raw.strip().lower()
    for bucket, patterns in STATUS_BUCKETS:
        if any(p in s for p in patterns):
            return bucket
    return None


# ---------------------------------------------------------------------------
# Sheet / header / column detection
# ---------------------------------------------------------------------------

def is_table5(name: str) -> bool:
    n = name.strip().lower().replace(" ", "")
    for prefix in ("table5", "tableau5", "tabela5", "tabla5"):
        if n == prefix or n.startswith(prefix):
            return True
    return n in ("ctf-five", "ctffive")


def identify_header_row(rows: list[list]) -> tuple[int, list]:
    """First row containing 'sector'/'secteur' wins; else first non-empty row."""
    first_idx: int | None = None
    first_row: list | None = None
    for row_idx, row in enumerate(rows[:MAX_HEADER_SEARCH_ROWS]):
        if all(cell is None for cell in row):
            continue
        if first_idx is None:
            first_idx = row_idx
            first_row = row
        for cell in row:
            if cell is not None:
                val = str(cell).lower()
                if "sector" in val or "secteur" in val:
                    return row_idx, row
    if first_idx is not None:
        return first_idx, first_row  # type: ignore[return-value]
    return 0, []


def _normalise_header(cell_value) -> str:
    s = str(cell_value).strip().lower()
    s = re.sub(r"[\(\[]\s*[a-z,\s\d]+\s*[\)\]]", "", s)
    s = re.sub(r"\b[a-z](?:,\s*[a-z\d])*\s*$", "", s)
    return " ".join(s.split())


def build_column_map(header_row: list) -> dict[str, list[int]]:
    col_map: dict[str, list[int]] = {}
    for col_idx, cell in enumerate(header_row):
        if cell is None:
            continue
        norm = _normalise_header(cell)
        if not norm or any(pat in norm for pat in SKIP_PATTERNS):
            continue
        for field, patterns in FIELD_PATTERNS:
            if any(pat in norm for pat in patterns):
                col_map.setdefault(field, []).append(col_idx)
                break
    return col_map


def detect_ghg_unit(rows: list[list], header_row_idx: int) -> str | None:
    """
    Detect the GHG reporting unit from the header row and sub-label rows.
    Returns 'Mt CO2 eq', 'kt CO2 eq', or None if not determinable.
    """
    texts = []
    for offset in range(0, GHG_SUBLABEL_LOOKAHEAD + 1):
        idx = header_row_idx + offset
        if idx < len(rows):
            texts.extend(str(c) for c in rows[idx] if c is not None)
    blob = " ".join(texts).lower().replace("₂", "2")
    if re.search(r"\bmt\s*-?\s*co2|\bmtco2", blob):
        return "Mt CO2 eq"
    if re.search(r"\bkt\s*-?\s*co2|\bktco2|\(kt\b", blob):
        return "kt CO2 eq"
    return None


# ---------------------------------------------------------------------------
# GHG sub-column resolution and value parsing
# ---------------------------------------------------------------------------

def resolve_ghg_columns(
    rows: list[list],
    header_row_idx: int,
    ghg_col_indices: list[int],
) -> tuple[int | None, int | None]:
    """Find (achieved, expected) column indices below a merged GHG header."""
    if not ghg_col_indices:
        return None, None

    sub_labels: dict[int, str] = {}
    for offset in range(1, GHG_SUBLABEL_LOOKAHEAD + 1):
        row_idx = header_row_idx + offset
        if row_idx >= len(rows):
            break
        row = rows[row_idx]
        for col_idx in ghg_col_indices:
            if col_idx < len(row) and row[col_idx] is not None:
                val = str(row[col_idx]).strip().lower()
                if val:
                    sub_labels[col_idx] = val

    if not sub_labels:
        if len(ghg_col_indices) == 1:
            return None, ghg_col_indices[0]
        return ghg_col_indices[0], ghg_col_indices[1]

    achieved_col: int | None = None
    expected_candidates: list[tuple[int, int]] = []

    for col_idx in ghg_col_indices:
        label = sub_labels.get(col_idx, "")
        if "achiev" in label:
            if achieved_col is None:
                achieved_col = col_idx
        elif "expect" in label:
            years = re.findall(r"\b(20\d\d)\b", label)
            expected_candidates.append((int(years[0]) if years else 0, col_idx))
        else:
            years = re.findall(r"\b(20\d\d)\b", label)
            if years:
                expected_candidates.append((int(years[0]), col_idx))

    expected_col: int | None = None
    if expected_candidates:
        expected_candidates.sort(key=lambda t: abs(t[0] - 2030))
        expected_col = expected_candidates[0][1]

    if expected_col is None:
        remaining = [c for c in ghg_col_indices if c != achieved_col]
        if remaining:
            expected_col = remaining[0]

    return achieved_col, expected_col


def _cell_text(row: list, col_idx: int | None) -> str:
    if col_idx is None or col_idx >= len(row):
        return ""
    val = row[col_idx]
    return "" if val is None else str(val).strip()


def parse_ghg(raw: str) -> float | None:
    if not raw:
        return None
    cleaned = re.sub(r"\s*\(\d+\)\s*$", "", raw).strip()
    if cleaned.upper() in ("NE", "IE", "NO", "C", "NA", ""):
        return None
    try:
        return float(cleaned)
    except ValueError:
        m = re.search(r"-?\d[\d.,]*(?:e[+-]?\d+)?", cleaned, re.IGNORECASE)
        if m:
            try:
                return float(m.group().replace(",", ""))
            except ValueError:
                pass
    return None


def parse_year(raw: str) -> int | None:
    if not raw:
        return None
    m = re.search(r"\b(19\d\d|20\d\d)\b", raw)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Main extraction entry point
# ---------------------------------------------------------------------------

def extract_measures(xlsx_path: str, country: str) -> dict:
    """
    Parse one CTF-NDC xlsx and return a result dict:

        {
          "error": str,                # non-empty on failure
          "sheet": str | None,
          "header_row_index": int | None,
          "ghg_unit": str | None,
          "measures": list[dict],      # keys match db.MEASURE_COLUMNS
          "skipped_rows": int,         # data rows not transport-relevant
        }
    """
    result = {
        "error": "",
        "sheet": None,
        "header_row_index": None,
        "ghg_unit": None,
        "measures": [],
        "skipped_rows": 0,
    }

    try:
        wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    except Exception as exc:
        result["error"] = f"Cannot open: {exc}"
        return result

    sheet_name = next((n for n in wb.sheetnames if is_table5(n)), None)
    if sheet_name is None:
        result["error"] = f"No Table5 sheet. Available: {', '.join(wb.sheetnames)}"
        wb.close()
        return result
    result["sheet"] = sheet_name

    rows: list[list] = [list(r) for r in wb[sheet_name].iter_rows(values_only=True)]
    wb.close()

    header_row_idx, header_values = identify_header_row(rows)
    if not header_values:
        result["error"] = "Sheet appears empty within search range"
        return result
    result["header_row_index"] = header_row_idx
    result["ghg_unit"] = detect_ghg_unit(rows, header_row_idx)

    col_map = build_column_map(header_values)
    missing = [f for f in ("measure_name", "sectors") if f not in col_map]
    if missing:
        result["error"] = (
            f"Could not locate columns for: {missing}. Header cells: "
            f"{[str(v)[:40] for v in header_values if v is not None]}"
        )
        return result

    ghg_achieved_col, ghg_expected_col = resolve_ghg_columns(
        rows, header_row_idx, col_map.get("ghg_reductions", [])
    )

    sectors_col = col_map["sectors"][0]
    name_col = col_map.get("measure_name", [None])[0]
    desc_col = col_map.get("description", [None])[0]
    obj_col = col_map.get("objectives", [None])[0]
    type_col = col_map.get("type_of_instrument", [None])[0]
    status_col = col_map.get("status", [None])[0]
    gases_col = col_map.get("gases", [None])[0]
    year_col = col_map.get("start_year", [None])[0]
    entity_col = col_map.get("implementing_entity", [None])[0]

    # Skip sub-label rows between the header and the first data row
    data_start_row = header_row_idx + 1
    for offset in range(1, GHG_SUBLABEL_LOOKAHEAD + 1):
        idx = header_row_idx + offset
        if idx >= len(rows):
            break
        sector_val = _cell_text(rows[idx], sectors_col)
        if (
            not sector_val
            or re.fullmatch(r"[\d\s\(\)\-/]+", sector_val)
            or (len(sector_val) < 20 and not any(c.isalpha() for c in sector_val))
        ):
            data_start_row = idx + 1
        else:
            break

    is_sector_specific = country.strip().lower() in TRANSPORT_ALL_ROWS_COUNTRIES

    for row_idx in range(data_start_row, len(rows)):
        row = rows[row_idx]
        if all(cell is None for cell in row):
            continue

        measure_name_val = _cell_text(row, name_col)
        if not measure_name_val:
            continue  # section-header rows, footnotes

        sectors_val = _cell_text(row, sectors_col)

        # Relevance decision
        if is_sector_specific:
            relevance, matched_kw, review = "sector_specific_file", None, "auto"
        elif sector_is_transport(sectors_val):
            relevance, matched_kw, review = "sector_label", None, "auto"
        else:
            text = " ".join(filter(None, (
                measure_name_val, _cell_text(row, desc_col), _cell_text(row, obj_col)
            )))
            hits = content_transport_matches(text)
            if not hits:
                result["skipped_rows"] += 1
                continue
            relevance = "content_match"
            matched_kw = ", ".join(sorted(set(hits)))
            review = "pending"

        status_raw = _cell_text(row, status_col)
        result["measures"].append({
            "measure_name": measure_name_val,
            "description": _cell_text(row, desc_col),
            "objectives": _cell_text(row, obj_col),
            "type_of_instrument": _cell_text(row, type_col),
            "status_raw": status_raw,
            "status_normalized": normalize_status(status_raw),
            "sectors": sectors_val,
            "gases": _cell_text(row, gases_col),
            "start_year": parse_year(_cell_text(row, year_col)),
            "implementing_entity": _cell_text(row, entity_col),
            "ghg_achieved": parse_ghg(_cell_text(row, ghg_achieved_col)),
            "ghg_expected": parse_ghg(_cell_text(row, ghg_expected_col)),
            "source_row": row_idx,
            "relevance_source": relevance,
            "matched_keywords": matched_kw,
            "review_status": review,
        })

    return result
