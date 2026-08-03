"""EVE SSO OAuth2 (PKCE, native/public client) flow with a local callback server."""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

from .. import config


@dataclass
class Token:
    access_token: str
    refresh_token: str
    expires_at: float
    character_id: int
    character_name: str
    scopes: list[str]

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at - 30

    def to_dict(self) -> dict:
        return self.__dict__

    @classmethod
    def from_dict(cls, d: dict) -> "Token":
        return cls(**d)


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------
def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _make_pkce() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _decode_jwt_payload(token: str) -> dict:
    """Decode (without verifying) the JWT body to read character id/name/scopes."""
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


# ---------------------------------------------------------------------------
# Local redirect capture
# ---------------------------------------------------------------------------
class _CallbackHandler(BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self):  # noqa: N802
        # Accept the redirect on ANY path (root or /callback); we only need the
        # query string. The redirect_uri string still has to match the app.
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if "code" not in params and "error" not in params:
            self.send_response(204)
            self.end_headers()
            return
        type(self).result = {k: v[0] for k, v in params.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(
            b"<html><body style='font-family:sans-serif;background:#111;color:#ddd'>"
            b"<h2>EVE Jump Planner</h2><p>Authentication complete. "
            b"You can close this tab and return to the app.</p></body></html>"
        )

    def log_message(self, *args):  # silence
        pass


def _capture_code(state: str, port: int, timeout: float = 180.0) -> dict:
    _CallbackHandler.result = {}
    server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.timeout = 1.0
    deadline = time.time() + timeout
    while time.time() < deadline and not _CallbackHandler.result:
        server.handle_request()
    server.server_close()
    result = _CallbackHandler.result
    if not result:
        raise TimeoutError("Timed out waiting for EVE SSO callback.")
    if result.get("state") != state:
        raise ValueError("OAuth state mismatch (possible CSRF).")
    if "code" not in result:
        raise ValueError(f"SSO error: {result}")
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def login(client_id: str, scopes: list[str] | None = None,
          open_browser=webbrowser.open) -> Token:
    """Run the full interactive PKCE login and return a Token."""
    scopes = scopes if scopes is not None else config.get_scopes()
    verifier, challenge = _make_pkce()
    state = secrets.token_urlsafe(16)
    redirect_uri = config.REDIRECT_URI

    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "scope": " ".join(scopes),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
    )
    port = urllib.parse.urlparse(redirect_uri).port or 80
    open_browser(f"{config.SSO_AUTHORIZE}?{query}")
    result = _capture_code(state, port)

    resp = requests.post(
        config.SSO_TOKEN,
        data={
            "grant_type": "authorization_code",
            "code": result["code"],
            "client_id": client_id,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Host": config.SSO_ISSUER},
        timeout=30,
    )
    resp.raise_for_status()
    return _token_from_response(resp.json(), client_id)


def refresh(token: Token, client_id: str) -> Token:
    resp = requests.post(
        config.SSO_TOKEN,
        data={
            "grant_type": "refresh_token",
            "refresh_token": token.refresh_token,
            "client_id": client_id,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Host": config.SSO_ISSUER},
        timeout=30,
    )
    resp.raise_for_status()
    return _token_from_response(resp.json(), client_id)


def _token_from_response(data: dict, client_id: str) -> Token:
    access = data["access_token"]
    claims = _decode_jwt_payload(access)
    char_id = int(str(claims["sub"]).split(":")[-1])
    scopes = claims.get("scp", [])
    if isinstance(scopes, str):
        scopes = [scopes]
    return Token(
        access_token=access,
        refresh_token=data["refresh_token"],
        expires_at=time.time() + int(data.get("expires_in", 1200)),
        character_id=char_id,
        character_name=claims.get("name", "Unknown"),
        scopes=scopes,
    )


def load_saved() -> Token | None:
    if not config.TOKEN_PATH.exists():
        return None
    try:
        return Token.from_dict(json.loads(config.TOKEN_PATH.read_text("utf-8")))
    except (json.JSONDecodeError, OSError, TypeError):
        return None


def save(token: Token) -> None:
    config.TOKEN_PATH.write_text(json.dumps(token.to_dict(), indent=2), "utf-8")


def logout() -> None:
    config.TOKEN_PATH.unlink(missing_ok=True)
