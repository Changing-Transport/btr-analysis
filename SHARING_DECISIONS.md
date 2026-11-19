# Reviewing together: how to share your decisions

Until the review app runs online, each of us works in the app on our own
computer, with our own copy of the data. This page explains how we combine our
work without anyone overwriting anyone else's.

## The idea in short

- You review as usual in the app on your computer.
- When you finish a session, you **save your decisions to a small file** that
  carries your initials, e.g. `decisions/XY.json`, and share it on GitHub.
- Before you start a session, you **bring in everyone else's decision files**.
  Your app then shows what your colleagues have already done.

Each person only ever writes their own file. Nobody passes around the whole
database any more, so nothing gets lost.

## Before you start: two agreements

1. **Always use the same initials in the app.** The initials you type when the app
   opens (e.g. `XY`) is stored with every decision and names your file. Use it
   every time, spelled the same way.
2. **Split the work by country.** Agree who reviews which countries. Two people
   deciding the same measure leads to a conflict that has to be sorted out by
   hand (see below).

## Every session

All commands are typed in PowerShell, in the project folder (`btr-analysis`).

### 1. Get the latest files

```powershell
git pull
```

### 2. Bring in your colleagues' decisions

```powershell
python pipeline.py import-decisions
```

This reads every file in the `decisions` folder and adds what is new to your
copy of the data. Running it again does no harm: decisions that are already
there are skipped.

What the summary means:

| Message | Meaning |
|---|---|
| added or updated | new decisions, now in your copy |
| already there | nothing to do |
| older than what is there (skipped) | an older version of a decision that the same person has since changed |
| CONFLICT (kept existing) | two people decided the same measure differently; see below |
| could not be placed | rare; tell the admin |

### 3. Review in the app

```powershell
python run_review_app.py
```

Open <http://127.0.0.1:5000> and work as usual. When you are done, stop the
app with `Ctrl+C` in the PowerShell window.

### 4. Save your decisions to your file

```powershell
python pipeline.py export-decisions --by XY
```

Replace `XY` with your initials exactly as you typed it in the app. This writes
`decisions/XY.json`, which contains all your decisions so far. It replaces
your previous file, so there is always exactly one file per person.

If it says *No decisions by 'XY'*, check the spelling: upper and lower case
count.

### 5. Share your file

```powershell
git add decisions
git commit -m "Review decisions XY"
git push
```

If `git push` is refused because someone else pushed in the meantime, run
`git pull` and then `git push` again. Because everyone only changes their own
file, this always works.

**Not using git?** Send your file from the `decisions` folder to the admin, who
adds it for you.

## What is shared and what is not

Shared:

- relevance decisions ("transport-relevant" / "not transport"), with your note;
- match reviews: which NDC candidates you confirmed or rejected, the Tracker
  classification you picked, and your notes;
- corrections you made to a measure's text or figures.

Not shared:

- **setting a decision back to "pending".** If you want to undo a decision,
  tell the others; better still, change it to the right decision instead of
  resetting it;
- anything outside the review app.

## When two people decided the same measure differently

The import keeps the decision that was already in the copy and lists the
measure as a conflict, for example:

```
Match review of Chile (BTR1, row 18): Electromovilidad - Transporte publico
regiones: XY decided 'matched', YZ decided 'not matched' or chose different
candidates. Kept XY's review.
```

To sort it out:

1. Talk to each other and agree on the right decision.
2. The person whose decision **changes** opens the measure in their app
   (*Browse* → search by initials → open it), corrects it, and then saves and shares
   their file again (steps 4 and 5).

After that the conflict no longer appears.

## Measures without NDC candidates

If someone marks a measure as transport-relevant, it needs NDC candidates
before it can be matched. The import tells you when this happens. Creating
candidates needs Azure OpenAI access: tell the admin, who runs the matching
step and shares the updated data.

## Starting fresh on a new computer

1. Get the project (`git clone ...`) and install it as described in
   `TESTING.md`.
2. Start the app once (`python run_review_app.py`) and stop it again; this
   creates your own copy of the data.
3. Run step 2 above (`import-decisions`) to bring in everyone's decisions.

## Important: do not share the database file any more

The old way of copying `btr_analysis.local.db` over `btr_analysis.db` and
pushing it would overwrite everyone else's work. Please share only your file
in the `decisions` folder. The admin takes care of the shared database itself.

## When the online version is ready

Once the review app runs on Azure, all decision files are combined one last
time and moved into the online database. From then on everyone works directly
in the online app, and this page is no longer needed.
