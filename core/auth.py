"""
Kite Connect authentication — login flow, token caching, session verification.
"""

from __future__ import annotations

import os
from datetime import date

from kiteconnect import KiteConnect, exceptions as kite_exc

from config.settings import ACCESS_TOKEN_FILE, KITE_API_KEY, KITE_API_SECRET
from utils.helpers import retry
from utils.logger import get_logger

log = get_logger(__name__)


def _token_cache_path() -> str:
    return ACCESS_TOKEN_FILE


def _read_cached_token() -> str | None:
    """Return today's cached access token, or None."""
    path = _token_cache_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as fh:
            stored_date, token = fh.read().strip().split("|", 1)
            if stored_date == str(date.today()):
                return token
    except (ValueError, OSError):
        pass
    return None


def _save_token(token: str) -> None:
    with open(_token_cache_path(), "w") as fh:
        fh.write(f"{date.today()}|{token}")
    log.info("Access token cached for %s.", date.today())


@retry(max_retries=2, exceptions=(kite_exc.NetworkException,))
def _verify_session(kite: KiteConnect) -> bool:
    """Verify the session by calling kite.profile()."""
    try:
        profile = kite.profile()
        log.info("Session OK — logged in as %s.", profile.get("user_name", "N/A"))
        return True
    except kite_exc.TokenException:
        log.warning("Cached token is invalid or expired.")
        return False


def authenticate() -> KiteConnect:
    """
    Perform Kite Connect authentication and return an authenticated
    KiteConnect instance.

    Flow:
      1. Try cached token → verify with kite.profile()
      2. If no valid cache → prompt user for request_token from browser
    """
    if not KITE_API_KEY or not KITE_API_SECRET:
        raise RuntimeError(
            "KITE_API_KEY and KITE_API_SECRET must be set in config/.env"
        )

    kite = KiteConnect(api_key=KITE_API_KEY)

    # Try cached token
    cached = _read_cached_token()
    if cached:
        kite.set_access_token(cached)
        if _verify_session(kite):
            return kite
        log.info("Cached token invalid — re-authenticating.")

    # Manual login flow
    login_url = kite.login_url()
    print("\n" + "=" * 60)
    print("Open the following URL in your browser to log in:")
    print(login_url)
    print("=" * 60)
    request_token = input("Paste the request_token here: ").strip()

    if not request_token:
        raise RuntimeError("No request_token provided.")

    try:
        session_data = kite.generate_session(
            request_token, api_secret=KITE_API_SECRET
        )
    except kite_exc.TokenException as exc:
        raise RuntimeError(f"Session generation failed: {exc}") from exc

    access_token = session_data["access_token"]
    kite.set_access_token(access_token)
    _save_token(access_token)

    if not _verify_session(kite):
        raise RuntimeError("Session verification failed after login.")

    return kite
