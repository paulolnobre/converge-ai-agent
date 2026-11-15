"""What the queue's quarantine holds, and dropping what is not worth keeping.

    python -m cli.queue_quarantine list
    python -m cli.queue_quarantine drop <id> [<id> ...]

The quarantine keeps queue items that could not be opened (tampered, a key no
longer loaded, broken JSON), sealed. The listing shows the reason, the time and
the size of each — never the content.
"""
from __future__ import annotations

import asyncio
import sys

from core.integrations import update_queue


async def main(argv: list[str]) -> int:
    if argv[:1] == ["list"] and len(argv) == 1:
        items = await update_queue.quarantine_items()
        for item in items:
            print(f"{item['id']}: {item['reason']} · {item['at'] or '-'} · "
                  f"{item['size'] if item['size'] is not None else '?'} caracteres")
        print(f"{len(items)} item(ns) na quarentena")
        return 0
    if argv[:1] == ["drop"] and len(argv) > 1 and all(a.isalnum() for a in argv[1:]):
        dropped = await update_queue.drop_quarantine(argv[1:])
        print(f"{dropped} item(ns) apagado(s)")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
