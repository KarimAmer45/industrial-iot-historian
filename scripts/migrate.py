"""Transactional local migration runner; use Flyway for Docker deployments.

The local runner maintains its own checksum ledger. Do not mix runners on one database.
Versioned files (V<n>__*.sql) are append-only: the runner refuses an edited, missing or
out-of-order versioned file. Repeatable files (R__*.sql) re-run when they change or when
a versioned migration was applied in the same run, so new tables also receive their grants.
"""
import hashlib
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ingest'))
from common import connect

VERSIONED = re.compile(r'^V(\d+)__.+\.sql$')


def version(name):
    return int(VERSIONED.match(name).group(1))


def main():
    migrations = ROOT / 'db/migrations'
    misnamed = sorted(p.name for p in migrations.glob('V*.sql') if not VERSIONED.match(p.name))
    if misnamed:
        raise SystemExit(f'Versioned migration names must look like V<number>__<description>.sql: {", ".join(misnamed)}')
    versioned = sorted((p for p in migrations.glob('V*.sql')), key=lambda p: version(p.name))
    numbers = [version(p.name) for p in versioned]
    duplicates = sorted({n for n in numbers if numbers.count(n) > 1})
    if duplicates:
        raise SystemExit(f'Duplicate migration version(s): {", ".join(map(str, duplicates))}')
    repeatable = sorted(migrations.glob('R__*.sql'))
    with connect('migrate') as conn:
        conn.execute("SELECT pg_advisory_xact_lock(71001001)")
        exists = conn.execute("SELECT to_regclass('public.flyway_schema_history')").fetchone()[0]
        if exists:
            raise SystemExit('This database uses Flyway. Run Flyway migrations instead.')
        conn.execute('CREATE TABLE IF NOT EXISTS public.project_migration (name text PRIMARY KEY, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())')
        ledger = dict(conn.execute('SELECT name, sha256 FROM public.project_migration').fetchall())
        applied_versions = [version(n) for n in ledger if VERSIONED.match(n)]
        present = {p.name for p in versioned}
        missing = sorted(n for n in ledger if VERSIONED.match(n) and n not in present)
        if missing:
            raise RuntimeError(f'Applied migration file missing or renamed: {", ".join(missing)}')
        highest = max(applied_versions, default=0)
        applied_now = False
        for path in versioned:
            sql = path.read_text(encoding='utf-8')
            checksum = hashlib.sha256(sql.encode()).hexdigest()
            old = ledger.get(path.name)
            if old == checksum:
                continue
            if old:
                raise RuntimeError(f'Applied migration changed: {path.name}')
            if version(path.name) <= highest:
                raise RuntimeError(f'Out-of-order migration {path.name}: version {highest} is already applied')
            conn.execute(sql)
            conn.execute('INSERT INTO public.project_migration(name,sha256) VALUES(%s,%s)', (path.name, checksum))
            highest = version(path.name)
            applied_now = True
            print(f'Applied {path.name}')
        for path in repeatable:
            sql = path.read_text(encoding='utf-8')
            checksum = hashlib.sha256(sql.encode()).hexdigest()
            if ledger.get(path.name) == checksum and not applied_now:
                continue
            conn.execute(sql)
            conn.execute('INSERT INTO public.project_migration(name,sha256) VALUES(%s,%s) ON CONFLICT(name) DO UPDATE SET sha256=EXCLUDED.sha256, applied_at=now()', (path.name, checksum))
            print(f'Applied {path.name}')


if __name__ == '__main__':
    main()
