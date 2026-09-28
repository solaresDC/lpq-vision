"""The simple session (Arch 25.3, SPEC 1.3): site accounts + admin, cookie session, in RAM.

Accounts this era are TEMPORAL by design: one admin and one site account ('demo'), whose
salted PBKDF2 hashes live as the .env keys ADMIN_PASSWORD_HASH / DEMO_PASSWORD_HASH (values
typed only by the human; .env never touches git). FASE 3 moves accounts to the database and
deletes the keys with zero residue.

Sessions live in this process's RAM: random ids in a dict, born fresh each boot. A restart logs
everyone out, harmlessly; there is NO signing secret anywhere and NO idle expiry (a session lives
until logout or restart). This depends on the api being ONE uvicorn process, which it is and
must remain this era. Nothing auth-related is ever logged beyond the user name.

The site rule: a site account gets every list endpoint FORCED to its site; admin sees all.
"""

from __future__ import annotations

import asyncio
import getpass
import hashlib
import hmac
import logging
import os
import secrets
import sys
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Request, Response

from brain.validator.models import LoginRequest

log = logging.getLogger("lpq.api.auth")

# --- named constants (SPEC section 4) -------------------------------------------------
PBKDF2_ITERATIONS = 600000
HASH_SCHEME = "pbkdf2_sha256"
SESSION_COOKIE = "lpq_session"
ADMIN_USER = "admin"
# user name -> the .env key holding its hash. The user name of a site account IS its site.
ENV_HASH_KEYS: dict[str, str] = {
    ADMIN_USER: "ADMIN_PASSWORD_HASH",
    "demo": "DEMO_PASSWORD_HASH",
}


@dataclass(frozen=True)
class Session:
    user: str
    site: str | None      # None = admin: every site
    is_admin: bool


# The RAM store: session id -> Session. Disposable by construction (SPEC 1.5 pattern).
_SESSIONS: dict[str, Session] = {}


# --- hashing --------------------------------------------------------------------------

def make_hash(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    """pbkdf2_sha256$<iterations>$<salt>$<hex>: stdlib only, a fresh 16-byte salt each time."""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("ascii"), iterations)
    return f"{HASH_SCHEME}${iterations}${salt}${digest.hex()}"


def verify_hash(password: str, stored: str) -> bool:
    """Constant-time check of a password against a stored hash; any malformed hash is a miss."""
    try:
        scheme, iterations_s, salt, expected = stored.strip().strip("'\"").split("$")
        iterations = int(iterations_s)
    except (ValueError, AttributeError):
        return False
    if scheme != HASH_SCHEME or iterations < 1:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("ascii"), iterations)
    return hmac.compare_digest(digest.hex(), expected)


def _stored_hash(user: str) -> str | None:
    """The hash for a known user from the environment; unknown user or unset key = None. Never logged."""
    key = ENV_HASH_KEYS.get(user)
    if key is None:
        return None
    value = os.environ.get(key, "").strip()
    return value or None


def _session_for(user: str) -> Session:
    if user == ADMIN_USER:
        return Session(user=user, site=None, is_admin=True)
    return Session(user=user, site=user, is_admin=False)


# --- the dependency and the site rule -------------------------------------------------

def require_session(request: Request) -> Session:
    """FastAPI dependency: the Session behind the cookie, or 401 (the pages show the login form)."""
    sid = request.cookies.get(SESSION_COOKIE)
    session = _SESSIONS.get(sid) if sid else None
    if session is None:
        raise HTTPException(status_code=401, detail="sesion requerida")
    return session


def scoped_site(session: Session, requested: str | None) -> str | None:
    """The site a list endpoint may show: a site account is FORCED to its own site (whatever it
    asked for); admin gets what it asked for, or None = all sites."""
    if session.is_admin:
        return requested or None
    return session.site


# --- the endpoints --------------------------------------------------------------------

def _check_password(password: str, stored: str | None) -> bool:
    """One PBKDF2 round ALWAYS (a dummy hash when the user is unknown, so timing does not reveal
    which user names exist), then the constant-time compare. Runs in a thread: a login never
    freezes the api's event loop."""
    target = stored if stored is not None else make_hash("")
    return verify_hash(password, target) and stored is not None


router = APIRouter()


@router.post("/api/login")
async def login(body: LoginRequest, response: Response) -> dict:
    user = body.user.strip()
    ok = await asyncio.to_thread(_check_password, body.password, _stored_hash(user))
    if not ok:
        log.info("login rejected user=%s", user)
        raise HTTPException(status_code=401, detail="usuario o contrasena incorrectos")
    sid = secrets.token_urlsafe(32)
    session = _session_for(user)
    _SESSIONS[sid] = session
    response.set_cookie(SESSION_COOKIE, sid, httponly=True, samesite="lax", path="/")
    log.info("login ok user=%s admin=%s", session.user, session.is_admin)
    return {"user": session.user, "site": session.site, "admin": session.is_admin}


@router.post("/api/logout")
async def logout(request: Request, response: Response) -> dict:
    sid = request.cookies.get(SESSION_COOKIE)
    if sid:
        _SESSIONS.pop(sid, None)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/api/session")
async def session_info(request: Request) -> dict:
    """Who am I: lets a page restore its locked site chip after a reload; 401 when no session."""
    session = require_session(request)
    return {"user": session.user, "site": session.site, "admin": session.is_admin}


# --- the listed hash-making tool (run by the HUMAN, interactively; the hash never enters a chat)

def _cli() -> int:
    args = sys.argv[1:]
    if len(args) != 2 or args[0] != "make-hash" or args[1] not in ENV_HASH_KEYS:
        users = ", ".join(ENV_HASH_KEYS)
        print(f"uso: python -m api.auth make-hash <{users}>", file=sys.stderr)
        return 2
    user = args[1]
    first = getpass.getpass(f"Contrasena para '{user}': ")
    second = getpass.getpass("Repite la contrasena: ")
    if not first or first != second:
        print("las contrasenas no coinciden o estan vacias; nada generado", file=sys.stderr)
        return 1
    print(f"{ENV_HASH_KEYS[user]}='{make_hash(first)}'")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
