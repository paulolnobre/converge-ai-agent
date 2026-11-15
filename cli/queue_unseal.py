"""Turn the queues back into plain JSON, for an image of data format 0.

    python -m cli.queue_unseal

Only for going back to an image from before the encryption, after Postgres was
restored from the pre-deploy backup (docs/CRIPTOGRAFIA.md, "Voltar ao formato
0"). It refuses while the bot may be running (a worker heartbeat in the last
60 s), puts back what the stopped bot had reserved, rewrites the active,
deferred and dead-letter lists in clear (an envelope that does not open goes to
the quarantine), and only then drops Redis's data_format marker, so
deploy/deploy.sh accepts the old image again. Run it with the bot stopped and
the image that still decrypts (format 1).
"""
from __future__ import annotations

import asyncio
import sys

from core.integrations import update_queue
from core.services import data_format


async def main(argv: list[str]) -> int:
    if argv:
        print(__doc__)
        return 2
    try:
        if await update_queue.worker_heartbeat_seen():
            print("Recusado: um worker deu sinal de vida nos últimos 60 s. Pare o bot antes.")
            return 1
        # With the bot stopped, what it had reserved goes back to the active
        # list (it was cancelled mid-turn, not acknowledged) and is unsealed too.
        await update_queue.recover_processing()
        changed = await update_queue.unseal_lists()
        await update_queue._redis().delete(data_format.REDIS_KEY)
    finally:
        await update_queue.close()
    print(f"{changed} item(ns) voltaram a JSON; marcador data_format do Redis removido.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
