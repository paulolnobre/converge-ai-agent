"""Prove the workspace credentials actually work, before the first real message.

Read-only: it lists, it never writes. Run it after configuring `.env` and again
after any credential rotation.

    python cli/smoke_workspace.py
"""
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from core.config import settings
from core.integrations import workspace
from core.services import scheduling


async def main() -> int:
    report = workspace.configuration_report()
    print(f"provider per capability: {report['capabilities']}")
    print(f"credentials:             {report['providers']}")
    if report["status"] != "configured":
        print(f"\nMissing credentials for: {', '.join(report['missing'])}")
        return 1

    failures = 0
    zone = ZoneInfo(settings.office_timezone)
    now = datetime.now(tz=zone)

    calendars = scheduling.calendars_in_scope()
    if calendars:
        try:
            busy = await workspace.calendar().busy_periods(
                calendars, now, now + timedelta(days=7)
            )
            total = sum(len(periods) for periods in busy.values())
            print(f"calendar:  ok — {len(calendars)} calendars, {total} busy periods")
            slots = await scheduling.offer_times()
            print(f"           would offer: {scheduling.phrase(slots) or 'nothing'}")
        except Exception as exc:
            failures += 1
            print(f"calendar:  FAILED — {exc}")
    else:
        print("calendar:  skipped — RECEPTION_CALENDAR_ID not set")

    if settings.reception_mailbox:
        try:
            messages = await workspace.mail().list_messages(
                settings.reception_mailbox, limit=5
            )
            print(f"mail:      ok — {len(messages)} recent messages readable")
        except Exception as exc:
            failures += 1
            print(f"mail:      FAILED — {exc}")
    else:
        print("mail:      skipped — RECEPTION_MAILBOX not set")

    try:
        found = await workspace.files().list_files(
            folder_id=settings.documents_root_folder_id, limit=5
        )
        print(f"files:     ok — {len(found)} items visible")
    except Exception as exc:
        failures += 1
        print(f"files:     FAILED — {exc}")

    await workspace.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
