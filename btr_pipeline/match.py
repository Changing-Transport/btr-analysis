"""
Embedding and semantic matching between BTR measures and NDC/LTS measures.

Embedding:
    * BTR side: auto-accepted or confirmed measures (review_status in
      ('auto', 'confirmed')); text = name + description + objectives.
    * NDC side: deduplicated quotes (ndc_quotes, one row per distinct
      country+quote across all generations); text = the bare quote.
      The same quote can carry several taxonomy classifications in the
      Tracker — matching on quotes keeps each text exactly once in the
      candidate list.
    * Vectors come from the Azure OpenAI embedding deployment
      (AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME) and are stored as little-endian
      float32 BLOBs in the `embedding` column (~6 KB per measure — compact
      enough to keep the SQLite file in git).

Matching:
    * Per BTR document, candidate NDC measures are those of the same country
      (via COUNTRY_NAME_MAP for naming differences). EU member states fall
      back to the European Union's measures when the Tracker has no national
      entry (EU members submit a joint NDC).
    * OpenAI embeddings are unit-normalised, so cosine similarity = dot product.
    * Top-3 candidates per BTR measure are written to measure_matches with
      rank 1-3; `validated` stays NULL until an analyst confirms/rejects.
"""

import os
import struct

from .countries import EU_MEMBERS, to_tracker_name

EU_TRACKER_NAME = "European Union"

TOP_K = 3
EMBED_BATCH_SIZE = 64


# ---------------------------------------------------------------------------
# Vector serialisation (little-endian float32 BLOB)
# ---------------------------------------------------------------------------

def vec_to_blob(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def blob_to_vec(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


def dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


# ---------------------------------------------------------------------------
# Azure OpenAI client
# ---------------------------------------------------------------------------

def get_embedding_client():
    """Returns (client, deployment_name). Raises RuntimeError if not configured."""
    required = ["AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY",
                "AZURE_OPENAI_API_VERSION", "AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME"]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")

    from openai import AzureOpenAI
    client = AzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        api_version=os.environ["AZURE_OPENAI_API_VERSION"],
    )
    return client, os.environ["AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME"]


def embed_texts(client, deployment: str, texts: list[str]) -> list[list[float]]:
    # The API rejects empty strings; substitute a single space.
    safe = [t if t.strip() else " " for t in texts]
    resp = client.embeddings.create(model=deployment, input=safe)
    return [d.embedding for d in resp.data]


# ---------------------------------------------------------------------------
# Embedding text composition
# ---------------------------------------------------------------------------

def btr_text(name, description, objectives) -> str:
    parts = [p for p in (name, description, objectives) if p]
    return " | ".join(str(p).strip() for p in parts)[:6000]


# ---------------------------------------------------------------------------
# Embed step
# ---------------------------------------------------------------------------

def embed_missing(conn, log=print) -> dict:
    """Embed all BTR measures (auto/confirmed) and NDC quotes lacking a vector."""
    client, deployment = get_embedding_client()

    btr_rows = conn.execute(
        """SELECT id, measure_name, description, objectives FROM btr_measures
           WHERE review_status IN ('auto', 'confirmed') AND embedding IS NULL
           ORDER BY id"""
    ).fetchall()
    ndc_rows = conn.execute(
        """SELECT id, quote FROM ndc_quotes
           WHERE embedding IS NULL
           ORDER BY id"""
    ).fetchall()

    jobs = [("btr_measures", r[0], btr_text(r[1], r[2], r[3])) for r in btr_rows]
    jobs += [("ndc_quotes", r[0], str(r[1]).strip()[:6000]) for r in ndc_rows]

    if not jobs:
        log("Nothing to embed — all vectors present.")
        return {"embedded": 0}

    log(f"Embedding {len(btr_rows)} BTR measures + {len(ndc_rows)} NDC quotes "
        f"with deployment '{deployment}'...")

    done = 0
    for i in range(0, len(jobs), EMBED_BATCH_SIZE):
        batch = jobs[i:i + EMBED_BATCH_SIZE]
        vectors = embed_texts(client, deployment, [t for _, _, t in batch])
        for (table, row_id, _), vec in zip(batch, vectors):
            conn.execute(
                f"UPDATE {table} SET embedding = ? WHERE id = ?",
                (vec_to_blob(vec), row_id),
            )
        conn.commit()
        done += len(batch)
        log(f"  {done}/{len(jobs)}")

    return {"embedded": done}


# ---------------------------------------------------------------------------
# Match step
# ---------------------------------------------------------------------------

def candidate_pool(conn, country: str, include_archived: bool = True
                   ) -> tuple[list[tuple[int, list[float]]], str | None]:
    """
    Candidate NDC quote vectors for one country, applying the same fallback
    chain as run_matching. Returns (pool, fallback_used).
    """
    def load(where: str, params: tuple) -> list[tuple[int, list[float]]]:
        return [
            (qid, blob_to_vec(blob))
            for qid, blob in conn.execute(
                f"SELECT id, embedding FROM ndc_quotes "
                f"WHERE embedding IS NOT NULL AND {where}", params
            )
        ]

    ndc_country = to_tracker_name(country)
    status_clause = "" if include_archived else " AND doc_statuses LIKE '%Active%'"

    pool = load(f"country = ?{status_clause}", (ndc_country,))
    if pool:
        return pool, None

    if ndc_country in EU_MEMBERS:
        pool = load(f"country = ?{status_clause}", (EU_TRACKER_NAME,))
        if pool:
            return pool, "european-union"

    if not include_archived:
        pool = load("country = ?", (ndc_country,))
        if pool:
            return pool, "archived"

    return [], None


def match_one(conn, btr_id: int, include_archived: bool = True) -> dict:
    """
    Generate top-K candidates for a single BTR measure, embedding it first if
    it has no vector yet. Used by the review app so a measure confirmed as
    transport-relevant gets its candidates straight away.

    Returns {"candidates": int, "fallback": str | None, "reason": str | None}.
    `reason` explains an empty result: 'no-credentials', 'no-pool' or
    'not-found'. Any analyst validations on this measure are preserved.
    """
    row = conn.execute(
        """SELECT m.id, m.embedding, m.measure_name, m.description, m.objectives,
                  d.country
           FROM btr_measures m JOIN documents d ON d.id = m.document_id
           WHERE m.id = ?""",
        (btr_id,),
    ).fetchone()
    if row is None:
        return {"candidates": 0, "fallback": None, "reason": "not-found"}

    _id, blob, name, description, objectives, country = row

    if blob is None:
        try:
            client, deployment = get_embedding_client()
            vectors = embed_texts(client, deployment,
                                  [btr_text(name, description, objectives)])
        except Exception:
            # No credentials, or the API is unavailable: the relevance decision
            # still stands, candidates can be generated later by `pipeline.py
            # embed && match`.
            return {"candidates": 0, "fallback": None, "reason": "no-credentials"}
        blob = vec_to_blob(vectors[0])
        conn.execute("UPDATE btr_measures SET embedding = ? WHERE id = ?",
                     (blob, btr_id))

    pool, fallback = candidate_pool(conn, country, include_archived)
    if not pool:
        conn.commit()
        return {"candidates": 0, "fallback": None, "reason": "no-pool"}

    vec = blob_to_vec(blob)
    scored = sorted(((dot(vec, cvec), cid) for cid, cvec in pool),
                    reverse=True)[:TOP_K]

    prior = {
        r[0]: (r[1], r[2], r[3], r[4], r[5])
        for r in conn.execute(
            """SELECT ndc_id, validated, validated_by, validated_at,
                      selected_classification_id, validation_note
               FROM measure_matches WHERE btr_id = ? AND validated IS NOT NULL""",
            (btr_id,),
        )
    }
    conn.execute("DELETE FROM measure_matches WHERE btr_id = ?", (btr_id,))

    for rank, (score, ndc_id) in enumerate(scored, 1):
        validated, by, at, cls, note = prior.get(ndc_id, (None,) * 5)
        conn.execute(
            """INSERT INTO measure_matches
               (btr_id, ndc_id, similarity_score, rank, validated, validated_by,
                validated_at, selected_classification_id, validation_note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (btr_id, ndc_id, round(score, 4), rank, validated, by, at, cls, note),
        )

    conn.commit()
    return {"candidates": len(scored), "fallback": fallback, "reason": None}


def run_matching(conn, log=print, include_archived: bool = True) -> dict:
    """
    Compute top-K NDC candidates per embedded BTR measure and rewrite
    measure_matches. Existing analyst validations are preserved by re-attaching
    them where the same (btr_id, ndc_id) pair reappears.

    By default ALL NDC/LTS generations are candidates — BTR measures often
    date back years and may only appear in earlier climate strategies.
    Pass include_archived=False to restrict candidates to Active documents.

    Fallback chain per BTR document when the primary pool is empty:
      1. National measures (all generations by default)
      2. Active European Union measures (EU member states, joint EU NDC)
      3. Archived national measures (only relevant in Active-only mode)
    """
    # Preserve prior validations before clearing
    prior = {
        (r[0], r[1]): (r[2], r[3], r[4])
        for r in conn.execute(
            """SELECT btr_id, ndc_id, validated, validated_by, validated_at
               FROM measure_matches WHERE validated IS NOT NULL"""
        )
    }
    conn.execute("DELETE FROM measure_matches")

    # Load NDC quote vectors grouped by country. `primary` holds the default
    # candidate pool; `archived` is the last-resort fallback (empty when
    # include_archived, since everything lands in primary then). A quote
    # counts as Active if any of its documents is Active.
    primary: dict[str, list[tuple[int, list[float]]]] = {}
    archived: dict[str, list[tuple[int, list[float]]]] = {}
    for qid, country, statuses, blob in conn.execute(
        """SELECT id, country, doc_statuses, embedding FROM ndc_quotes
           WHERE embedding IS NOT NULL"""
    ):
        is_active = "Active" in (statuses or "")
        pool = primary if (include_archived or is_active) else archived
        pool.setdefault(country, []).append((qid, blob_to_vec(blob)))

    # BTR measures with vectors, joined to their document's country
    btr_rows = conn.execute(
        """SELECT m.id, d.country, m.embedding
           FROM btr_measures m JOIN documents d ON d.id = m.document_id
           WHERE m.embedding IS NOT NULL
           ORDER BY d.country, m.id"""
    ).fetchall()

    stats = {"matched": 0, "eu_fallback": set(),
             "archived_fallback": set(), "no_candidates": set()}

    for btr_id, btr_country, blob in btr_rows:
        # documents.country is stored in Tracker naming since the countries
        # module was introduced; to_tracker_name also covers older DB states.
        ndc_country = to_tracker_name(btr_country)
        candidates = primary.get(ndc_country)

        if not candidates and ndc_country in EU_MEMBERS:
            candidates = primary.get(EU_TRACKER_NAME)
            if candidates:
                stats["eu_fallback"].add(btr_country)

        if not candidates:
            candidates = archived.get(ndc_country)
            if candidates:
                stats["archived_fallback"].add(btr_country)

        if not candidates:
            stats["no_candidates"].add(btr_country)
            continue

        vec = blob_to_vec(blob)
        scored = sorted(
            ((dot(vec, cvec), cid) for cid, cvec in candidates), reverse=True
        )[:TOP_K]

        for rank, (score, ndc_id) in enumerate(scored, 1):
            validated, by, at = prior.get((btr_id, ndc_id), (None, None, None))
            conn.execute(
                """INSERT INTO measure_matches
                   (btr_id, ndc_id, similarity_score, rank,
                    validated, validated_by, validated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (btr_id, ndc_id, round(score, 4), rank, validated, by, at),
            )
        stats["matched"] += 1

    conn.commit()

    log(f"Matched {stats['matched']} BTR measures (top-{TOP_K} candidates each).")
    if stats["eu_fallback"]:
        log(f"EU-NDC fallback used for: {', '.join(sorted(stats['eu_fallback']))}")
    if stats["archived_fallback"]:
        log(f"Archived-NDC fallback (no Active document in Tracker) used for: "
            f"{', '.join(sorted(stats['archived_fallback']))}")
    if stats["no_candidates"]:
        log(f"No NDC candidates for: {', '.join(sorted(stats['no_candidates']))}")
    return stats
