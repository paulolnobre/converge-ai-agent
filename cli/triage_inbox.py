"""Triage the shared inbox once, from the command line.

The same code the poller runs, so it is the right way to see what the agent
would do with today's mail before letting it do it on a schedule.

    python cli/triage_inbox.py
    python cli/triage_inbox.py --list        # what is still open, most urgent first
"""
import argparse
import asyncio

from core.logger import setup_logging
from core.repositories import mailbox as mailbox_repo
from core.repositories.connection import connect_async
from core.services import mailbox


async def main() -> int:
    parser = argparse.ArgumentParser(description="Triage the reception mailbox.")
    parser.add_argument("--list", action="store_true", help="only show open threads")
    args = parser.parse_args()

    setup_logging()
    conn = await connect_async()
    try:
        if not args.list:
            handled = await mailbox.process_inbox(conn)
            await conn.commit()
            print(f"Triaged {handled} new threads.")

        print("\nOpen threads, most urgent first:")
        for row in await mailbox_repo.open_threads(conn):
            thread_id, subject, category, priority, status, last = row
            print(f"  [{priority}] {status:<10} {category or '-':<14} {subject or '(no subject)'}")
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
