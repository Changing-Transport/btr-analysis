# Analyst review app (demo)

Flask app for validating transport relevance and BTR↔NDC matches directly in
the analysis database — the interactive successor to the Excel validation pilot.

## Run it

```bash
python run_review_app.py
```

Then open <http://127.0.0.1:5000>. Options: `--db`, `--port`, `--host`, `--debug`.

## Workflow

**Stage 1 — Relevance.** The 839 `content_match` measures are filed under a
non-transport sector (mostly energy/industry) but mention transport in their
text. The analyst sees the measure, which keywords triggered it, and the LLM
verdict if `pipeline.py classify` has run, then decides **transport-relevant**
or **not transport**. Writes `review_status` = `confirmed` / `rejected` plus
`reviewed_by` / `reviewed_at` / `review_note`.

**Stage 2 — Matching.** For each BTR measure the top-3 NDC quote candidates are
shown next to the measure. For each candidate the analyst picks **Match** or
**No match**:

- **Several candidates may be confirmed** — a broad measure can implement more
  than one commitment.
- **Confirming nothing records a BTR-only measure** — implemented, but not
  committed to in the NDC. This is a real analytical result, not a skip.
- **Classification picker**: ~12% of candidate quotes carry more than one
  taxonomy classification in the Tracker (one Austrian quote is classified
  three ways: behaviour change, public transport, active mobility). When that
  happens the analyst chooses which reading applies; with a single
  classification it is recorded automatically and no choice is shown.

Writes `measure_matches.validated` / `validated_by` / `validated_at` /
`selected_classification_id` / `validation_note`, and
`btr_measures.match_outcome` = `matched` / `btr_only`.

Both stages have a country filter, and the overview page lists recent decisions
with a **reopen** button that returns a measure to the queue.

## After stage 1

Measures newly confirmed as transport-relevant have no candidates yet. Generate
them with:

```bash
python pipeline.py embed && python pipeline.py match
```

`embed` covers `review_status IN ('auto','confirmed')`, so confirmed measures
get vectors and then candidates. Existing analyst validations survive `match`
re-runs (re-attached by `btr_id` + `ndc_id`).

## Identity

The analyst types their name once; it is kept in the session and stored with
every decision. No password — adequate for a local pilot. For a hosted
deployment put Entra ID in front of the app and set `FLASK_SECRET_KEY`.

## Mounting into the climate-policy-miner

The app is a blueprint behind a factory, so it can be folded into the existing
Flask app without restructuring:

```python
from review_app import bp as review_bp

app.config["BTR_DB_PATH"] = "/path/to/btr_analysis.db"
app.register_blueprint(review_bp, url_prefix="/btr-review")
```

All SQL lives in `queries.py`, mirroring `btr_pipeline/db.py`, so the move to
Azure PostgreSQL touches one module per side.

## Files

```
review_app/
├── __init__.py      # create_app factory + blueprint export
├── views.py         # routes
├── queries.py       # all SQL (read + write)
├── templates/       # base, index, identify, relevance, match, _queue
└── static/style.css
run_review_app.py    # local entry point
```
