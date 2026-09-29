"""Google OAuth 2.0 authorization-code flow (server side, no extra dependencies)."""
from urllib.parse import urlencode

import httpx

from . import config

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
CALLBACK_PATH = "/auth/google/callback"
STATE_COOKIE = "oauth_state"


class GoogleAuthError(RuntimeError):
    pass


def enabled() -> bool:
    return bool(config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET)


def authorization_url(redirect_uri: str, state: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "client_id": config.GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
    })


async def fetch_profile(code: str, redirect_uri: str, client: httpx.AsyncClient | None = None) -> dict:
    """Exchange the authorization code and return {"email", "name"} for a verified Google account."""
    owns = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))
    try:
        token = await client.post(TOKEN_URL, data={
            "code": code, "client_id": config.GOOGLE_CLIENT_ID, "client_secret": config.GOOGLE_CLIENT_SECRET,
            "redirect_uri": redirect_uri, "grant_type": "authorization_code"})
        if token.status_code != 200 or "access_token" not in token.json():
            raise GoogleAuthError("Google rejected the sign-in code")
        info = await client.get(USERINFO_URL, headers={"Authorization": f"Bearer {token.json()['access_token']}"})
        if info.status_code != 200:
            raise GoogleAuthError("Could not read your Google profile")
        profile = info.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise GoogleAuthError(f"Could not reach Google: {exc.__class__.__name__}")
    finally:
        if owns:
            await client.aclose()
    if not profile.get("email") or profile.get("email_verified") is not True:
        raise GoogleAuthError("Your Google email address is not verified")
    return {"email": profile["email"].strip().lower(), "name": profile.get("name", "")}
