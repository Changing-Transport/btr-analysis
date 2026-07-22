"""
Generate analyst validation workbooks (one per colleague), each covering a set
of BTR documents. Each workbook has an Instructions sheet and a grouped
"Matches to validate" sheet with dropdown validation columns.

Run from the repository root:
    python validation_samples/generate_samples.py
"""
import os
import sqlite3

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

DB = os.path.join("data", "btr_analysis.db")
OUT_DIR = "validation_samples"

GROUPS = {
    "Group1": ["Malawi", "Oman", "Jordan", "South Africa", "Malta"],
    "Group2": ["Singapore", "Trinidad and Tobago", "Thailand",
               "United Arab Emirates", "Morocco"],
    "Group3": ["Côte D'Ivoire", "Mauritius", "Paraguay",
               "Lao People’s Democratic Republic", "Egypt"],
}

QUERY = """
    SELECT mm.id, d.country, mm.btr_id, b.measure_name, b.description,
           b.status_raw, b.sectors, mm.rank, mm.similarity_score,
           nq.quote, nq.versions, cl.asi, cl.categories, cl.purposes
    FROM measure_matches mm
    JOIN btr_measures b ON b.id = mm.btr_id
    JOIN documents d    ON d.id = b.document_id
    JOIN ndc_quotes nq  ON nq.id = mm.ndc_id
    LEFT JOIN (
        SELECT quote_id,
               GROUP_CONCAT(DISTINCT asi)      AS asi,
               GROUP_CONCAT(DISTINCT category) AS categories,
               GROUP_CONCAT(DISTINCT purpose)  AS purposes
        FROM ndc_measures GROUP BY quote_id
    ) cl ON cl.quote_id = nq.id
    WHERE d.country = ?
    ORDER BY b.id, mm.rank
"""

COLUMNS = [
    ("match_id", 9), ("country", 16), ("btr_id", 8),
    ("btr_measure", 34), ("btr_description", 50), ("btr_status", 14),
    ("btr_sectors", 16), ("rank", 6), ("ai_similarity", 11),
    ("ndc_quote", 55), ("ndc_generation", 16), ("ndc_asi", 12),
    ("ndc_category", 20), ("ndc_purpose", 26),
    ("VALID? (yes/no/unsure)", 18), ("comment", 30),
]
VALID_COL = 15   # 1-based index of "VALID?"
COMMENT_COL = 16

HEADER_FILL = PatternFill("solid", fgColor="305496")
HEADER_FONT = Font(bold=True, color="FFFFFF", size=10)
INPUT_FILL = PatternFill("solid", fgColor="FFF2CC")      # analyst-fill columns
GROUP_FILL = PatternFill("solid", fgColor="F2F2F2")      # alternate measure shading
THIN = Side(style="thin", color="D0D0D0")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

INSTRUCTIONS = [
    ("title", "BTR ↔ NDC match validation"),
    ("spacer", ""),
    ("field", "Validator name:  ____________________________"),
    ("field", "Documents in this file:  {countries}"),
    ("spacer", ""),
    ("h", "What this is"),
    ("p", "Each row on the 'Matches to validate' tab is an AI-suggested link between "
          "a measure a country reports having IMPLEMENTED in its Biennial Transparency "
          "Report (BTR) and a commitment from its NDC / long-term strategy. We need you "
          "to judge whether the suggestion is correct."),
    ("spacer", ""),
    ("h", "How to fill it in"),
    ("p", "• Each BTR measure has up to 3 suggested NDC matches (rank 1–3), shaded "
          "together as one block."),
    ("p", "• In the yellow 'VALID?' column choose: yes = the NDC quote refers to the "
          "same measure/commitment; no = it does not; unsure = you can't tell or want "
          "to discuss."),
    ("p", "• More than one 'yes' per block is fine if the BTR measure genuinely "
          "corresponds to several NDC commitments."),
    ("p", "• If none of the 3 fit, mark all three 'no'. That is a real and useful "
          "result: the measure is being implemented but was not in the NDC "
          "(a 'BTR-only' measure)."),
    ("p", "• Use 'comment' for anything worth noting: a partial match, a better quote "
          "you know exists, a wrong sector, etc."),
    ("spacer", ""),
    ("h", "What to compare"),
    ("p", "• Read the BTR measure (name + description) against the NDC quote."),
    ("p", "• 'ai_similarity' is the model's text-similarity score (higher = more "
          "similar wording). It is a hint, not the answer — trust your judgement."),
    ("p", "• The same NDC statement sometimes appears twice, once short and once "
          "extended (see the quote text). Judge each on its own."),
    ("p", "• 'ndc_generation' shows which NDC/LTS version the quote is from; matches "
          "may come from older generations, which is expected."),
    ("spacer", ""),
    ("h", "Practical notes"),
    ("p", "• Please don't re-sort the sheet before saving — it keeps the rank 1–3 rows "
          "grouped. If you must, sort by btr_id then rank."),
    ("p", "• Only the two yellow columns need your input. Everything else is reference."),
    ("p", "• Save and send the file back when done. Thank you!"),
]


def write_instructions(ws, countries):
    ws.column_dimensions["A"].width = 105
    r = 1
    for kind, text in INSTRUCTIONS:
        text = text.format(countries=", ".join(countries))
        cell = ws.cell(row=r, column=1, value=text)
        if kind == "title":
            cell.font = Font(bold=True, size=15, color="305496")
        elif kind == "h":
            cell.font = Font(bold=True, size=11, color="305496")
        elif kind == "field":
            cell.font = Font(bold=True, size=11)
        else:
            cell.font = Font(size=10)
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[r].height = 30 if kind in ("p", "field") else 20
        r += 1
    ws.sheet_view.showGridLines = False


def write_matches(ws, conn, countries):
    for i, (name, width) in enumerate(COLUMNS, 1):
        c = ws.cell(row=1, column=i, value=name)
        c.fill = HEADER_FILL
        c.font = HEADER_FONT
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = BORDER
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.row_dimensions[1].height = 30

    row = 2
    shade = False
    prev_btr = None
    for country in countries:
        for rec in conn.execute(QUERY, (country,)):
            (match_id, ctry, btr_id, name, desc, status, sectors, rank, sim,
             quote, versions, asi, cats, purposes) = rec
            if btr_id != prev_btr:
                shade = not shade
                prev_btr = btr_id
            values = [
                match_id, ctry, btr_id, name, desc, status, sectors, rank,
                round(sim, 3) if sim is not None else None,
                quote, versions, asi, cats, purposes, None, None,
            ]
            for i, v in enumerate(values, 1):
                c = ws.cell(row=row, column=i, value=v)
                c.border = BORDER
                c.alignment = Alignment(
                    wrap_text=i in (4, 5, 10, 13, 14, 16),
                    vertical="top",
                    horizontal="center" if i in (3, 8, 9) else "left",
                )
                if i in (VALID_COL, COMMENT_COL):
                    c.fill = INPUT_FILL
                elif shade:
                    c.fill = GROUP_FILL
            ws.row_dimensions[row].height = 72
            row += 1

    dv = DataValidation(type="list", formula1='"yes,no,unsure"', allow_blank=True)
    dv.error = "Choose yes, no, or unsure"
    dv.prompt = "yes = correct match · no = not a match · unsure = discuss"
    ws.add_data_validation(dv)
    col = get_column_letter(VALID_COL)
    dv.add(f"{col}2:{col}{row-1}")

    ws.freeze_panes = "D2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{row-1}"
    ws.sheet_view.showGridLines = False
    return row - 2


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    conn = sqlite3.connect(DB)
    for group, countries in GROUPS.items():
        wb = openpyxl.Workbook()
        ws_instr = wb.active
        ws_instr.title = "Instructions"
        write_instructions(ws_instr, countries)
        ws_data = wb.create_sheet("Matches to validate")
        n = write_matches(ws_data, conn, countries)
        out = os.path.join(OUT_DIR, f"BTR_NDC_validation_{group}.xlsx")
        wb.save(out)
        print(f"{group}: {len(countries)} countries, {n} candidate rows -> {out}")
    conn.close()


if __name__ == "__main__":
    main()
