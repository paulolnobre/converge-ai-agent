"""Grant/revoke explicit room membership without changing general panel access.

python -m cli.admin_room add <email>
python -m cli.admin_room remove <email>
python -m cli.admin_room list
Only existing, active panel accounts can be added. No guesses by display name.
"""
import argparse
import asyncio

from core.repositories import admin_room
from core.repositories.connection import connect_async


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('add', 'remove', 'list'))
    parser.add_argument('email', nargs='?')
    args = parser.parse_args()
    if args.action != 'list' and not args.email:
        parser.error('an email is required')
    conn = await connect_async()
    try:
        if args.action == 'list':
            for row in await admin_room.members(conn):
                print(f"{row['id']}: {row['name']}")
            return 0
        cur = await conn.execute('SELECT id FROM panel_users WHERE email = %s AND active',
                                 (args.email.strip().lower(),))
        row = await cur.fetchone()
        if row is None:
            print('No active panel account with this email. Invite the person first.')
            return 1
        if args.action == 'add':
            await conn.execute('INSERT INTO admin_room_members (user_id) VALUES (%s) '
                               'ON CONFLICT DO NOTHING', (row[0],))
        else:
            await conn.execute('DELETE FROM admin_room_members WHERE user_id = %s', (row[0],))
        await conn.commit()
        print('Room membership updated.')
        return 0
    finally:
        await conn.close()


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
