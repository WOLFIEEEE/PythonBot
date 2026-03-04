"""
Kite Connect authentication — login flow, token caching, session verification.

Supports two modes:
  1. Auto mode (default) — spins up a local HTTP server on port 5555 to
     capture the OAuth redirect automatically. You just log in to
     Zerodha in the browser — no copy-pasting needed.
  2. Manual fallback — if the local server fails, falls back to the
     original paste-the-request-token flow.
"""

from __future__ import annotations

import http.server
import os
import threading
import webbrowser
from datetime import date
from urllib.parse import parse_qs, urlparse

from kiteconnect import KiteConnect, exceptions as kite_exc

from config.settings import ACCESS_TOKEN_FILE, KITE_API_KEY, KITE_API_SECRET
from utils.helpers import retry
from utils.logger import get_logger

log = get_logger(__name__)

LOCAL_REDIRECT_PORT = 5555
LOCAL_REDIRECT_URL = f"http://127.0.0.1:{LOCAL_REDIRECT_PORT}/callback"


# ── Token cache ──────────────────────────────────────────────────────
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


# ── Auto-redirect capture ────────────────────────────────────────────
class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Tiny HTTP handler that captures the request_token from the redirect."""

    request_token: str | None = None

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if "request_token" in params:
            _CallbackHandler.request_token = params["request_token"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            html = (
                "<html><body style='font-family:sans-serif;text-align:center;"
                "padding:60px;background:#0d1117;color:#c9d1d9'>"
                "<h1 style='color:#3fb950'>Authentication Successful</h1>"
                "<p>You can close this tab and return to the bot.</p>"
                "</body></html>"
            )
            self.wfile.write(html.encode())
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Missing request_token.")

    def log_message(self, format: str, *args) -> None:
        # Silence HTTP logs
        pass


def _capture_request_token(login_url: str) -> str | None:
    """
    Start a local HTTP server, open the login URL in the browser,
    and wait for the redirect with the request_token.
    Returns the token or None on failure/timeout.
    """
    _CallbackHandler.request_token = None
    server = http.server.HTTPServer(("127.0.0.1", LOCAL_REDIRECT_PORT), _CallbackHandler)
    server.timeout = 180  # 3-minute timeout

    log.info("Starting local auth server on port %d...", LOCAL_REDIRECT_PORT)

    try:
        webbrowser.open(login_url)
        print(f"\nBrowser opened for login. Waiting for authentication...")
        print(f"(If the browser didn't open, visit: {login_url})\n")

        # Handle requests until we get the token or timeout
        while _CallbackHandler.request_token is None:
            server.handle_request()
            if _CallbackHandler.request_token:
                break

    except Exception as exc:
        log.warning("Auto-capture failed: %s", exc)
        return None
    finally:
        server.server_close()

    return _CallbackHandler.request_token


# ── Main auth function ───────────────────────────────────────────────
def authenticate(redirect_url: str | None = None) -> KiteConnect:
    """
    Perform Kite Connect authentication and return an authenticated
    KiteConnect instance.

    Flow:
      1. Try cached token → verify with kite.profile()
      2. Try auto-redirect capture with local HTTP server
      3. Fall back to manual request_token input
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

    # Build login URL
    login_url = kite.login_url()

    # If a redirect URL is provided, append it to login
    effective_redirect = redirect_url or LOCAL_REDIRECT_URL

    # Try auto-capture
    request_token = None
    try:
        request_token = _capture_request_token(login_url)
    except Exception as exc:
        log.warning("Auto-capture unavailable: %s", exc)

    # Fall back to manual
    if not request_token:
        print("\n" + "=" * 60)
        print("Automatic redirect capture failed.")
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
