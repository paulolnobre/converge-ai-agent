"""Converting, checking and locking the encrypted fields (cli.seal_data).

Rows written in clear — by the image before migration 0009, or by it while the
migration ran — are sealed one by one, each with an update conditional on the
value that was read: if the new image wrote the row in between, the update
touches nothing and the new value stands. Then `check` confirms nothing is
left, and `install_locks` makes the database refuse clear text in the old
columns from then on.

Nothing here prints or logs a value: a Problem names the table, the column and
the row, never the content.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from core.crypto import (
    SealedDataError,
    current_key_version,
    fingerprint,
    key_version_of,
    open_sealed,
    seal,
)
from core.sealed_fields import FIELDS, Field, field, read

_TEST_TITLE = "[TESTE] "
_SANITIZED_DETAIL = re.compile(r"^[A-Za-z]+( HTTP [0-9]+)?$")
_PER_KIND = 100  # problems listed per item and field; the rest are counted, not listed


@dataclass
class Problem:
    item: int
    table: str
    column: str
    row_id: int | None
    reason: str

    def __str__(self) -> str:
        where = f" id={self.row_id}" if self.row_id is not None else ""
        return f"item {self.item}: {self.table}.{self.column}{where}: {self.reason}"


async def _before_write(table: str, row_id: int) -> None:
    """Called between reading a row and the conditional update. Nothing in
    production; the tests put a concurrent write here."""


async def _rows(conn, sql: str, params=()) -> list:
    async with conn.cursor() as cur:
        await cur.execute(sql, params)
        return await cur.fetchall()


async def _execute(conn, sql: str, params=()) -> int:
    async with conn.cursor() as cur:
        await cur.execute(sql, params)
        return cur.rowcount


def _unconverted(f: Field) -> str:
    """The state table: a row this field still holds in clear."""
    return f"{f.sealed} IS NULL" if f.required else f"{f.column} IS NOT NULL"


def _fingerprint_column(f: Field) -> str | None:
    if f.table == "contacts" and f.column in ("phone", "email"):
        return f"{f.column}_fp"
    return None


async def convert(conn, batch: int = 500) -> dict[str, int]:
    """Seal every row still in clear. Returns how many were converted per field."""
    counts: dict[str, int] = {}
    for f in FIELDS:
        converted, last = 0, 0
        while True:
            rows = await _rows(
                conn,
                f"SELECT id, {f.column} FROM {f.table} "
                f"WHERE {_unconverted(f)} AND id > %s ORDER BY id LIMIT %s",
                (last, batch),
            )
            for row_id, legacy in rows:
                last = row_id
                await _before_write(f.table, row_id)
                emptied = "''" if f.required else "NULL"
                sets, params = [f"{f.sealed} = %s", f"{f.column} = {emptied}"], [
                    seal(f.context, legacy)]
                fp = _fingerprint_column(f)
                if fp:
                    sets.append(f"{fp} = %s")
                    params.append(fingerprint(f.column, legacy))  # type: ignore[arg-type]
                guard = f"{f.sealed} IS NULL AND " if f.required else ""
                converted += await _execute(
                    conn,
                    f"UPDATE {f.table} SET {', '.join(sets)} "
                    f"WHERE id = %s AND {guard}{f.column} = %s",
                    (*params, row_id, legacy),
                )
            if len(rows) < batch:
                break
        counts[f.context] = converted
    return counts


async def mark_test_appointments(conn) -> int:
    """Mark the test bookings the old image wrote without the mark — before
    their titles are converted, while the title can still be matched."""
    return await _execute(
        conn,
        "UPDATE appointments a SET test_mode = TRUE "
        "WHERE NOT a.test_mode AND a.title_sealed IS NULL AND a.title LIKE %s "
        "  AND (a.contact_id IS NULL OR EXISTS (SELECT 1 FROM contacts c "
        "       WHERE c.id = a.contact_id AND c.test_mode IS NOT NULL))",
        (_TEST_TITLE + "%",),
    )


async def scrub_audit(conn) -> int:
    """Failures recorded before the audit stopped taking content: no file name
    in `target` for a failed upload, and only the kind of error in `detail`."""
    return await _execute(
        conn,
        "UPDATE workspace_actions SET "
        "  target = CASE WHEN action = 'upload' THEN NULL ELSE target END, "
        "  detail = CASE WHEN detail IS NULL OR detail ~ %s THEN detail "
        "                ELSE 'WorkspaceError' END "
        "WHERE NOT succeeded AND ((action = 'upload' AND target IS NOT NULL) "
        "   OR (detail IS NOT NULL AND detail !~ %s))",
        (_SANITIZED_DETAIL.pattern, _SANITIZED_DETAIL.pattern),
    )


def _lock_name(f: Field) -> str:
    return f"{f.table}_{f.column}_sealed_only"


async def _locks_present(conn) -> set[str]:
    rows = await _rows(conn, "SELECT conname FROM pg_constraint WHERE conname LIKE %s",
                       ("%_sealed_only",))
    return {row[0] for row in rows}


async def install_locks(conn) -> None:
    """From here on the database refuses clear text in the old columns. Added
    NOT VALID and then validated; skipped when already there."""
    present = await _locks_present(conn)
    for f in FIELDS:
        name = _lock_name(f)
        if name in present:
            continue
        rule = f"{f.column} = ''" if f.required else f"{f.column} IS NULL"
        await _execute(conn, f"ALTER TABLE {f.table} ADD CONSTRAINT {name} "
                             f"CHECK ({rule}) NOT VALID")
        await _execute(conn, f"ALTER TABLE {f.table} VALIDATE CONSTRAINT {name}")


async def check(conn, key_version: int | None = None, *,
                include_locks: bool = True) -> list[Problem]:
    """Everything the conversion promises, item by item (the spec's numbering):
    1 no clear text left; 2 every required field sealed; 3 every envelope
    opens (in `key_version`, if given); 4 fingerprints match; 5 test bookings
    marked and no file name in a failed upload; 8 the locks in place."""
    problems: list[Problem] = []

    def add(problem: Problem, listed: dict[tuple[int, str], int]) -> None:
        key = (problem.item, f"{problem.table}.{problem.column}")
        listed[key] = listed.get(key, 0) + 1
        if listed[key] <= _PER_KIND:
            problems.append(problem)

    listed: dict[tuple[int, str], int] = {}
    for f in FIELDS:
        left = (f"{f.column} <> ''" if f.required else f"{f.column} IS NOT NULL")
        for (row_id,) in await _rows(conn, f"SELECT id FROM {f.table} WHERE {left} "
                                           f"AND {f.sealed} IS NOT NULL ORDER BY id"):
            add(Problem(1, f.table, f.column, row_id, "texto claro na coluna antiga"), listed)
        if f.required:
            for (row_id,) in await _rows(conn, f"SELECT id FROM {f.table} "
                                               f"WHERE {f.sealed} IS NULL ORDER BY id"):
                add(Problem(2, f.table, f.column, row_id, "campo obrigatório sem _sealed"),
                    listed)
        else:
            for (row_id,) in await _rows(conn, f"SELECT id FROM {f.table} "
                                               f"WHERE {f.column} IS NOT NULL "
                                               f"AND {f.sealed} IS NULL ORDER BY id"):
                add(Problem(1, f.table, f.column, row_id, "texto claro na coluna antiga"),
                    listed)
        for row_id, sealed in await _rows(conn, f"SELECT id, {f.sealed} FROM {f.table} "
                                                f"WHERE {f.sealed} IS NOT NULL ORDER BY id"):
            try:
                open_sealed(f.context, sealed)
            except SealedDataError:
                add(Problem(3, f.table, f.column, row_id, "envelope não abre"), listed)
                continue
            if key_version is not None and key_version_of(sealed) != key_version:
                add(Problem(3, f.table, f.column, row_id,
                            f"envelope na versão {key_version_of(sealed)}, não na {key_version}"),
                    listed)

    phone, email = field("contacts", "phone"), field("contacts", "email")
    for row in await _rows(conn, "SELECT id, phone, phone_sealed, email, email_sealed, "
                                 "phone_fp, email_fp FROM contacts ORDER BY id"):
        for f, legacy, sealed, stored in ((phone, row[1], row[2], row[5]),
                                          (email, row[3], row[4], row[6])):
            try:
                value = read(f, legacy, sealed)
            except SealedDataError:
                continue  # already item 3
            expected = fingerprint(f.column, value)  # type: ignore[arg-type]
            if (bytes(stored) if stored is not None else None) != expected:
                add(Problem(4, "contacts", f"{f.column}_fp", row[0],
                            "impressão digital não bate"), listed)

    title = field("appointments", "title")
    for row_id, legacy, sealed in await _rows(
            conn, "SELECT id, title, title_sealed FROM appointments WHERE NOT test_mode "
                  "ORDER BY id"):
        try:
            text = read(title, legacy, sealed)
        except SealedDataError:
            continue
        if text and text.startswith(_TEST_TITLE):
            add(Problem(5, "appointments", "test_mode", row_id,
                        "agendamento de teste sem a marca"), listed)
    for (row_id,) in await _rows(conn, "SELECT id FROM workspace_actions WHERE NOT succeeded "
                                       "AND action = 'upload' AND target IS NOT NULL"):
        add(Problem(5, "workspace_actions", "target", row_id,
                    "falha de upload com o nome do arquivo"), listed)

    if include_locks:
        present = await _locks_present(conn)
        for f in FIELDS:
            if _lock_name(f) not in present:
                problems.append(Problem(8, f.table, f.column, None, "trava ausente"))
    # The room has no legacy clear-text column. Its context also binds the row id.
    if await _rows(conn, "SELECT to_regclass('admin_room_messages')") != [(None,)]:
        for row_id, sealed in await _rows(conn, "SELECT id, body_enc FROM admin_room_messages ORDER BY id"):
            try:
                open_sealed(f"admin_room_messages.body:{row_id}", sealed)
                if key_version is not None and key_version_of(sealed) != key_version:
                    add(Problem(3, 'admin_room_messages', 'body_enc', row_id,
                                'envelope em outra versão'), listed)
            except SealedDataError:
                add(Problem(3, 'admin_room_messages', 'body_enc', row_id, 'envelope não abre'), listed)
    return problems


async def rekey(conn) -> int:
    """Re-encrypt every envelope that is not in the current key. A row the app
    rewrote in between is left alone: it is already in the current key."""
    current, moved = current_key_version(), 0
    for f in FIELDS:
        for row_id, sealed in await _rows(conn, f"SELECT id, {f.sealed} FROM {f.table} "
                                                f"WHERE {f.sealed} IS NOT NULL ORDER BY id"):
            if key_version_of(sealed) == current:
                continue
            value = open_sealed(f.context, sealed)
            await _before_write(f.table, row_id)
            moved += await _execute(
                conn,
                f"UPDATE {f.table} SET {f.sealed} = %s WHERE id = %s AND {f.sealed} = %s",
                (seal(f.context, value), row_id, sealed),
            )
    if await _rows(conn, "SELECT to_regclass('admin_room_messages')") != [(None,)]:
        for row_id, sealed in await _rows(conn, "SELECT id, body_enc FROM admin_room_messages ORDER BY id"):
            if key_version_of(sealed) != current:
                context = f"admin_room_messages.body:{row_id}"
                value = open_sealed(context, sealed)
                await _before_write('admin_room_messages', row_id)
                moved += await _execute(conn, "UPDATE admin_room_messages SET body_enc = %s "
                    "WHERE id = %s AND body_enc = %s", (seal(context, value), row_id, sealed))
    return moved


async def reindex(conn) -> int:
    """Recompute the phone and e-mail fingerprints with the current index key.
    Run with the bot stopped (docs/CRIPTOGRAFIA.md)."""
    phone, email = field("contacts", "phone"), field("contacts", "email")
    changed = 0
    for row in await _rows(conn, "SELECT id, phone, phone_sealed, email, email_sealed, "
                                 "phone_fp, email_fp FROM contacts ORDER BY id"):
        new_phone = fingerprint("phone", read(phone, row[1], row[2]))
        new_email = fingerprint("email", read(email, row[3], row[4]))
        old = tuple(bytes(v) if v is not None else None for v in (row[5], row[6]))
        if old == (new_phone, new_email):
            continue
        changed += await _execute(
            conn,
            "UPDATE contacts SET phone_fp = %s, email_fp = %s WHERE id = %s "
            "AND phone IS NOT DISTINCT FROM %s AND phone_sealed IS NOT DISTINCT FROM %s "
            "AND email IS NOT DISTINCT FROM %s AND email_sealed IS NOT DISTINCT FROM %s",
            (new_phone, new_email, row[0], row[1], row[2], row[3], row[4]),
        )
    return changed
