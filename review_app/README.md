# Analyst review app (demo)

Flask app for validating transport relevance and BTR↔NDC matches directly in
the analysis database — the interactive successor to the Excel validation pilot.

## Run it

```bash
python run_review_app.py
```

Then open <http://127.0.0.1:5000>. Options: `--db`, `--port`, `--host`, `--debug`.

**Which database it writes to.** By default the app works on
`data/btr_analysis.local.db`, a private working copy seeded once from the
version-controlled snapshot `data/btr_analysis.db`. The working copy is
git-ignored, so `git pull`, branch switches and merges cannot overwrite review
decisions — a hazard that is otherwise easy to hit, because SQLite keeps recent
writes in a `-wal` sidecar that git knows nothing about. Pass
`--db data/btr_analysis.db` to work on the tracked snapshot deliberately, and
stop the app before running git operations against it.

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

Both stages have a country filter, and the overview page lists recent decisions.

**Stage 3 — Browse & edit.** The *Browse* tab lists every extracted measure with
its state in both stages, filterable by country, workflow state (relevance
pending/decided, matching pending/matched/BTR-only, manually edited) and a
free-text search over name and description. Opening a measure gives a detail
page where anything can be revised:

- **Change or reset the relevance decision** (back to pending).
- **Re-decide the match verdicts** — the form is pre-filled with the current
  verdicts, classification choice and notes, so corrections are a single edit.
  *Clear all match decisions* returns the measure to the stage-2 queue.
- **Correct extracted fields** — name, description, objectives, instrument,
  status (raw and normalised), sectors, gases, start year, entity and the GHG
  figures. Edits are stamped with `edited_by` / `edited_at` and the measure is
  badged *edited* in the browse list.

## Re-ingestion safety

Re-running `pipeline.py batch` or `ingest` replaces a document's measures, which
would discard the review decisions attached to them. Documents carrying review
work are therefore **skipped** with an explanatory message; pass `--force` to
overwrite deliberately.

Two related guarantees make repeated pipeline runs safe:

- `measure_matches` cascades on delete from both `btr_measures` and
  `ndc_quotes`, so re-ingestion and Tracker re-imports cannot fail on a
  foreign-key violation.
- `import-ndc` upserts quote rows instead of recreating them, so `ndc_quotes.id`
  stays stable and confirmed matches survive a Tracker refresh. Only the
  analyst's *classification choice* is reset (the underlying classification rows
  are rebuilt with new ids); the confirmed match itself is kept.

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
├── templates/       # base, index, identify, relevance, match, browse, measure, _queue
└── static/style.css
run_review_app.py    # local entry point
```
