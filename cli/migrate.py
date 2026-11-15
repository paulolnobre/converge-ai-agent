"""Apply the schema changes this database has not seen yet, each exactly once.

infra/init.sql is the baseline: Postgres runs it once, when the volume is
first created, and it is not edited again. Every change after it is a file in
infra/migrations/, named so that sorting them is applying them in order
(0001_add_x.sql, 0002_...). Each runs in its own transaction and is recorded in
schema_migrations; a file that fails leaves nothing half-applied, and the
deploy stops before the new version starts.

Why not re-run init.sql on every deploy: it is almost idempotent, and almost is
the problem — its data backfills would move leads between funnel stages on
every release, over whatever a person set in the panel.

    python -m cli.migrate            # apply what is pending
    python -m cli.migrate --status   # list applied and pending, change nothing
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import LiteralString, cast

from core.repositories.connection import connect_async

MIGRATIONS = Path(__file__).resolve().parent.parent / "infra" / "migrations"

# Any constant works; it only has to be the same for every replica, so two
# deploys racing each other take turns instead of applying a file twice.
_LOCK_KEY = 7_411_203


def pending(available: list[str], applied: set[str]) -> list[str]:
    return [name for name in sorted(available) if name not in applied]


def _files() -> list[str]:
    return [path.name for path in MIGRATIONS.glob("*.sql")]


async def main() -> int:
    conn = await connect_async()
    try:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " name TEXT PRIMARY KEY,"
            " applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
        )
        await conn.commit()
        await conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))
        try:
            cur = await conn.execute("SELECT name FROM schema_migrations")
            applied = {row[0] for row in await cur.fetchall()}
            todo = pending(_files(), applied)
            await conn.commit()

            if "--status" in sys.argv:
                for name in sorted(applied):
                    print(f"applied  {name}")
                for name in todo:
                    print(f"pending  {name}")
                return 0

            for name in todo:
                # Trusted like source code: it ships in the image, from the repo.
                sql = cast(LiteralString, (MIGRATIONS / name).read_text(encoding="utf-8"))
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (name) VALUES (%s)", (name,)
                    )
                print(f"applied  {name}")
            if not todo:
                print("Schema is up to date.")
            return 0
        finally:
            await conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
            await conn.commit()
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
