# Analyst review app (demo)

Flask app for validating transport relevance and BTR↔NDC matches directly in
the analysis database — the interactive successor to the Excel validation pilot.

## Run it

```bash
python run_review_app.py
```

Then open <http://127.0.0.1:5000>. Options: `--db` (SQLite path or
`postgresql://` URL), `--port`, `--host`, `--debug`. The hosted version runs
the same app under gunicorn (`wsgi.py`, `Dockerfile`); see
[../deploy/AZURE.md](../deploy/AZURE.md).

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
text. All of them were decided in the first review round (2026-11: 262
confirmed, 577 rejected), so the queue is empty until new BTRs are ingested;
earlier decisions can still be revised in *Browse*. The analyst sees the measure, which keywords triggered it, and the LLM
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

## From stage 1 straight into stage 2

Confirming a measure as transport-relevant **generates its NDC candidates
immediately**: the app embeds that one measure via Azure OpenAI and scores it
against the country's quotes (already embedded), so it appears in the stage-2
queue right away. The confirmation message says how many candidates were found.

This needs `AZURE_OPENAI_*` credentials, which `run_review_app.py` reads from
`.env`. Without them the relevance decision is still saved — the app just says
candidates will follow later, and you generate them in bulk with:

```bash
python pipeline.py --db data/btr_analysis.local.db embed
python pipeline.py --db data/btr_analysis.local.db match --missing-only
```

`embed` covers `review_status IN ('auto','confirmed')`, so confirmed measures
get vectors; `match --missing-only` then adds candidates only for measures that
have none and leaves existing rows untouched. A full `match` re-run also keeps
analyst work (verdict, analyst, timestamp, classification choice and note are
re-attached by `btr_id` + `ndc_id`), but recomputes every candidate list.

## Identity

Locally, the analyst types their name once; it is kept in the session and
stored with every decision.

Hosted, Azure Container Apps authentication (Entra ID) signs users in before
any request reaches the app. With `BTR_EASY_AUTH=1` the app takes the analyst
from the `X-MS-CLIENT-PRINCIPAL-NAME` header the platform sets (usually the
e-mail address), ignores typed names, answers 401 to requests without it, and
"sign out" goes to `/.auth/logout`. Only set `BTR_EASY_AUTH=1` when that
authentication is enabled and required for every request; the header is
trusted as given. `FLASK_SECRET_KEY` must be set in that mode. `/healthz` stays
open for the platform's health probes.

## Parallel review

When several analysts work at the same time, the queues hand each of them a
different measure:

- Showing a measure in stage 1 or 2 locks it (`btr_measures.locked_by` /
  `locked_at`) for 30 minutes (`LOCK_MINUTES` in `queries.py`). Colleagues'
  queues skip it. Reloading the page shows the analyst the same measure.
- Saving the decision releases the lock; taking the next measure releases the
  previous one; an abandoned lock expires.
- A decision submitted from a queue is refused, with a message, if a colleague
  now holds the lock or has already decided the measure (possible only after
  the submitter's lock expired).
- *Browse* and the detail page are not locked: edits there apply immediately.
  The detail page says when a colleague has the measure open in a queue.

The claim is a single conditional UPDATE, so two analysts requesting the same
measure at the same moment cannot both get it (tested with eight concurrent
claims on PostgreSQL).

## Mounting into the climate-policy-miner

The app is a blueprint behind a factory, so it can be folded into the existing
Flask app without restructuring:

```python
from review_app import bp as review_bp

app.config["BTR_DB_PATH"] = "postgresql://..."   # or a SQLite path
app.register_blueprint(review_bp, url_prefix="/btr-review")
```

All SQL lives in `queries.py` and runs on SQLite and PostgreSQL through
`btr_pipeline.db.connect`.

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
