"""Seal the customer fields still in clear, check, and lock (docs/CRIPTOGRAFIA.md).

    python -m cli.seal_data                    # mark, convert, check; if clean, lock
    python -m cli.seal_data --check [--key-version N]
    python -m cli.seal_data --rekey            # re-encrypt into the first of DATA_KEYS
                                               # (the Inbox's kept files too)
    python -m cli.seal_data --reindex          # recompute fingerprints (bot stopped)

Run by deploy/deploy.sh after every release. Exits 1, listing each problem by
table, column and row — never the content — when the check is not clean.
Every step is safe to repeat.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from core.crypto import current_key_version
from core.integrations import update_queue
from core.repositories.connection import connect_async
from core.services import media_cleanup, sealing


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m cli.seal_data")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--rekey", action="store_true")
    mode.add_argument("--reindex", action="store_true")
    parser.add_argument("--key-version", type=int)
    return parser


async def run(conn, argv: list[str]) -> tuple[int, list[str]]:
    """The command on an open connection; returns the exit code and the lines
    it would print. Commits after each step."""
    args = _parser().parse_args(argv)
    lines: list[str] = []
    if args.check:
        problems = await _check_all(conn, args.key_version)
    elif args.rekey:
        moved = await sealing.rekey(conn)
        await conn.commit()
        moved_in_queues = await update_queue.seal_stored_lists(rekey=True)
        moved_files = await media_cleanup.rekey_files(conn)
        await conn.commit()
        lines.append(f"recifrados: {moved} no banco, {moved_in_queues} nas filas, "
                     f"{moved_files} arquivos")
        problems = await _check_all(conn, current_key_version())
    elif args.reindex:
        changed = await sealing.reindex(conn)
        await conn.commit()
        lines.append(f"impressões digitais recalculadas: {changed}")
        problems = await _check_all(conn)
    else:
        marked = await sealing.mark_test_appointments(conn)
        scrubbed = await sealing.scrub_audit(conn)
        converted = await sealing.convert(conn)
        await conn.commit()
        sealed_in_queues = await update_queue.seal_stored_lists()
        lines.append(f"agendamentos de teste marcados: {marked}")
        lines.append(f"itens cifrados nas filas: {sealed_in_queues}")
        lines.append(f"auditoria limpa: {scrubbed}")
        lines.extend(f"convertidos {name}: {count}" for name, count in converted.items() if count)
        problems = await _check_all(conn, include_locks=False)
        if not problems:
            await sealing.install_locks(conn)
            await conn.commit()
            lines.append("travas instaladas")
            problems = await _check_all(conn)
    await conn.commit()
    lines.extend(str(problem) for problem in problems)
    lines.append("conferência limpa" if not problems else f"{len(problems)} problema(s)")
    return (1 if problems else 0), lines


async def _check_all(conn, key_version: int | None = None, *,
                     include_locks: bool = True) -> list:
    """The database's items and the queues' (6 and 7)."""
    problems: list = await sealing.check(conn, key_version, include_locks=include_locks)
    problems.extend(await update_queue.check_lists(key_version))
    return problems


async def main(argv: list[str]) -> int:
    conn = await connect_async()
    try:
        code, lines = await run(conn, argv)
    finally:
        await conn.close()
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
