"""One-off consent for GOOGLE_AUTH_MODE=refresh_token.

The small-office path: no Workspace admin, no domain-wide delegation. One shared
account (obra@…) signs in once here and the refresh token it returns goes into
`.env`, where it keeps working until someone revokes it.

    python cli/authorize_google.py

Needs an OAuth client of type "Desktop app" in Google Cloud Console, with
GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET already in `.env`.
"""
import urllib.parse

import httpx

from core.config import settings
from core.integrations.workspace.oauth import google_scopes

# Google retired the out-of-band flow (the code shown directly in the
# consent page) in 2022. The loopback redirect is what it recommends instead
# for a desktop client — Google accepts any http://localhost[:port] for this
# client type with nothing to register — but nothing here listens on it, so
# the browser lands on a page that fails to load. That is expected: the code
# is in its address bar (?code=...&scope=...), pasted below by hand, same as
# the old flow. Running a local server to catch it automatically is more
# moving parts for a one-off script run once per shared account.
_REDIRECT = "http://localhost"
_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"


def main() -> int:
    if not (settings.google_client_id and settings.google_client_secret):
        print("Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env first.")
        return 1

    query = urllib.parse.urlencode(
        {
            "client_id": settings.google_client_id,
            "redirect_uri": _REDIRECT,
            "response_type": "code",
            "scope": " ".join(google_scopes()),
            # Without both of these Google returns an access token and no
            # refresh token, and the bot stops working in an hour.
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    print("1. Open this URL, sign in as the shared account, and approve:\n")
    print(f"   {_AUTH_URL}?{query}\n")
    print(
        "2. The browser will land on http://localhost and fail to load — that's "
        "expected. Copy the value after 'code=' (and before the next '&') from "
        "its address bar."
    )
    code = input("3. Paste that code here: ").strip()
    if not code:
        print("No code given.")
        return 1

    response = httpx.post(
        _TOKEN_URL,
        data={
            "code": code,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri": _REDIRECT,
            "grant_type": "authorization_code",
        },
        timeout=30.0,
    )
    if response.status_code != 200:
        print(f"Google returned {response.status_code}: {response.text[:300]}")
        return 1

    refresh_token = response.json().get("refresh_token")
    if not refresh_token:
        print("Google returned no refresh token. Revoke the app's access and retry.")
        return 1

    print("\n3. Put this in .env and set GOOGLE_AUTH_MODE=refresh_token:\n")
    print(f"GOOGLE_REFRESH_TOKEN={refresh_token}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
