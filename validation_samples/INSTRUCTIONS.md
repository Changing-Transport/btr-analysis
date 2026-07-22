# BTR ↔ NDC match validation — instructions

Thank you for helping validate the automated matching. This is a pilot: your
feedback tells us how well the AI matching works and what the review tool needs
to do once we build it.

## Background (30 seconds)

- **BTR measures** = what a country reports having **implemented** in its
  Biennial Transparency Report.
- **NDC quotes** = what the country **committed to** in its NDC / long-term
  strategy.
- The tool takes each BTR measure and suggests the **3 most similar** NDC quotes,
  using AI text-similarity. Your job is to judge whether each suggestion is
  actually the same measure/commitment.

## How to fill it in

Open the **"Matches to validate"** tab. Only the two **yellow** columns need
your input; everything else is reference.

- Each BTR measure has up to **3 suggested NDC matches** (rank 1–3), shaded
  together as one block.
- In **VALID?** choose from the dropdown:
  - **yes** — the NDC quote refers to the same measure/commitment
  - **no** — it does not
  - **unsure** — you can't tell, or you'd like to discuss it
- **More than one "yes" per block is fine** if the BTR measure genuinely matches
  several NDC commitments.
- **If none of the 3 fit, mark all three "no".** That is a real, useful result:
  the measure is being implemented but wasn't in the NDC (a "BTR-only" measure).
- Use **comment** for anything notable — a partial match, a better quote you know
  exists, a wrong sector, etc.

## What to compare

- Read the BTR measure (**btr_measure** + **btr_description**) against the
  **ndc_quote**.
- **ai_similarity** is the model's score (higher = more similar wording).
- **ndc_generation** shows which NDC/LTS version the quote comes from. Matches
  may come from older generations, that's expected and fine.

## Practical notes

- Put your **name** in the cell on the Instructions tab.
- Please **don't re-sort** the sheet before saving — it keeps the rank 1–3 rows
  grouped. If you must, sort by `btr_id` then `rank`.
- Each file covers **5 countries (~25 measures, ~75 rows)**.
- Save and send the file back when done.

## What we'll learn from this

- What share of AI suggestions are correct (precision)?
- Is the top-ranked (rank 1) suggestion usually the best?
- How often is a BTR measure "BTR-only" (no NDC equivalent)?
- What extra information would you have wanted while judging?

## File assignment

| File | Countries |
|---|---|
| `BTR_NDC_validation_Group1.xlsx` | Malawi, Oman, Jordan, South Africa, Malta |
| `BTR_NDC_validation_Group2.xlsx` | Singapore, Trinidad & Tobago, Thailand, United Arab Emirates, Morocco |
| `BTR_NDC_validation_Group3.xlsx` | Côte d'Ivoire, Mauritius, Paraguay, Lao PDR, Egypt |
