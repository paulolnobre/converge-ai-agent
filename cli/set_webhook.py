"""Point Telegram at this deployment (or show what it is currently pointing at).

    python cli/set_webhook.py            # register PUBLIC_BASE_URL
    python cli/set_webhook.py --show     # what Telegram thinks it should call
    python cli/set_webhook.py --delete   # stop delivery (local debugging)
"""
import asyncio
import sys

import httpx

from core.config import TELEGRAM_URL, settings


async def main() -> int:
    if not settings.telegram_bot_token:
        print("TELEGRAM_BOT_TOKEN is not set.")
        return 1

    async with httpx.AsyncClient(timeout=30.0) as client:
        if "--show" in sys.argv:
            response = await client.get(f"{TELEGRAM_URL}/getWebhookInfo")
            print(response.json())
            return 0
        if "--delete" in sys.argv:
            response = await client.post(f"{TELEGRAM_URL}/deleteWebhook")
            print(response.json())
            return 0

        if not settings.public_base_url:
            print("PUBLIC_BASE_URL is not set.")
            return 1
        if not settings.telegram_webhook_secret:
            print("TELEGRAM_WEBHOOK_SECRET is not set — refusing to register an open webhook.")
            return 1

        response = await client.post(
            f"{TELEGRAM_URL}/setWebhook",
            json={
                "url": f"{settings.public_base_url.rstrip('/')}/telegram/webhook",
                "secret_token": settings.telegram_webhook_secret,
                # Everything else Telegram could send is noise this bot has no
                # handler for, and each one costs a request.
                "allowed_updates": ["message", "edited_message", "callback_query"],
                "drop_pending_updates": False,
            },
        )
        print(response.json())
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
