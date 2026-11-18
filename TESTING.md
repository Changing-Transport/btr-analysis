# Trying out the BTR review app

A short guide to test the app and give feedback. 

## What you are looking at

The app supports two analytical questions:

1. **Is this measure transport-related at all?** Some measures are filed by
   countries under *energy* or *industry* but describe transport (electric
   buses, fuel standards, rail freight). An automatic keyword screen flagged
   839 such measures; they need a human verdict.
   All 839 have now been decided (262 confirmed, 577 rejected).
2. **Does this implemented measure correspond to something the country
   committed to?** For each transport measure in a BTR, the tool proposes the
   three most similar NDC/LTS commitments from the GIZ-SLOCAT Tracker, and you
   judge whether they are really the same thing.

## Setup

You need **Python 3.10 or newer** (`python --version` to check) and
**git**. If you don't have Python, install it from
[python.org/downloads](https://www.python.org/downloads/) — on Windows tick
*"Add python.exe to PATH"* during installation.

```bash
git clone https://github.com/Changing-Transport/btr-analysis.git
cd btr-analysis
python -m venv .venv
```

Activate the virtual environment:

- **Windows (PowerShell)**: `.\.venv\Scripts\activate`
- **macOS / Linux**: `source .venv/bin/activate`

Then install and run:

```bash
pip install flask
python run_review_app.py
```

You should see `Review app on http://127.0.0.1:5000`. Open that address in a
browser. To stop the app press `Ctrl+C`; to start it again, activate the
environment and run the last command.

> The clone is ~160 MB because the database and the cached BTR spreadsheets are
> included — that is what lets you run it without any pipeline step or API key.
> `flask` is the only dependency the app itself needs.
>
> On first run the app makes its own working copy, `data/btr_analysis.local.db`.
> Your decisions go there, and git ignores it, so a later `git pull` cannot
> overwrite your work.

## A suggested 15-minute walk-through

**1. Enter your initials** on the start page. Every decision is stored under
that name so we can discuss specific judgements later.

**2. Open “2 · Matching”.** You'll see one BTR measure on the left and up to
three suggested NDC commitments on the right.

- Mark each suggestion **Match** or **No match**. Several can be a match if the
  measure genuinely implements more than one commitment.
- **If none fit, just save with everything on “No match”** — that records a
  *BTR-only* measure (implemented, but never committed to in the NDC), which is
  a real finding, not a skip.
- Some quotes carry several classifications in the Tracker. When they do, you
  are asked which reading applies — e.g. one Austrian quote is classified as
  behaviour change, public transport *and* active mobility.
- The similarity score is the AI's text-similarity hint. Scores sit roughly
  between 0.7 and 1.0, so treat them as a ranking, not a probability.

Try a few measures. **Malawi** is a good starting point (use the country filter):
its NDC and BTR wording is nearly identical, so it shows the tool at its best.
**Albania** or **Croatia** show harder, more typical cases.

**3. “1 · Relevance”** is empty: all flagged measures have been decided. To
see or question those decisions, use *Browse* with the state filter
*Relevance: decided*. Many were false positives, e.g. an energy-efficiency
programme that merely mentions vehicles in passing.

Note: if you change a decision to transport-relevant, the app normally pulls
the measure's NDC candidates in straight away. That step needs an Azure OpenAI
key which this test setup does not include, so you will instead see a note
saying candidates will be generated later. That is expected, not a fault.

**4. Open “3 · Browse”** to find anything you already decided. Filter by
country or state, open a measure, and try:

- changing a decision, or resetting it to pending;
- correcting a field the extractor got wrong (a mangled status, a truncated
  name). Edits are recorded under your name.

Nothing you do here can break anything — it is your own local copy of the data.

## What we would like to hear

- **Are the suggestions any good?** Roughly what share of rank-1 suggestions are
  correct? Is rank 1 usually the best of the three?
- **Is anything missing on screen** that you needed in order to decide? (Source
  page numbers, a link to the original document, more of the BTR description…)
- **How long** does one measure take you, once you have the hang of it?
- **Is the BTR-only concept clear** from the interface, or did it need
  explaining?
- **Anything confusing, mislabelled, or in the wrong order?**

Screenshots of anything odd are very welcome.

## Known limitations in this build

- Decisions are stored in **your own working copy**
  (`data/btr_analysis.local.db`), so we cannot pool work across people yet.
  Reviewing in parallel comes with the hosted version. To send your results,
  share that file — or just tell us what you found.
- There is no undo history — a changed decision overwrites the previous one
  (though you can always change it back).
- Bahrain, Guyana and the Philippines have no entries in the Tracker, so their
  measures have no candidates at all.
- The app is a **demo**: no login, and it runs on the Flask development server.

## If something goes wrong

| Symptom | Fix |
|---|---|
| `python: command not found` | Try `python3`, or reinstall Python with *Add to PATH* ticked |
| `ModuleNotFoundError: No module named 'flask'` | The virtual environment isn't active — activate it and re-run `pip install openpyxl flask` |
| `Address already in use` | Something else uses port 5000: `python run_review_app.py --port 5001` |
| Cannot activate the venv on Windows | Run `Set-ExecutionPolicy -Scope Process RemoteSigned` in that PowerShell window, then activate again |
| Page looks broken / stale | Hard-refresh with `Ctrl+F5` |

Anything else — send the message you see in the terminal window.
