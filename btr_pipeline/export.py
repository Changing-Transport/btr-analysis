"""
Export the analysis database to a browsable Excel workbook.

One tab per table (embedding BLOBs excluded), plus an enriched Matches tab
where every match row carries the full text of both the BTR measure and the
NDC candidate side by side, so an analyst can compare and validate directly.
"""

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

HEADER_FILL = PatternFill("solid", fgColor="D9E1F2")
MAX_COL_WIDTH = 60
# Long-text columns get a fixed generous width instead of content-based sizing
WIDE_COLUMNS = {
    "description", "objectives", "quote", "btr_description", "btr_objectives",
    "ndc_quote", "measure_name", "btr_measure_name", "purpose", "ndc_purpose",
}

DOCUMENTS_SQL = """
    SELECT id, country, doc_type, source_file, doc_url, table5_sheet,
           header_row_index, ghg_unit, ingested_at
    FROM documents ORDER BY country
"""

BTR_SQL = """
    SELECT m.id, d.country, m.measure_name, m.description, m.objectives,
           m.type_of_instrument, m.status_raw, m.status_normalized, m.sectors,
           m.gases, m.start_year, m.implementing_entity,
           m.ghg_achieved, m.ghg_expected, d.ghg_unit,
           m.relevance_source, m.matched_keywords, m.review_status,
           m.llm_relevant, m.llm_reasoning, m.source_row, d.source_file
    FROM btr_measures m JOIN documents d ON d.id = m.document_id
    ORDER BY d.country, m.id
"""

NDC_SQL = """
    SELECT id, country, country_code, region, doc_type, document_name,
           version, doc_status, doc_date, category, purpose, instrument,
           quote, asi, page, modes, geography, activity_type,
           status_of_measure, parameter, annex, income_group, g20,
           detailed_document_name, tracker_document_id
    FROM ndc_measures ORDER BY country, id
"""

MATCHES_SQL = """
    SELECT
        mm.id            AS match_id,
        d.country        AS btr_country,
        mm.rank,
        mm.similarity_score,
        mm.validated,
        mm.validated_by,
        mm.validated_at,
        b.id             AS btr_id,
        b.measure_name   AS btr_measure_name,
        b.description    AS btr_description,
        b.objectives     AS btr_objectives,
        b.status_raw     AS btr_status,
        b.sectors        AS btr_sectors,
        b.start_year     AS btr_start_year,
        b.relevance_source AS btr_relevance_source,
        n.id             AS ndc_id,
        n.country        AS ndc_country,
        n.quote          AS ndc_quote,
        n.purpose        AS ndc_purpose,
        n.instrument     AS ndc_instrument,
        n.category       AS ndc_category,
        n.asi            AS ndc_asi,
        n.status_of_measure AS ndc_status_of_measure,
        n.doc_type       AS ndc_doc_type,
        n.version        AS ndc_version,
        n.doc_status     AS ndc_doc_status,
        n.page           AS ndc_page
    FROM measure_matches mm
    JOIN btr_measures b ON b.id = mm.btr_id
    JOIN documents d    ON d.id = b.document_id
    JOIN ndc_measures n ON n.id = mm.ndc_id
    ORDER BY d.country, b.id, mm.rank
"""

SHEETS = [
    ("Documents", DOCUMENTS_SQL),
    ("BTR measures", BTR_SQL),
    ("NDC measures", NDC_SQL),
    ("Matches", MATCHES_SQL),
]


def _write_sheet(ws, cursor) -> int:
    headers = [c[0] for c in cursor.description]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center")

    n_rows = 0
    max_lens = [len(h) for h in headers]
    for row in cursor:
        clean = [str(v) if v is not None else "" for v in row]
        ws.append(clean)
        n_rows += 1
        for i, v in enumerate(clean):
            if len(v) > max_lens[i]:
                max_lens[i] = len(v)

    for i, header in enumerate(headers, 1):
        if header.lower() in WIDE_COLUMNS:
            width = MAX_COL_WIDTH
        else:
            width = min(max_lens[i - 1] + 3, MAX_COL_WIDTH)
        ws.column_dimensions[get_column_letter(i)].width = width

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    return n_rows


def export_excel(conn, out_path: str, log=print) -> None:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    for sheet_name, sql in SHEETS:
        ws = wb.create_sheet(sheet_name)
        n = _write_sheet(ws, conn.execute(sql))
        log(f"  {sheet_name}: {n} rows")

    wb.save(out_path)
    log(f"Workbook saved: {out_path}")
