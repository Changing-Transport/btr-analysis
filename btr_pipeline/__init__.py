"""
btr_pipeline — extraction of transport-relevant measures from BTR CTF-NDC tables.

Modules:
    db       — data-access layer (SQLite file or PostgreSQL, see pg.py)
    pg       — PostgreSQL connection, dialect bridge and schema
    copydb   — SQLite -> PostgreSQL copy with verification
    extract  — per-file Table 5 parsing, relevance screening, normalisation

Entry point: pipeline.py in the repository root (batch / ingest / classify / stats).
"""

__version__ = "0.2.0"
