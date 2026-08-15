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


class RateLimited(RuntimeError):
    """CCP is throttling this application's requests right now (420/429)."""


def check_response(resp: requests.Response, client_id: str = "") -> requests.Response:
    """Like resp.raise_for_status(), but names the one failure mode worth a
    different message.

    A 420/429 means THIS APPLICATION's own budget is spent, not anything the
    calling user personally did -- and for the shared default Client ID this
    app ships with (see config.DEFAULT_CLIENT_ID), "this application" can
    mean many people's combined usage landing on one shared application. ESI
    itself keys its modern rate limits per (application, character), so one
    busy stranger cannot exhaust another user's allowance this way -- but the
    older 420 error-limit's own keying is not clearly documented, and CCP can
    suspend a shared application outright if its aggregate behaviour looks
    abusive, which breaks it for everyone at once with no way to tell from
    inside the app whether that is what happened. Either way the fix from
    here is the same: a personal application has its own, separate budget.

    Everything else still raises the normal requests.HTTPError, with ESI's
    own error body attached, exactly as raise_for_status() always did.
    """
    if resp.status_code in (420, 429):
        shared = bool(client_id) and client_id == config.DEFAULT_CLIENT_ID
        hint = (
            " This app ships with a shared default application so signing in "
            "works immediately; if it is busy, create your own free one in "
            "Settings > EVE account (a couple of minutes, no review needed) "
            "for your own separate allowance."
            if shared else
            " Wait a bit before retrying."
        )
        raise RateLimited(
            f"EVE is rate-limiting requests right now (HTTP {resp.status_code})."
            + hint)
    resp.raise_for_status()
    return resp


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
            b"<h2>Eve-Strait</h2><p>Authentication complete. "
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
    check_response(resp, client_id)
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
    check_response(resp, client_id)
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


# -- multi-character token store -------------------------------------------
# tokens.json: {"active": <character_id>, "characters": {"<id>": {...token...}}}
# The old single-character token.json is migrated in on first load.

def _read_store() -> dict:
    if config.TOKENS_PATH.exists():
        try:
            data = json.loads(config.TOKENS_PATH.read_text("utf-8"))
            if isinstance(data, dict) and "characters" in data:
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return _migrate_legacy_token()


def _migrate_legacy_token() -> dict:
    """Carry a pre-multi-character token.json into the new store."""
    store = {"active": None, "characters": {}}
    if not config.TOKEN_PATH.exists():
        return store
    try:
        token = Token.from_dict(json.loads(config.TOKEN_PATH.read_text("utf-8")))
    except (json.JSONDecodeError, OSError, TypeError):
        return store
    store["characters"][str(token.character_id)] = token.to_dict()
    store["active"] = token.character_id
    _write_store(store)      # leave token.json in place as a backup
    return store


def _write_store(store: dict) -> None:
    try:
        config.TOKENS_PATH.write_text(json.dumps(store, indent=2), "utf-8")
    except OSError:
        pass


def load_all() -> dict[int, Token]:
    """Every linked character, keyed by character_id."""
    out: dict[int, Token] = {}
    for cid, raw in (_read_store().get("characters") or {}).items():
        try:
            out[int(cid)] = Token.from_dict(raw)
        except (TypeError, ValueError):
            continue
    return out


def active_character_id() -> int | None:
    store = _read_store()
    active = store.get("active")
    if active is not None and str(active) in (store.get("characters") or {}):
        return int(active)
    chars = store.get("characters") or {}
    return int(next(iter(chars))) if chars else None


def load_saved() -> Token | None:
    """The active character's token, or None when nothing is linked."""
    cid = active_character_id()
    return load_all().get(cid) if cid is not None else None


def save(token: Token, make_active: bool = True) -> None:
    """Add or replace one character's token."""
    store = _read_store()
    store.setdefault("characters", {})[str(token.character_id)] = token.to_dict()
    if make_active or store.get("active") is None:
        store["active"] = token.character_id
    _write_store(store)


def set_active(character_id: int) -> None:
    store = _read_store()
    if str(character_id) in (store.get("characters") or {}):
        store["active"] = character_id
        _write_store(store)


def remove(character_id: int) -> None:
    """Unlink one character."""
    store = _read_store()
    store.get("characters", {}).pop(str(character_id), None)
    if store.get("active") == character_id:
        chars = store.get("characters") or {}
        store["active"] = int(next(iter(chars))) if chars else None
    _write_store(store)


def logout() -> None:
    """Unlink every character."""
    config.TOKEN_PATH.unlink(missing_ok=True)
    config.TOKENS_PATH.unlink(missing_ok=True)
