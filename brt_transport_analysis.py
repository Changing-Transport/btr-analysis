"""
BTR Transport Analysis
======================
Scrapes https://unfccc.int/first-biennial-transparency-reports to find all countries
that have submitted CTF-NDC tables, downloads the Excel files, and checks whether
"Transport" appears in column G ("Sector(s)") of the "Table5" sheet.

Requirements (install once):
    pip install playwright openpyxl requests
    playwright install chromium

Output: btr_transport_results.xlsx
Cache:  downloaded_xlsx/  (xlsx files are cached; delete folder to re-download)
"""

import os
import re
import time

import openpyxl
import requests
from playwright.sync_api import sync_playwright, Page

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BTR_PAGE = "https://unfccc.int/first-biennial-transparency-reports"
BASE_URL = "https://unfccc.int"
CACHE_DIR = "downloaded_xlsx"
OUTPUT_FILE = "btr_transport_results.xlsx"
PAGE_DELAY = 2.0
DOWNLOAD_DELAY = 1.0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

# Keywords that indicate transport sector (all lowercased for matching).
# The check uses substring matching, so "Energy,Transport" is caught by "transport".
TRANSPORT_KEYWORDS = [
    "transport",      # English (also French/German)
    "transporte",     # Spanish / Portuguese
    "транспорт",      # Russian
    "交通",           # Chinese (traffic/transport)
    "运输",           # Chinese (freight transport)
]

# Countries whose entire Table5 should be treated as transport-related,
# because the submitted file is sector-specific (transport only).
# Match against lowercased, underscore-normalised country names.
TRANSPORT_ALL_ROWS_COUNTRIES = {
    "lao pdr",
    "lao_pdr",
    "lao people s democratic republic",
    "lao people's democratic republic",
}

# When an UNFCCC listing country name doesn't match the manually saved cache filename,
# add an entry here: UNFCCC name → cache filename stem (without .xlsx).
# This prevents duplicate rows in the output.
COUNTRY_FILENAME_MAP = {
    "Lao People's Democratic Republic":
        "Lao_PDR",
    "Venezuela (Bolivarian Republic of)":
        "Venezuela",
    "United Kingdom of Great Britain and Northern Ireland":
        "United_Kingdom",
}


def log(msg: str) -> None:
    print(msg, flush=True)


def safe_filename(country: str) -> str:
    return re.sub(r"[^\w\-]", "_", country) + ".xlsx"


def cached_path(country: str) -> str:
    """
    Return the expected cache file path for a country.
    Uses COUNTRY_FILENAME_MAP to handle cases where the manually saved filename
    differs from the UNFCCC listing name.
    """
    stem = COUNTRY_FILENAME_MAP.get(country) or re.sub(r"[^\w\-]", "_", country)
    return os.path.join(CACHE_DIR, stem + ".xlsx")


# ---------------------------------------------------------------------------
# Step 1: Scrape the BTR listing page with Playwright
# ---------------------------------------------------------------------------

def get_ctf_ndc_entries(page: Page) -> list[tuple[str, str]]:
    """
    Returns a list of (country_name, document_page_url) for every row that has
    a "CTF tables NDC" / "CTF Tables NDC" link in the Annex column.
    """
    log(f"Loading {BTR_PAGE} ...")
    page.goto(BTR_PAGE, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_selector("table tr td", timeout=30_000)

    rows = page.query_selector_all("table tr")
    entries = []

    for row in rows:
        cells = row.query_selector_all("td")
        if not cells:
            continue

        country = cells[0].inner_text().strip()
        if not country:
            continue

        anchors = row.query_selector_all("a")
        for a in anchors:
            text = (a.inner_text() + " " + (a.get_attribute("title") or "")).lower()
            if "ctf tables ndc" in text or "ctf table ndc" in text:
                href = (a.get_attribute("href") or "").strip()
                if href.startswith("/documents/"):
                    entries.append((country, BASE_URL + href))
                    break

    return entries


# ---------------------------------------------------------------------------
# Step 2: Get the .xlsx download URL from a document page (via Playwright)
# ---------------------------------------------------------------------------

def get_xlsx_url(page: Page, doc_url: str) -> str | None:
    try:
        page.goto(doc_url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_selector("a[href*='.xlsx']", timeout=8_000)
    except Exception:
        return None

    for a in page.query_selector_all("a[href]"):
        href = (a.get_attribute("href") or "").strip()
        if ".xlsx" in href.lower():
            return href if href.startswith("http") else BASE_URL + href

    return None


# ---------------------------------------------------------------------------
# Step 3: Download and cache the xlsx file
# ---------------------------------------------------------------------------

def download_xlsx(page: Page, xlsx_url: str, country: str) -> str | None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    local_path = cached_path(country)

    if os.path.exists(local_path):
        log(f"    [cached]     {local_path}")
        return local_path

    cookies = {c["name"]: c["value"] for c in page.context.cookies()}
    try:
        resp = requests.get(xlsx_url, headers=HEADERS, cookies=cookies, timeout=60)
        resp.raise_for_status()
        with open(local_path, "wb") as f:
            f.write(resp.content)
        log(f"    [downloaded] {local_path}  ({len(resp.content) // 1024} KB)")
        return local_path
    except Exception as e:
        log(f"    ERROR downloading {xlsx_url}: {e}")
        return None


# ---------------------------------------------------------------------------
# Step 4: Inspect Table5 — detect sector column dynamically, check for transport
# ---------------------------------------------------------------------------

def _is_table5(name: str) -> bool:
    """
    Match sheet names for Table 5 across naming conventions:
      - English:    Table5, Table 5, Table5_CG, Table 5_CG, Table 5_BTR ...
      - French:     Tableau 5, Tableau5, Tableau 5_...
      - Portuguese: Tabela 5, Tabela5, Tabela 5_...
    Strips leading/trailing whitespace and collapses internal spaces before matching.
    """
    # Remove leading/trailing whitespace, lowercase, then collapse spaces
    n = name.strip().lower().replace(" ", "")
    for prefix in ("table5", "tableau5", "tabela5"):
        if n == prefix or n.startswith(prefix):
            return True
    # Armenia uses "CTF-Five" for Table 5
    if n in ("ctf-five", "ctffive"):
        return True
    return False


def _has_transport_keyword(cell_value) -> bool:
    """Return True if the cell value contains any transport keyword (substring match)."""
    val = str(cell_value).lower()
    return any(kw in val for kw in TRANSPORT_KEYWORDS)


def _find_sector_column(sheet) -> tuple[int, str]:
    """
    Scan the first 10 rows of the sheet for the header cell whose text contains
    'sector' or 'secteur' (French). Returns (column_index, header_text).
    Falls back to column G (index 6, header 'fallback') if not found.
    """
    for row in sheet.iter_rows(max_row=10, values_only=True):
        for col_idx, cell in enumerate(row):
            if cell is not None:
                val = str(cell).lower()
                if "sector" in val or "secteur" in val:
                    return col_idx, str(cell)
    return 6, "fallback (col G)"


def check_transport(local_path: str, country: str = "") -> dict:
    """
    Opens the xlsx and checks the Table5 sheet.
    Dynamically locates the 'Sector(s) affected' column rather than assuming
    column G — handles files where columns are shifted (e.g. Poland uses col I).

    Special case: if the country is flagged as transport-only (sector-specific file),
    all data rows in Table5 count as transport rows.
    """
    result = {
        "sheet_found": False,
        "transport_rows": 0,
        "has_transport": False,
        "note": "",
    }

    is_transport_only = country.strip().lower() in TRANSPORT_ALL_ROWS_COUNTRIES

    try:
        wb = openpyxl.load_workbook(local_path, read_only=True, data_only=True)
    except Exception as e:
        result["note"] = f"Cannot open file: {e}"
        return result

    sheet = None
    for name in wb.sheetnames:
        if _is_table5(name):
            sheet = wb[name]
            break

    if sheet is None:
        result["note"] = (
            f"Sheet 'Table5' not found. "
            f"Available: {', '.join(wb.sheetnames)}"
        )
        wb.close()
        return result

    result["sheet_found"] = True

    # Locate the sector column dynamically
    sector_col, sector_header = _find_sector_column(sheet)
    col_letter = chr(65 + sector_col) if sector_col < 26 else f"col{sector_col}"

    transport_count = 0
    data_rows = 0

    for row in sheet.iter_rows(values_only=True):
        if all(cell is None for cell in row):
            continue

        if is_transport_only:
            # Sector-specific file: every non-empty row counts as transport
            if len(row) > sector_col and row[sector_col] is not None:
                data_rows += 1
        else:
            if len(row) > sector_col and row[sector_col] is not None:
                if _has_transport_keyword(row[sector_col]):
                    transport_count += 1

    if is_transport_only:
        result["transport_rows"] = data_rows
        result["has_transport"] = data_rows > 0
        result["note"] = (
            f"Sector-specific file (transport): all Table5 rows counted "
            f"[sector col={col_letter}: '{sector_header}']"
        )
    else:
        result["transport_rows"] = transport_count
        result["has_transport"] = transport_count > 0
        if sector_header == "fallback (col G)":
            result["note"] = "Sector column header not found; fell back to col G"

    wb.close()
    return result


# ---------------------------------------------------------------------------
# Step 5: Write results to Excel
# ---------------------------------------------------------------------------

def write_results(results: list[dict]) -> None:
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Results"

    headers = [
        "Country", "Has Transport", "Transport Row Count",
        "Table5 Found", "Document Page", "Notes",
    ]
    ws.append(headers)

    header_font = Font(bold=True)
    header_fill = PatternFill("solid", fgColor="D9E1F2")
    for cell in ws[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")

    for r in sorted(results, key=lambda x: x["country"]):
        ws.append([
            r["country"],
            "Yes" if r["has_transport"] else "No",
            r["transport_rows"],
            "Yes" if r["sheet_found"] else "No",
            r["doc_url"],
            r["note"],
        ])

    for col in ws.columns:
        max_len = max((len(str(cell.value or "")) for cell in col), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 60)

    # Summary sheet
    ws2 = wb.create_sheet("Summary")
    with_transport = [r for r in results if r["has_transport"]]
    no_transport   = [r for r in results if r["sheet_found"] and not r["has_transport"]]
    no_table       = [r for r in results if not r["sheet_found"]]

    ws2.append(["Metric", "Count"])
    ws2.append(["Total countries analysed", len(results)])
    ws2.append(["Countries WITH Transport in Table5", len(with_transport)])
    ws2.append(["Countries WITHOUT Transport in Table5", len(no_transport)])
    ws2.append(["Countries where Table5 not found / not retrieved", len(no_table)])
    ws2.append([])
    ws2.append(["Countries with Transport (alphabetical):"])
    for r in sorted(with_transport, key=lambda x: x["country"]):
        ws2.append([r["country"], r["transport_rows"], "transport-relevant rows"])

    wb.save(OUTPUT_FILE)
    log(f"\nResults saved to: {OUTPUT_FILE}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    results = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()

        # Phase 1: get official country list from UNFCCC page
        entries = get_ctf_ndc_entries(page)
        if not entries:
            log("No CTF-NDC entries found — page structure may have changed.")
            browser.close()
            return

        log(f"\nFound {len(entries)} countries with CTF-NDC tables on the UNFCCC page.\n")

        # Phase 2-4: process each country (cache-first to minimise live requests)
        processed_files = set()

        for i, (country, doc_url) in enumerate(entries, 1):
            log(f"[{i}/{len(entries)}] {country}")
            local_path = cached_path(country)
            # Register both the mapped filename and the auto-generated one as processed
            # so the extra-files scan doesn't pick up stale downloads under the old name
            stem = COUNTRY_FILENAME_MAP.get(country) or re.sub(r"[^\w\-]", "_", country)
            processed_files.add(stem + ".xlsx")
            processed_files.add(re.sub(r"[^\w\-]", "_", country) + ".xlsx")

            if not os.path.exists(local_path):
                time.sleep(PAGE_DELAY)
                xlsx_url = get_xlsx_url(page, doc_url)

                if not xlsx_url:
                    log(f"    No xlsx URL found on {doc_url}")
                    results.append({
                        "country": country, "doc_url": doc_url,
                        "sheet_found": False, "transport_rows": 0,
                        "has_transport": False,
                        "note": "xlsx URL not found on document page",
                    })
                    continue

                local_path_dl = download_xlsx(page, xlsx_url, country)
                if not local_path_dl:
                    results.append({
                        "country": country, "doc_url": doc_url,
                        "sheet_found": False, "transport_rows": 0,
                        "has_transport": False, "note": "Download failed",
                    })
                    time.sleep(DOWNLOAD_DELAY)
                    continue

                local_path = local_path_dl
                time.sleep(DOWNLOAD_DELAY)
            else:
                log(f"    [cached]     {local_path}")

            check = check_transport(local_path, country)
            results.append({"country": country, "doc_url": doc_url, **check})

            status = "TRANSPORT FOUND" if check["has_transport"] else "no transport"
            log(f"    [{status}] Table5={check['sheet_found']}, rows={check['transport_rows']}")
            if check["note"]:
                log(f"    Note: {check['note']}")

        browser.close()

    # Phase 2b: pick up any manually added cache files not in the UNFCCC listing
    log("\n--- Checking cache for manually added files not in the UNFCCC listing ---")
    if os.path.isdir(CACHE_DIR):
        for fname in sorted(os.listdir(CACHE_DIR)):
            if not fname.endswith(".xlsx"):
                continue
            if fname in processed_files:
                continue
            # Derive a display name from the filename
            country_display = fname.replace(".xlsx", "").replace("_", " ")
            local_path = os.path.join(CACHE_DIR, fname)
            log(f"[extra] {country_display}")

            check = check_transport(local_path, fname.replace(".xlsx", ""))
            results.append({
                "country": country_display,
                "doc_url": "manually added",
                **check,
            })

            status = "TRANSPORT FOUND" if check["has_transport"] else "no transport"
            log(f"    [{status}] Table5={check['sheet_found']}, rows={check['transport_rows']}")
            if check["note"]:
                log(f"    Note: {check['note']}")

    # Phase 5: write output
    write_results(results)

    with_transport = [r for r in results if r["has_transport"]]
    log(f"\n{'='*55}")
    log(f"SUMMARY: {len(with_transport)} / {len(results)} countries have Transport in Table5")
    log("="*55)
    for r in sorted(with_transport, key=lambda x: x["country"]):
        log(f"  {r['country']:45s}  ({r['transport_rows']} rows)")


if __name__ == "__main__":
    main()
