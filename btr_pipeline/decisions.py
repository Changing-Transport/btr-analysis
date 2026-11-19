"""
Share review decisions between local copies of the database.

Until the hosted database is available, every reviewer works in their own
copy. Instead of passing whole database files around (which overwrites other
people's work), reviewers exchange small decision files:

    export_decisions(conn)          -> dict  (written as JSON)
    import_decisions(conn, data)    -> report

A decision file holds three kinds of decisions, each with who made it and when:

    relevance  confirmed / rejected (stage 1), with the note
    matches    the match review of one measure (stage 2): the outcome and the
               verdict on every candidate, incl. classification choice and note
    edits      corrected measure fields (Browse > measure page)

Records are identified by content, not by database ids, so files stay valid
across copies: a measure by (country, document type, Table 5 row), an NDC
candidate by (country, quote text), a Tracker classification by its category,
purpose, instrument, document type, version and page.

Merge rules, applied per decision:
    * the target has no decision yet          -> apply
    * the same decision is already there      -> nothing to do
    * same reviewer, incoming is newer        -> apply (the reviewer revised it)
    * same reviewer, incoming is older        -> skip (outdated copy)
    * a different reviewer decided otherwise  -> conflict: target kept, reported
Undoing a decision (back to "pending") is not carried by decision files.
"""

from datetime import datetime, timezone

FORMAT = "btr-decisions/1"

EDIT_FIELDS = [
    "measure_name", "description", "objectives", "type_of_instrument",
    "status_raw", "status_normalized", "sectors", "gases", "start_year",
    "implementing_entity", "ghg_achieved", "ghg_expected",
]
CLASSIFICATION_FIELDS = ["category", "purpose", "instrument", "doc_type",
                         "version", "page"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def _measure_rows(conn, where: str) -> list:
    return conn.execute(
        f"""SELECT m.*, d.country, d.doc_type
            FROM btr_measures m JOIN documents d ON d.id = m.document_id
            WHERE {where}
            ORDER BY d.country, m.source_row"""
    ).fetchall()


def _key(row) -> dict:
    return {"country": row["country"], "doc_type": row["doc_type"],
            "row": row["source_row"]}


def export_decisions(conn, reviewer: str | None = None) -> dict:
    """
    Collect all review decisions in `conn`, or only those made by `reviewer`.
    """
    def mine(by) -> bool:
        return reviewer is None or by == reviewer

    relevance = [
        {"measure": _key(r), "measure_name": r["measure_name"],
         "decision": r["review_status"], "by": r["reviewed_by"],
         "at": r["reviewed_at"], "note": r["review_note"]}
        for r in _measure_rows(
            conn, "m.reviewed_by IS NOT NULL "
                  "AND m.review_status IN ('confirmed', 'rejected')")
        if mine(r["reviewed_by"])
    ]

    matches = []
    for r in _measure_rows(conn, "m.match_outcome IS NOT NULL"):
        if not mine(r["matched_by"]):
            continue
        candidates = []
        for c in conn.execute(
            """SELECT q.country, q.quote, mm.validated, mm.validated_by,
                      mm.validated_at, mm.validation_note,
                      mm.selected_classification_id
               FROM measure_matches mm JOIN ndc_quotes q ON q.id = mm.ndc_id
               WHERE mm.btr_id = ? ORDER BY mm.rank""",
            (r["id"],),
        ).fetchall():
            classification = None
            if c[6] is not None:
                cl = conn.execute(
                    f"SELECT {', '.join(CLASSIFICATION_FIELDS)} "
                    f"FROM ndc_measures WHERE id = ?", (c[6],)
                ).fetchone()
                if cl is not None:
                    classification = dict(zip(CLASSIFICATION_FIELDS, cl))
            candidates.append({
                "quote_country": c[0], "quote": c[1], "verdict": c[2],
                "by": c[3], "at": c[4], "note": c[5],
                "classification": classification,
            })
        matches.append({
            "measure": _key(r), "measure_name": r["measure_name"],
            "outcome": r["match_outcome"], "by": r["matched_by"],
            "at": r["matched_at"], "candidates": candidates,
        })

    edits = [
        {"measure": _key(r), "measure_name": r["measure_name"],
         "by": r["edited_by"], "at": r["edited_at"],
         "fields": {f: r[f] for f in EDIT_FIELDS}}
        for r in _measure_rows(conn, "m.edited_by IS NOT NULL")
        if mine(r["edited_by"])
    ]

    return {
        "format": FORMAT,
        "exported_at": _now(),
        "reviewer": reviewer,
        "counts": {"relevance": len(relevance), "matches": len(matches),
                   "edits": len(edits)},
        "relevance": relevance,
        "matches": matches,
        "edits": edits,
    }


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

class Report:
    """What an import did, in words a reviewer can read."""

    KINDS = ("relevance", "matches", "edits")

    def __init__(self):
        self.counts = {k: {"applied": 0, "unchanged": 0, "outdated": 0,
                           "conflict": 0, "not_found": 0} for k in self.KINDS}
        self.conflicts: list[str] = []
        self.problems: list[str] = []
        self.need_candidates: list[str] = []

    def add(self, kind: str, outcome: str) -> None:
        self.counts[kind][outcome] += 1

    @property
    def applied(self) -> int:
        return sum(c["applied"] for c in self.counts.values())


def _label(entry, current=None) -> str:
    """Readable name of a measure; prefers the name in the target database."""
    m = entry["measure"]
    name = ((current["measure_name"] if current is not None else None)
            or entry.get("measure_name") or "").strip()
    if len(name) > 70:
        name = name[:67] + "..."
    return f"{m['country']} ({m['doc_type']}, row {m['row']}): {name}"


def _find_measure(conn, key):
    return conn.execute(
        """SELECT m.* FROM btr_measures m JOIN documents d ON d.id = m.document_id
           WHERE d.country = ? AND d.doc_type = ? AND m.source_row = ?""",
        (key["country"], key["doc_type"], key["row"]),
    ).fetchone()


def _rule(current_value, current_by, current_at,
          new_value, new_by, new_at) -> str:
    """Return 'apply', 'unchanged', 'outdated' or 'conflict'."""
    if current_value == new_value:
        return "unchanged"
    if current_by is None:
        return "apply"
    if current_by == new_by:
        return "apply" if (new_at or "") > (current_at or "") else "outdated"
    return "conflict"


def _import_relevance(conn, entry, report: Report) -> None:
    m = _find_measure(conn, entry["measure"])
    if m is None:
        report.add("relevance", "not_found")
        report.problems.append(f"Measure not found: {_label(entry)}")
        return
    current = m["review_status"] if m["reviewed_by"] else None
    verdict = _rule(current, m["reviewed_by"], m["reviewed_at"],
                    entry["decision"], entry["by"], entry["at"])
    if verdict == "apply":
        conn.execute(
            """UPDATE btr_measures SET review_status = ?, reviewed_by = ?,
                      reviewed_at = ?, review_note = ?
               WHERE id = ?""",
            (entry["decision"], entry["by"], entry["at"], entry["note"], m["id"]),
        )
        if entry["decision"] == "confirmed" and not conn.execute(
            "SELECT 1 FROM measure_matches WHERE btr_id = ?", (m["id"],)
        ).fetchone():
            report.need_candidates.append(_label(entry, m))
        report.add("relevance", "applied")
    elif verdict == "conflict":
        report.add("relevance", "conflict")
        report.conflicts.append(
            f"Relevance of {_label(entry, m)}: {m['reviewed_by']} said "
            f"'{current}', {entry['by']} said '{entry['decision']}'. "
            f"Kept {m['reviewed_by']}'s decision."
        )
    else:
        report.add("relevance", verdict)


def _match_state(conn, btr_id: int) -> dict:
    """Candidate verdicts of a measure, keyed by (quote country, quote)."""
    state = {}
    for r in conn.execute(
        """SELECT mm.id, q.country, q.quote, mm.validated,
                  mm.selected_classification_id, q.id
           FROM measure_matches mm JOIN ndc_quotes q ON q.id = mm.ndc_id
           WHERE mm.btr_id = ?""",
        (btr_id,),
    ).fetchall():
        state[(r[1], r[2])] = {"match_id": r[0], "verdict": r[3],
                               "classification_id": r[4], "quote_id": r[5]}
    return state


def _find_classification(conn, quote_id: int, wanted: dict | None):
    if not wanted:
        return None
    for row in conn.execute(
        f"SELECT id, {', '.join(CLASSIFICATION_FIELDS)} "
        f"FROM ndc_measures WHERE quote_id = ? ORDER BY id",
        (quote_id,),
    ).fetchall():
        if all(row[i + 1] == wanted.get(f)
               for i, f in enumerate(CLASSIFICATION_FIELDS)):
            return row[0]
    return False  # wanted, but no such classification in this copy


def _import_match(conn, entry, report: Report) -> None:
    m = _find_measure(conn, entry["measure"])
    if m is None:
        report.add("matches", "not_found")
        report.problems.append(f"Measure not found: {_label(entry)}")
        return

    state = _match_state(conn, m["id"])
    incoming = {(c["quote_country"], c["quote"]): c for c in entry["candidates"]}

    missing = [k for k, c in incoming.items() if c["verdict"] and k not in state]
    if missing:
        report.add("matches", "not_found")
        report.problems.append(
            f"Match review of {_label(entry, m)} not applied: "
            f"{len(missing)} of its candidates are not listed in this copy "
            f"(candidates were regenerated differently)."
        )
        return

    current_value = (m["match_outcome"],
                     sorted((k[1], s["verdict"] or "") for k, s in state.items()))
    new_value = (entry["outcome"],
                 sorted((k[1], (incoming[k]["verdict"] if k in incoming else s["verdict"]) or "")
                        for k, s in state.items()))
    verdict = _rule(current_value if m["matched_by"] else None,
                    m["matched_by"], m["matched_at"],
                    new_value, entry["by"], entry["at"])

    if verdict == "conflict":
        report.add("matches", "conflict")
        report.conflicts.append(
            f"Match review of {_label(entry, m)}: {m['matched_by']} decided "
            f"'{m['match_outcome']}', {entry['by']} decided '{entry['outcome']}' "
            f"or chose different candidates. Kept {m['matched_by']}'s review."
        )
        return
    if verdict != "apply":
        report.add("matches", verdict)
        return

    for k, c in incoming.items():
        if k not in state:
            continue
        cls_id = None
        if c["verdict"] == "confirmed":
            cls_id = _find_classification(conn, state[k]["quote_id"],
                                          c["classification"])
            if cls_id is False:
                report.problems.append(
                    f"{_label(entry, m)}: the chosen Tracker classification "
                    f"was not found in this copy; match kept without it."
                )
                cls_id = None
        conn.execute(
            """UPDATE measure_matches
               SET validated = ?, validated_by = ?, validated_at = ?,
                   selected_classification_id = ?, validation_note = ?
               WHERE id = ?""",
            (c["verdict"], c["by"], c["at"], cls_id, c["note"],
             state[k]["match_id"]),
        )
    conn.execute(
        """UPDATE btr_measures SET match_outcome = ?, matched_by = ?, matched_at = ?
           WHERE id = ?""",
        (entry["outcome"], entry["by"], entry["at"], m["id"]),
    )
    report.add("matches", "applied")


def _import_edit(conn, entry, report: Report) -> None:
    m = _find_measure(conn, entry["measure"])
    if m is None:
        report.add("edits", "not_found")
        report.problems.append(f"Measure not found: {_label(entry)}")
        return
    changed = {f: v for f, v in entry["fields"].items()
               if f in EDIT_FIELDS and m[f] != v}
    if not changed:
        report.add("edits", "unchanged")
        return
    verdict = _rule("current" if m["edited_by"] else None,
                    m["edited_by"], m["edited_at"],
                    "incoming", entry["by"], entry["at"])
    if verdict == "conflict":
        report.add("edits", "conflict")
        report.conflicts.append(
            f"Corrections to {_label(entry, m)}: {m['edited_by']} and {entry['by']} "
            f"both changed it ({', '.join(changed)}). Kept {m['edited_by']}'s version."
        )
        return
    if verdict != "apply":
        report.add("edits", verdict)
        return
    sets = ", ".join(f"{f} = ?" for f in changed)
    conn.execute(
        f"UPDATE btr_measures SET {sets}, edited_by = ?, edited_at = ? WHERE id = ?",
        (*changed.values(), entry["by"], entry["at"], m["id"]),
    )
    report.add("edits", "applied")


def import_decisions(conn, data: dict, dry_run: bool = False) -> Report:
    """Merge one decision file into `conn`. Nothing is written if dry_run."""
    if data.get("format") != FORMAT:
        raise ValueError("This is not a BTR decision file (or an unknown version).")
    report = Report()
    try:
        for entry in data.get("relevance", []):
            _import_relevance(conn, entry, report)
        for entry in data.get("edits", []):
            _import_edit(conn, entry, report)
        for entry in data.get("matches", []):
            _import_match(conn, entry, report)
    except Exception:
        conn.rollback()
        raise
    if dry_run:
        conn.rollback()
    else:
        conn.commit()
    return report
