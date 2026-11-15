"""The files kept for the Inbox (docs/CRIPTOGRAFIA.md, docs/BACKUP.md).

    python -m cli.media status             # records, files by key version, queues
    python -m cli.media cleanup            # one sweep now
    python -m cli.media purge              # every file and name gone (service stopped)
    python -m cli.media normalize-queues   # dead-letter and quarantine without media

Prints counts only — never a file name or a byte of content.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from core.integrations import update_queue
from core.repositories import media as media_repo
from core.repositories.connection import connect_async
from core.services import media_cleanup


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m cli.media")
    parser.add_argument("command", choices=("status", "cleanup", "purge", "normalize-queues"))
    return parser


def _counts(done: dict[str, int]) -> str:
    return ", ".join(f"{name}: {count}" for name, count in done.items())


async def _status(conn) -> list[str]:
    found = await media_cleanup.inventory(conn)
    records = found["records"]
    assert isinstance(records, dict)
    lines = ["registros:"]
    lines.extend(f"  {status}: {row['count']} ({row['bytes']} bytes)"
                 for status, row in records.items())
    if not records:
        lines.append("  nenhum")
    files, sizes = found["files_by_key_version"], found["bytes_by_key_version"]
    assert isinstance(files, dict) and isinstance(sizes, dict)
    lines.append("arquivos por versão de chave:")
    lines.extend(f"  versão {version}: {count} ({sizes[version]} bytes)"
                 for version, count in sorted(files.items()))
    if not files:
        lines.append("  nenhum")
    lines.append(f"órfãos: {found['orphans']}")
    lines.append(f"temporários: {found['temporaries']}")
    lines.append(f"ilegíveis: {found['unreadable']}")
    queues = found["queues"]
    assert isinstance(queues, dict)
    lines.append(f"queued_media_updates: {found['queued_media_updates']} ({_counts(queues)})")
    return lines


async def run(conn, argv: list[str]) -> tuple[int, list[str]]:
    """The command on an open connection; returns the exit code and the lines
    it would print."""
    command = _parser().parse_args(argv).command
    if command == "status":
        return 0, await _status(conn)
    if command == "cleanup":
        done = await media_cleanup.sweep(conn)
        return (1 if done["errors"] else 0), [_counts(done)]
    if command == "purge":
        done = await media_cleanup.purge(conn)
        lines = [f"registros expirados: {done['records']}", f"arquivos apagados: {done['files']}",
                 f"temporários apagados: {done['temporaries']}"]
        return 0, lines + await _status(conn)

    async def seen(update_id: str) -> bool:
        return await media_repo.event_seen(conn, update_id)

    done = await update_queue.normalize_media_payloads(seen)
    await conn.commit()
    return 0, [_counts(done)]


async def main(argv: list[str]) -> int:
    conn = await connect_async()
    try:
        code, lines = await run(conn, argv)
    finally:
        await conn.close()
        await update_queue.close()
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
