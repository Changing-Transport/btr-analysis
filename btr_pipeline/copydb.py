"""
Copy the SQLite database into PostgreSQL, then verify the copy.

Used once to move the review database to Azure (and again if the hosted copy
ever has to be rebuilt from a snapshot). Rows keep their ids, so every
reference between tables, and every review decision, survives unchanged.

Verification compares, per table, the row count and a SHA-256 digest over
all rows ordered by id (floats rounded to 9 significant digits, binary
embeddings hashed). Any difference aborts with a non-zero exit in the CLI.
"""

import hashlib
import sqlite3

from . import db as dbmod
from . import pg

BATCH = 500


def _columns(conn, table: str) -> list[str]:
    if dbmod.is_postgres(conn):
        rows = conn.execute(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema = ? AND table_name = ?
               ORDER BY ordinal_position""",
            (pg.SCHEMA_NAME, table),
        ).fetchall()
        return [r[0] for r in rows]
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _norm(value):
    if value is None:
        return "∅"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "b:" + hashlib.sha256(bytes(value)).hexdigest()
    if isinstance(value, float):
        return f"f:{value:.9g}"
    if isinstance(value, int):
        return f"i:{value}"
    return f"s:{value}"


def table_digest(conn, table: str, columns: list[str]) -> tuple[int, str]:
    h = hashlib.sha256()
    n = 0
    for row in conn.execute(f"SELECT {', '.join(columns)} FROM {table} ORDER BY id"):
        h.update("\x1f".join(_norm(v) for v in row).encode("utf-8"))
        h.update(b"\x1e")
        n += 1
    return n, h.hexdigest()


def copy_database(src: sqlite3.Connection, dst, replace: bool = False,
                  log=print) -> dict:
    """Copy all tables from src (SQLite) to dst (PostgreSQL) and verify."""
    if not dbmod.is_postgres(dst):
        raise ValueError("The copy target must be a PostgreSQL database.")

    # Bring the source to the current layout first (adds new columns, backfills
    # country codes), so both sides carry the same columns.
    dbmod.init_schema(src)
    dbmod.init_schema(dst)

    non_empty = [t for t in pg.TABLES
                 if dst.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]]
    if non_empty and not replace:
        raise RuntimeError(
            f"Target already holds data in: {', '.join(non_empty)}. "
            f"Pass --replace to overwrite it."
        )
    if non_empty:
        dst.execute(f"TRUNCATE {', '.join(pg.TABLES)} RESTART IDENTITY CASCADE")
        log(f"  emptied target tables: {', '.join(non_empty)}")

    plan: dict[str, list[str]] = {}
    for table in pg.TABLES:
        src_cols = _columns(src, table)
        dst_cols = set(_columns(dst, table))
        missing = [c for c in src_cols if c not in dst_cols]
        if missing:
            raise RuntimeError(f"{table}: target lacks columns {missing}")
        plan[table] = src_cols

    for table, cols in plan.items():
        placeholders = ", ".join("?" for _ in cols)
        sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
        cur = src.execute(f"SELECT {', '.join(cols)} FROM {table} ORDER BY id")
        n = 0
        while True:
            batch = cur.fetchmany(BATCH)
            if not batch:
                break
            dst.executemany(sql, [tuple(r) for r in batch])
            n += len(batch)
        log(f"  {table}: {n} rows copied")

    dst.commit()
    pg.reset_identities(dst)

    report = {}
    ok = True
    for table, cols in plan.items():
        s_n, s_hash = table_digest(src, table, cols)
        d_n, d_hash = table_digest(dst, table, cols)
        same = (s_n == d_n and s_hash == d_hash)
        ok &= same
        report[table] = {"rows": d_n, "identical": same}
        log(f"  verify {table}: {d_n} rows, "
            f"{'identical' if same else f'MISMATCH (source {s_n} rows)'}")
    report["ok"] = ok
    return report
