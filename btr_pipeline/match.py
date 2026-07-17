"""
Embedding and semantic matching between BTR measures and NDC/LTS measures.

Embedding:
    * BTR side: auto-accepted or confirmed measures (review_status in
      ('auto', 'confirmed')); text = name + description + objectives.
    * NDC side: ALL measures from the Tracker import (Active and Archived,
      so NDC-generation progression analysis is possible later);
      text = quote + purpose + instrument + category.
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


def ndc_text(quote, purpose, instrument, category) -> str:
    parts = [p for p in (quote, purpose, instrument, category) if p]
    return " | ".join(str(p).strip() for p in parts)[:6000]


# ---------------------------------------------------------------------------
# Embed step
# ---------------------------------------------------------------------------

def embed_missing(conn, log=print) -> dict:
    """Embed all BTR (auto/confirmed) and Active NDC measures lacking a vector."""
    client, deployment = get_embedding_client()

    btr_rows = conn.execute(
        """SELECT id, measure_name, description, objectives FROM btr_measures
           WHERE review_status IN ('auto', 'confirmed') AND embedding IS NULL
           ORDER BY id"""
    ).fetchall()
    ndc_rows = conn.execute(
        """SELECT id, quote, purpose, instrument, category FROM ndc_measures
           WHERE embedding IS NULL
           ORDER BY id"""
    ).fetchall()

    jobs = [("btr_measures", r[0], btr_text(r[1], r[2], r[3])) for r in btr_rows]
    jobs += [("ndc_measures", r[0], ndc_text(r[1], r[2], r[3], r[4])) for r in ndc_rows]

    if not jobs:
        log("Nothing to embed — all vectors present.")
        return {"embedded": 0}

    log(f"Embedding {len(btr_rows)} BTR + {len(ndc_rows)} NDC measures "
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

    # Load NDC vectors grouped by country. `primary` holds the default
    # candidate pool; `archived` is the last-resort fallback (empty when
    # include_archived, since everything lands in primary then).
    primary: dict[str, list[tuple[int, list[float]]]] = {}
    archived: dict[str, list[tuple[int, list[float]]]] = {}
    for mid, country, status, blob in conn.execute(
        """SELECT id, country, doc_status, embedding FROM ndc_measures
           WHERE embedding IS NOT NULL"""
    ):
        pool = primary if (include_archived or status == "Active") else archived
        pool.setdefault(country, []).append((mid, blob_to_vec(blob)))

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
