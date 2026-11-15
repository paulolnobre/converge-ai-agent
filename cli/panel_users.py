"""Who can sign in to /painel.

    python -m cli.panel_users invite <email> [--name "Nome"] [--telegram]
    python -m cli.panel_users invite <email> --reset [--telegram]
    python -m cli.panel_users disable <email>
    python -m cli.panel_users enable <email>
    python -m cli.panel_users list
    python -m cli.panel_users disconnect <email>

An invite is a one-time link, valid for 24 hours, where the person sets a
password and registers an authenticator app. --telegram sends it to their
private chat with the bot (their chat_id in content/staff.json) instead of
only printing it. --reset is for a lost phone and lost recovery codes: it
erases the password and the authenticator and signs them out everywhere.
disable signs them out at once and keeps them out.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from core import content
from core.config import settings
from core.integrations import telegram
from core.repositories import panel_auth as auth_repo
from core.repositories.connection import connect_async
from core.services import panel_auth as auth

INVITE_HOURS = 24


async def _staff(conn, email: str) -> tuple[str | None, str | None]:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT name, chat_id FROM staff WHERE lower(email) = lower(%s)", (email,)
        )
        row = await cur.fetchone()
    return (row[0], row[1]) if row else (None, None)


async def invite(conn, args) -> int:
    email = args.email.strip().lower()
    staff_name, chat_id = await _staff(conn, email)
    name = args.name or staff_name
    existing = await auth_repo.user_by_email(conn, email)
    if existing is None and not name:
        print(f"{email} is not in the staff table: pass --name \"Nome\".")
        return 1
    if existing and existing["enrolled_at"] and not args.reset:
        print(f"{email} already has access. --reset erases their password and "
              "authenticator and sends a new invite.")
        return 1

    name = name or (existing["name"] if existing else email)
    user_id = await auth_repo.ensure_user(conn, email, name)
    await auth_repo.reset_enrollment(conn, user_id)
    token = auth.new_token()
    await auth_repo.create_invite(conn, auth.digest(token), user_id, INVITE_HOURS)
    await auth_repo.log_event(conn, "invited", user_id=user_id, email=email, ip=None)
    await conn.commit()

    base = (settings.public_base_url or "").rstrip("/")
    link = f"{base}/painel/convite#{token}"
    if not base:
        print("PUBLIC_BASE_URL is not set — the link below is only a path.")

    if args.telegram:
        if not chat_id or not str(chat_id).lstrip("-").isdigit():
            print(f"No Telegram chat_id for {email} in the staff table; send the link yourself.")
        else:
            text = content.render("panel.invite", nome=name, link=link, horas=INVITE_HOURS)
            if await telegram.send_message(chat_id, text, parse_mode=None):
                print(f"Invite sent to {name} on Telegram (valid {INVITE_HOURS}h).")
                return 0
            print("Telegram did not accept the message (has this person started the bot?).")
    print(f"Invite for {name}, valid {INVITE_HOURS}h, works once:\n{link}")
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m cli.panel_users")
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("invite")
    add.add_argument("email")
    add.add_argument("--name")
    add.add_argument("--reset", action="store_true")
    add.add_argument("--telegram", action="store_true")
    for name in ("disable", "enable", "disconnect"):
        commands.add_parser(name).add_argument("email")
    commands.add_parser("list")
    args = parser.parse_args()

    if args.command == "invite" and not auth.configured():
        print("PANEL_SECRET_KEY is missing or invalid — see .env.example.")
        return 1

    conn = await connect_async()
    try:
        if args.command == "invite":
            return await invite(conn, args)
        if args.command == "disconnect":
            count = await auth_repo.disconnect_devices(conn, args.email)
            user = await auth_repo.user_by_email(conn, args.email.strip())
            await auth_repo.log_event(conn, "device_revoked", user_id=user["id"] if user else None,
                                      email=args.email, ip=None)
            await conn.commit()
            print(f"{count} aparelho(s) desconectado(s).")
            return 0
        if args.command in ("disable", "enable"):
            found = await auth_repo.set_active(conn, args.email, args.command == "enable")
            await conn.commit()
            print(f"{args.email}: {args.command}d." if found else f"{args.email}: no such account.")
            return 0 if found else 1
        for user in await auth_repo.list_users(conn):
            state = ("disabled" if not user["active"] else "locked" if user["locked"]
                     else "active" if user["enrolled_at"] else "invited")
            last = user["last_login_at"].strftime("%d/%m %H:%M") if user["last_login_at"] else "never"
            print(f"{user['name']:<24} {user['email']:<36} {state:<9} last sign-in: {last}")
        return 0
    finally:
        await conn.close()
        await telegram.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
