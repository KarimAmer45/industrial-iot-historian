"""Shared helpers: database connection and the sources file."""
import os
import pathlib

import psycopg
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_URL = ""  # libpq PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE are also supported


def sources() -> dict:
    """config/sources.yaml as a dict. Every process number comes from here."""
    return yaml.safe_load((ROOT / "config" / "sources.yaml").read_text(encoding="utf-8"))


def connect(app_user: str = "ingest") -> psycopg.Connection:
    """Connect and tag the session so audit.change_log records who did it."""
    conn = psycopg.connect(os.environ.get("DATABASE_URL", DEFAULT_URL),connect_timeout=10)
    with conn.cursor() as cur:
        cur.execute("SET TIME ZONE 'UTC'")
        cur.execute("SELECT set_config('app.user', %s, false)", (app_user,))
    return conn


def reset_identity(cur, table: str, column: str) -> None:
    """After inserting explicit ids, move the identity sequence past the max id."""
    cur.execute(
        f"SELECT setval(pg_get_serial_sequence(%s, %s), coalesce((SELECT max({column}) FROM {table}), 0) + 1, false)",
        (table, column),
    )
