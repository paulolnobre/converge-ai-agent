"""Load the organization's own content into the running system.

Two things the desk cannot work without, both of which live in `content/` as
files a person edits rather than as rows somebody types:

    the FAQ    chunked, embedded and stored, so answers come from the
               organization instead of from the model
    the team   loaded into `staff`, so escalation has somewhere to go

Safe to re-run. The FAQ is replaced wholesale, which is what makes a deleted
question stop being answerable; the team is matched on e-mail and updated.

    python cli/load_content.py                # both
    python cli/load_content.py --faq-only
    python cli/load_content.py --staff-only
    python cli/load_content.py --check        # what is loaded now, changes nothing
"""
import argparse
import asyncio

from core import channels, content
from core.logger import setup_logging
from core.repositories import contacts as contacts_repo
from core.repositories import knowledge as knowledge_repo
from core.repositories.connection import connect_async
from core.services import knowledge


async def _load_faq(conn) -> None:
    document, source = await knowledge.load_document()
    stored = await knowledge.ingest(conn, document, source)
    print(f"FAQ: {stored} chunks from {source}")


async def _load_staff(conn) -> None:
    team = content.staff()
    if not team:
        print("Staff: nothing in content/staff.json — copy content/staff.example.json "
              "to it and fill in the team (it is kept out of git).")
        return
    for member in team:
        await contacts_repo.upsert_staff(conn, member)
        raw = member.get("whatsapp")
        if raw and not channels.whatsapp_ids(raw):
            print(f"Staff: {member['name']}'s WhatsApp number {raw!r} was not stored — "
                  "write it with the country code, e.g. +55 91 98000-0002.")
    # Second pass: everyone exists now, so the cover-for chain can be linked.
    for member in team:
        await contacts_repo.set_escalation(
            conn, member["email"], member.get("escalates_to")
        )
    print(f"Staff: {len(team)} people")


async def _report(conn) -> None:
    chunks = await knowledge_repo.count(conn)
    print(f"Knowledge chunks stored: {chunks}")
    if not chunks:
        print("  The desk cannot answer a single question until this is loaded.")
    async with conn.cursor() as cur:
        await cur.execute("SELECT name, role, array_length(specialties, 1) FROM staff")
        for name, role, specialties in await cur.fetchall():
            print(f"  {role:<8} {name} ({specialties or 0} specialties)")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Load content/ into the database.")
    parser.add_argument("--faq-only", action="store_true")
    parser.add_argument("--staff-only", action="store_true")
    parser.add_argument("--check", action="store_true", help="report, change nothing")
    args = parser.parse_args()

    setup_logging()
    content.validate()

    conn = await connect_async()
    try:
        if args.check:
            await _report(conn)
            return 0
        if not args.staff_only:
            await _load_faq(conn)
        if not args.faq_only:
            await _load_staff(conn)
        await conn.commit()
        print()
        await _report(conn)
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
