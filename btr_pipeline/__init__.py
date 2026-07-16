"""
btr_pipeline — extraction of transport-relevant measures from BTR CTF-NDC tables.

Modules:
    db       — SQLite data-access layer (schema portable to PostgreSQL)
    extract  — per-file Table 5 parsing, relevance screening, normalisation

Entry point: pipeline.py in the repository root (batch / ingest / classify / stats).
"""

__version__ = "0.2.0"
