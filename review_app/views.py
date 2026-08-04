"""Routes for the analyst review app."""

from flask import (Blueprint, current_app, g, redirect, render_template,
                   request, session, url_for)

from . import queries as q

bp = Blueprint("review", __name__, template_folder="templates",
               static_folder="static")


def db():
    if "db" not in g:
        g.db = q.connect(current_app.config["BTR_DB_PATH"])
    return g.db


@bp.teardown_app_request
def close_db(exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def analyst() -> str | None:
    return session.get("analyst")


@bp.route("/")
def index():
    conn = db()
    return render_template(
        "index.html",
        analyst=analyst(),
        progress=q.progress(conn),
        recent=q.recent_decisions(conn, limit=8),
    )


@bp.route("/identify", methods=["POST"])
def identify():
    name = (request.form.get("analyst") or "").strip()
    if name:
        session["analyst"] = name
    return redirect(request.form.get("next") or url_for("review.index"))


@bp.route("/signout")
def signout():
    session.pop("analyst", None)
    return redirect(url_for("review.index"))


# ---------------------------------------------------------------------------
# Stage 1 — relevance screening of non-transport-sector measures
# ---------------------------------------------------------------------------

@bp.route("/relevance")
def relevance():
    conn = db()
    country = request.args.get("country") or None
    if not analyst():
        return render_template("identify.html", stage="relevance",
                               next_url=url_for("review.relevance", country=country))

    measure = q.next_relevance(conn, country)
    return render_template(
        "relevance.html",
        analyst=analyst(),
        measure=measure,
        country=country,
        countries=q.countries(conn, "relevance"),
        progress=q.progress(conn),
    )


@bp.route("/relevance/<int:measure_id>", methods=["POST"])
def relevance_save(measure_id: int):
    decision = request.form.get("decision")
    if decision in ("confirmed", "rejected"):
        q.save_relevance(db(), measure_id, decision, analyst() or "unknown",
                         request.form.get("note"))
    return redirect(url_for("review.relevance",
                            country=request.form.get("country") or None))


# ---------------------------------------------------------------------------
# Stage 2 — match validation
# ---------------------------------------------------------------------------

@bp.route("/match")
def match():
    conn = db()
    country = request.args.get("country") or None
    if not analyst():
        return render_template("identify.html", stage="match",
                               next_url=url_for("review.match", country=country))

    measure = q.next_match(conn, country)
    return render_template(
        "match.html",
        analyst=analyst(),
        measure=measure,
        candidates=q.candidates(conn, measure["id"]) if measure else [],
        country=country,
        countries=q.countries(conn, "match"),
        progress=q.progress(conn),
    )


@bp.route("/match/<int:measure_id>", methods=["POST"])
def match_save(measure_id: int):
    """
    Form fields per candidate row (match_id = N):
        verdict-N          'confirmed' | 'rejected'
        classification-N   ndc_measures.id chosen when the quote has several
        note-N             free-text
    Candidates left unset default to 'rejected', so "none of these fit" is
    simply submitting without confirming any — recorded as a BTR-only measure.
    """
    decisions: dict[int, str] = {}
    choices: dict[int, int | None] = {}
    notes: dict[int, str | None] = {}

    for key, value in request.form.items():
        if not key.startswith("verdict-"):
            continue
        match_id = int(key.split("-", 1)[1])
        decisions[match_id] = "confirmed" if value == "confirmed" else "rejected"
        raw_choice = request.form.get(f"classification-{match_id}")
        choices[match_id] = int(raw_choice) if raw_choice else None
        notes[match_id] = request.form.get(f"note-{match_id}")

    if decisions:
        q.save_match(db(), measure_id, analyst() or "unknown",
                     decisions, choices, notes)
    return redirect(url_for("review.match",
                            country=request.form.get("country") or None))


@bp.route("/match/<int:measure_id>/reopen", methods=["POST"])
def match_reopen(measure_id: int):
    q.reopen_match(db(), measure_id)
    return redirect(request.form.get("back") or url_for("review.index"))


# ---------------------------------------------------------------------------
# Browse & edit
# ---------------------------------------------------------------------------

PAGE_SIZE = 50

STATES = [
    ("", "All measures"),
    ("relevance_pending", "Relevance: pending"),
    ("relevance_done", "Relevance: decided"),
    ("match_pending", "Matching: pending"),
    ("matched", "Matching: matched"),
    ("btr_only", "Matching: BTR-only"),
    ("edited", "Manually edited"),
]


@bp.route("/browse")
def browse():
    conn = db()
    country = request.args.get("country") or None
    state = request.args.get("state") or None
    search = (request.args.get("q") or "").strip() or None
    page = max(1, int(request.args.get("page", 1)))

    rows, total = q.browse(conn, country, state, search,
                           limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
    return render_template(
        "browse.html",
        analyst=analyst(),
        rows=rows, total=total, page=page, page_size=PAGE_SIZE,
        pages=max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
        country=country, state=state, search=search,
        all_countries=q.all_countries(conn), states=STATES,
    )


@bp.route("/measure/<int:measure_id>")
def measure_detail(measure_id: int):
    conn = db()
    m = q.measure(conn, measure_id)
    if m is None:
        return redirect(url_for("review.browse"))
    return render_template(
        "measure.html",
        analyst=analyst(),
        measure=m,
        candidates=q.candidates(conn, measure_id),
        editable=q.EDITABLE_FIELDS,
        status_options=q.STATUS_OPTIONS,
        back=request.args.get("back"),
    )


@bp.route("/measure/<int:measure_id>/fields", methods=["POST"])
def measure_fields(measure_id: int):
    q.update_fields(db(), measure_id, request.form.to_dict(),
                    analyst() or "unknown")
    return redirect(url_for("review.measure_detail", measure_id=measure_id))


@bp.route("/measure/<int:measure_id>/relevance", methods=["POST"])
def measure_relevance(measure_id: int):
    """Set or clear the relevance decision from the detail page."""
    decision = request.form.get("decision")
    if decision == "reset":
        q.reset_relevance(db(), measure_id)
    elif decision in ("confirmed", "rejected"):
        q.save_relevance(db(), measure_id, decision, analyst() or "unknown",
                         request.form.get("note"))
    return redirect(url_for("review.measure_detail", measure_id=measure_id))


@bp.route("/measure/<int:measure_id>/match", methods=["POST"])
def measure_match(measure_id: int):
    """Re-decide match verdicts from the detail page (same form shape as stage 2)."""
    decisions, choices, notes = {}, {}, {}
    for key, value in request.form.items():
        if not key.startswith("verdict-"):
            continue
        match_id = int(key.split("-", 1)[1])
        decisions[match_id] = "confirmed" if value == "confirmed" else "rejected"
        raw = request.form.get(f"classification-{match_id}")
        choices[match_id] = int(raw) if raw else None
        notes[match_id] = request.form.get(f"note-{match_id}")

    if decisions:
        q.save_match(db(), measure_id, analyst() or "unknown",
                     decisions, choices, notes)
    return redirect(url_for("review.measure_detail", measure_id=measure_id))
