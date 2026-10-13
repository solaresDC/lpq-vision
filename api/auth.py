"""The session (Arch 25.3, SPEC 1.3): accounts in the DATABASE, cookie sessions in RAM, the machine
grant, the gafete door, and the phones' pass (Section 3.13.2).

Accounts (Fase 3): the `users` table. Login is the same PBKDF2 as Fase 2 (stdlib only, one import-time
dummy round for names that cannot log in, the check off the event loop, a constant-time compare), now
read from a row. An unknown name, an INACTIVE account and a MACHINE-role row (the reserved `cuarto`,
whose hash IS the machine floor) all pay the dummy round and get the same rejection: the machine
password can never open the front door. The Fase-2 .env hashes were imported ONCE by the boot
bootstrap (brain.db.init); nothing here reads them.

Sessions live in this process's RAM: random ids in a dict, born fresh each boot. A restart logs
everyone out, harmlessly; there is NO signing secret anywhere. A session expires after
machine.session_hours (read per request, no sliding renewal), and deactivating an account, resetting
its password or changing its role drops that account's live sessions at once (drop_sessions_of).
This depends on the api being ONE uvicorn process, which it is and must remain.

The machine floor: once the hub has checked the `cuarto` password (check_machine_password), THIS
session holds a grant for MACHINE_GRANT_MIN minutes, in RAM, keyed by its session id; logout drops it.

The gafete, the second wall inside the club: every site owns a device key (sites.device_key), and since
Section 3.13.2 only the MACHINES carry it (the bot reads it from the database; a Pi will carry it), always
in the X-Device-Key header: never in a URL, never in a cookie. guard_session_or_key and guard_known are
the read checks of SPEC 1.3's enforcement matrix (api/routes.py and two Fase-1 endpoints in api/main.py).

The phone's pass (Section 3.13.2): a phone accepted on a camera carries only its own pass, a random token
in the long-lived httponly cookie lpq_pase; the database keeps only its sha256 (telefonos.pase).
guard_capture opens the capture lane to THAT site's gafete or to the pass of the phone on THAT camera. A
memo in RAM keeps, for a day, why a phone lost its camera, so its page can say it; a restart forgets it,
harmlessly (the page then just asks for the camera's link).

Nothing auth-related is ever logged beyond the user or site name: no password, no hash, no key, no pass.
The site rule: a site account gets every list endpoint FORCED to its site; admin sees all.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from psycopg.types.json import Jsonb

from brain.db import queries as q
from brain.validator.models import (
    MACHINE_DEFAULTS,
    MACHINE_USER,
    SITE_SLUG_RE,
    LoginRequest,
    OwnPasswordRequest,
    load_config,
)

log = logging.getLogger("lpq.api.auth")

# --- named constants (SPEC section 4) -------------------------------------------------
PBKDF2_ITERATIONS = 600000
HASH_SCHEME = "pbkdf2_sha256"
# The separator of the stored format scheme/iterations/salt/digest is the dollar sign, written as an
# escape so that no copy of this file can ever lose it. The format is byte-identical to Fase 2's.
HASH_SEP = "\x24"
SESSION_COOKIE = "lpq_session"
DEVICE_HEADER = "X-Device-Key"
PASE_COOKIE = "lpq_pase"
PASE_COOKIE_DAYS = 400            # the longest lifetime browsers honor for a cookie
PASE_BYTES = 32                   # random bytes of a phone's pass
FUERA_TTL_S = 24 * 3600           # how long the memo keeps why a phone lost its camera
MACHINE_GRANT_MIN = 15
ROLE_ADMIN = "admin"
ROLE_MANAGER = "manager"
ROLE_MACHINE = "machine"


@dataclass(frozen=True)
class Session:
    sid: str
    user: str
    role: str                 # 'admin' | 'manager' (a machine row never logs in)
    site: str | None          # None = admin: every site
    created: float            # wall-clock seconds; machine.session_hours counts from here
    uid: int | None = None    # the permanent account number (owner ruling 3.9.2)

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN


# The RAM stores, disposable by construction (SPEC 1.5 pattern). One lock: the stores are touched
# from the event loop and from worker threads (sync dependencies, admin actions run off the loop).
_SESSIONS: dict[str, Session] = {}
_GRANTS: dict[str, float] = {}     # session id -> monotonic deadline of its machine grant
_FUERA: dict[str, tuple[str, float]] = {}   # pass sha256 -> (why its phone lost its camera, monotonic deadline)
_LOCK = threading.Lock()


# --- hashing --------------------------------------------------------------------------

def make_hash(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    """scheme, iterations, salt and hex digest joined by HASH_SEP: stdlib only, a fresh 16-byte salt each time."""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("ascii"), iterations)
    return HASH_SEP.join((HASH_SCHEME, str(iterations), salt, digest.hex()))


# The dummy hash a login that cannot succeed is checked against, computed ONCE at import: every login
# costs exactly one PBKDF2 round, so timing never reveals which names exist or can log in.
_DUMMY_HASH = make_hash("")


def verify_hash(password: str, stored: str) -> bool:
    """Constant-time check of a password against a stored hash; any malformed hash is a miss."""
    try:
        scheme, iterations_s, salt, expected = stored.strip().strip("'\"").split(HASH_SEP)
        iterations = int(iterations_s)
    except (ValueError, AttributeError):
        return False
    if scheme != HASH_SCHEME or iterations < 1:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("ascii"), iterations)
    return hmac.compare_digest(digest.hex(), expected)


# --- accounts -------------------------------------------------------------------------

def _account(usuario: str) -> dict[str, Any] | None:
    """The row a PERSON may log in as: known, active and not the machine row. Anything else = None."""
    with q.connect() as conn:
        row = conn.execute(q.SELECT_USER, {"usuario": usuario}).fetchone()
    if row is None or not row["active"] or row["role"] == ROLE_MACHINE:
        return None
    return row


def _check_login(usuario: str, password: str) -> dict[str, Any] | None:
    """Runs in a thread: one DB read and EXACTLY one PBKDF2 round whatever happens (the dummy hash when
    the account cannot log in), then the constant-time compare. Returns the row on success."""
    row = _account(usuario)
    target = row["password_hash"] if row is not None else _DUMMY_HASH
    ok = verify_hash(password, target)
    return row if (ok and row is not None) else None


def check_machine_password(password: str) -> bool:
    """The machine floor's door (the hub calls it off the event loop, for admins only): only the ACTIVE
    row named MACHINE_USER with role machine counts; anything else pays the dummy round and fails."""
    with q.connect() as conn:
        row = conn.execute(q.SELECT_USER, {"usuario": MACHINE_USER}).fetchone()
    valid = row is not None and bool(row["active"]) and row["role"] == ROLE_MACHINE
    ok = verify_hash(password, row["password_hash"] if valid else _DUMMY_HASH)
    return ok and valid


# --- sessions -------------------------------------------------------------------------

def _session_hours() -> int:
    """machine.session_hours, read per request; a config that cannot load never logs everyone out."""
    try:
        return load_config().machine.session_hours
    except Exception:
        return MACHINE_DEFAULTS.session_hours


def _live(sid: str | None) -> Session | None:
    """The session behind this id, or None; an expired one is dropped here (with its grant)."""
    if not sid:
        return None
    with _LOCK:
        session = _SESSIONS.get(sid)
    if session is None:
        return None
    if time.time() - session.created > _session_hours() * 3600:
        with _LOCK:
            _SESSIONS.pop(sid, None)
            _GRANTS.pop(sid, None)
        log.info("session expired user=%s", session.user)
        return None
    return session


def require_session(request: Request) -> Session:
    """FastAPI dependency: the live Session behind the cookie, or 401 (the pages show the login form)."""
    session = _live(request.cookies.get(SESSION_COOKIE))
    if session is None:
        raise HTTPException(status_code=401, detail="sesion requerida")
    return session


def scoped_site(session: Session, requested: str | None) -> str | None:
    """The site a list endpoint may show: a site account is FORCED to its own site (whatever it
    asked for); admin gets what it asked for, or None = all sites."""
    if session.is_admin:
        return requested or None
    return session.site


def drop_sessions_of(usuario: str) -> int:
    """Log an account out everywhere at once (deactivation, password reset, role change). Returns how many."""
    with _LOCK:
        doomed = [sid for sid, s in _SESSIONS.items() if s.user == usuario]
        for sid in doomed:
            _SESSIONS.pop(sid, None)
            _GRANTS.pop(sid, None)
    if doomed:
        log.info("sessions dropped user=%s count=%d", usuario, len(doomed))
    return len(doomed)


# --- the machine grant ----------------------------------------------------------------

def grant_machine(session: Session) -> int:
    """Open the machine floor for THIS session for MACHINE_GRANT_MIN minutes. Returns the seconds granted."""
    seconds = MACHINE_GRANT_MIN * 60
    with _LOCK:
        _GRANTS[session.sid] = time.monotonic() + seconds
    log.info("machine floor granted user=%s minutes=%d", session.user, MACHINE_GRANT_MIN)
    return seconds


def machine_grant_left(session: Session) -> int:
    """Seconds left on this session's machine grant; 0 when it has none (an expired one is dropped)."""
    with _LOCK:
        deadline = _GRANTS.get(session.sid)
    if deadline is None:
        return 0
    left = int(deadline - time.monotonic())
    if left <= 0:
        with _LOCK:
            _GRANTS.pop(session.sid, None)
        return 0
    return left


def revoke_machine(session: Session) -> None:
    with _LOCK:
        _GRANTS.pop(session.sid, None)


# --- the gafete -----------------------------------------------------------------------

def device_key_from(request: Request) -> str | None:
    """The gafete a machine presents, in the X-Device-Key header (the only road since Section 3.13.2)."""
    key = (request.headers.get(DEVICE_HEADER) or "").strip()
    return key or None


def site_key_ok(site: str, key: str | None) -> bool:
    """Constant-time check of a presented gafete against the site's own (an inactive or unknown site has
    none, and a name that cannot be a site never reaches the database). Reads the database: call it off
    the event loop. The key is never logged."""
    if not key or not SITE_SLUG_RE.match(site):
        return False
    with q.connect() as conn:
        row = conn.execute(q.SELECT_DEVICE_KEY, {"site": site}).fetchone()
    stored = (row or {}).get("device_key") or ""
    return hmac.compare_digest(key.encode("utf-8"), stored.encode("utf-8")) and bool(stored)


def site_of_key(key: str | None) -> str | None:
    """The active site whose gafete this is, or None: a constant-time compare against every active
    site's key (a few rows). Reads the database: call it off the event loop. The key is never logged."""
    if not key:
        return None
    with q.connect() as conn:
        rows = conn.execute(q.SELECT_SITE_KEYS).fetchall()
    found = None
    for row in rows:
        stored = row["device_key"] or ""
        if stored and hmac.compare_digest(key.encode("utf-8"), stored.encode("utf-8")):
            found = row["site"]
    return found


async def guard_session_or_key(request: Request, site: str) -> Session | None:
    """The formerly session-free reads (the frame, the state, /api/upload, /api/plates/{id}): a session
    of THIS site (the admin's opens every site) OR that site's gafete. Returns the Session, or None when
    a machine's gafete opened the door. A session of ANOTHER site never shadows a valid gafete.
    Refusals: no credential at all = 401; a gafete that is not this site's = 403; a session of another
    site with no gafete = 403."""
    session = await asyncio.to_thread(_live, request.cookies.get(SESSION_COOKIE))
    if session is not None and (session.is_admin or session.site == site):
        return session
    key = device_key_from(request)
    if key is not None:
        if await asyncio.to_thread(site_key_ok, site, key):
            return None
        log.info("gafete refused site=%s", site)
        raise HTTPException(status_code=403, detail=f"gafete incorrecto para el sitio {site}")
    if session is not None:
        raise HTTPException(status_code=403, detail=f"sitio fuera de tu alcance: {site}")
    raise HTTPException(status_code=401, detail="sesión o gafete requeridos")


async def may_read_site(request: Request, site: str) -> bool:
    """guard_session_or_key as a yes or no, for a one-row read that answers 404 outside its scope."""
    try:
        await guard_session_or_key(request, site)
    except HTTPException:
        return False
    return True


async def guard_known(request: Request) -> None:
    """Any credential this api knows: a live session, or a gafete some active site owns. Else 401 (none
    presented) or 403 (a gafete no active site owns). The door before a one-row read, so a stranger
    never learns which ids exist."""
    if await asyncio.to_thread(_live, request.cookies.get(SESSION_COOKIE)) is not None:
        return
    key = device_key_from(request)
    if key is None:
        raise HTTPException(status_code=401, detail="sesión o gafete requeridos")
    if await asyncio.to_thread(site_of_key, key) is None:
        log.info("gafete not recognized")
        raise HTTPException(status_code=403, detail="gafete de ningún sitio activo")


# --- the phone's pass (Section 3.13.2) -----------------------------------------------

def pase_hash(token: str) -> str:
    """What the database keeps of a pass: its sha256 (the token itself is never stored or logged)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def pase_token(request: Request) -> str | None:
    """The pass this browser presents (the lpq_pase cookie), or None."""
    token = (request.cookies.get(PASE_COOKIE) or "").strip()
    return token or None


def new_pase() -> str:
    return secrets.token_urlsafe(PASE_BYTES)


def set_pase(response: Response, token: str) -> None:
    """Give (or refresh) a phone its pass: httponly, so no script on the page can read it."""
    response.set_cookie(PASE_COOKIE, token, max_age=PASE_COOKIE_DAYS * 86400, httponly=True, samesite="lax", path="/")


def phone_of(pase: str) -> dict[str, Any] | None:
    """The phone row behind a pass's sha256, with whether its sites are active. Reads the database: call it off
    the event loop."""
    with q.connect() as conn:
        return conn.execute(q.SELECT_TELEFONO_BY_PASE, {"pase": pase}).fetchone()


def note_fuera(pase: str, why: str) -> None:
    """Remember for a day why the phone of this pass lost its camera (or its request): its page says it."""
    with _LOCK:
        now = time.monotonic()
        for doomed in [p for p, (_, until) in _FUERA.items() if until < now]:
            _FUERA.pop(doomed, None)
        _FUERA[pase] = (why, now + FUERA_TTL_S)


def fuera_of(pase: str | None) -> str | None:
    if pase is None:
        return None
    with _LOCK:
        entry = _FUERA.get(pase)
    return entry[0] if entry and entry[1] >= time.monotonic() else None


def forget_fuera(pase: str) -> None:
    with _LOCK:
        _FUERA.pop(pase, None)


async def guard_capture(request: Request, site: str, camera: str | None) -> int | None:
    """The capture lane's door (the frame push, upload_burst; Section 3.13.2): THAT site's gafete in the header (a
    machine, answers None) or the pass of the phone accepted on THAT camera of an active site (answers the phone's
    id). A session alone opens nothing here. Refusals: nothing presented 401; a gafete that is not this site's 403;
    a pass whose phone is not on this camera 403, with the reason (the page then asks /api/captura/yo)."""
    key = device_key_from(request)
    if key is not None:
        if await asyncio.to_thread(site_key_ok, site, key):
            return None
        log.info("gafete refused site=%s", site)
        raise HTTPException(status_code=403, detail=f"gafete incorrecto para el sitio {site}")
    token = pase_token(request)
    if token is None:
        raise HTTPException(status_code=401, detail="este teléfono no tiene cámara: abre el enlace de tu cámara")
    phone = await asyncio.to_thread(phone_of, pase_hash(token))
    if phone is None or phone["site"] is None:
        raise HTTPException(status_code=403, detail="este teléfono no tiene cámara: abre el enlace de tu cámara")
    if not phone["site_active"]:
        raise HTTPException(status_code=403, detail=f"el sitio {phone['site']} está desactivado")
    if (phone["site"], phone["camera"]) != (site, camera):
        raise HTTPException(status_code=403, detail=f"este teléfono es la cámara {phone['camera']} de {phone['site']}")
    return int(phone["id"])


# --- the endpoints --------------------------------------------------------------------

router = APIRouter()


def _whoami(session: Session) -> dict[str, Any]:
    return {
        "user": session.user,
        "site": session.site,
        "admin": session.is_admin,
        "role": session.role,
        "uid": session.uid,
        "machine_grant_s": machine_grant_left(session),
    }


@router.post("/api/login")
async def login(body: LoginRequest, response: Response) -> dict:
    user = body.user.strip()
    row = await asyncio.to_thread(_check_login, user, body.password)
    if row is None:
        log.info("login rejected user=%s", user)
        raise HTTPException(status_code=401, detail="usuario o contrasena incorrectos")
    sid = secrets.token_urlsafe(32)
    role = row["role"]
    session = Session(
        sid=sid,
        user=row["usuario"],
        role=role,
        site=None if role == ROLE_ADMIN else row["site"],
        created=time.time(),
        uid=row.get("uid"),
    )
    with _LOCK:
        _SESSIONS[sid] = session
    response.set_cookie(SESSION_COOKIE, sid, httponly=True, samesite="lax", path="/")
    log.info("login ok user=%s role=%s", session.user, session.role)
    return _whoami(session)


@router.post("/api/logout")
async def logout(request: Request, response: Response) -> dict:
    sid = request.cookies.get(SESSION_COOKIE)
    if sid:
        with _LOCK:
            _SESSIONS.pop(sid, None)
            _GRANTS.pop(sid, None)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/api/session")
async def session_info(request: Request) -> dict:
    """Who am I: lets a page restore its locked site chip and its machine grant after a reload; 401 when none."""
    session = await asyncio.to_thread(require_session, request)
    return _whoami(session)


@router.post("/api/account/password")
async def change_own_password(body: OwnPasswordRequest, request: Request, response: Response) -> dict:
    """Mi cuenta: a person changes its OWN password. The current one is checked first (one PBKDF2 round, the
    same constant-time road as login); then every session of the account closes, this one included. Nobody can
    SEE a password: only its hash is stored. Nothing here is logged beyond the user name."""
    session = await asyncio.to_thread(require_session, request)
    if await asyncio.to_thread(_check_login, session.user, body.current) is None:
        log.info("own password change refused user=%s", session.user)
        raise HTTPException(status_code=403, detail="La contraseña actual no es correcta.")
    digest = await asyncio.to_thread(make_hash, body.password)

    def work() -> int | None:
        with q.connect() as conn, conn.transaction():
            done = conn.execute(q.SET_OWN_PASSWORD, {"usuario": session.user, "password_hash": digest}).fetchone()
            if done is None:
                return None
            conn.execute(q.INSERT_ADMIN_LOG_BY, {
                "usuario": session.user,
                "usuario_uid": session.uid,
                "action": "user.password_self",
                "detail": Jsonb({"usuario": session.user, "uid": done["uid"]}),
            })
            return int(done["uid"])

    if await asyncio.to_thread(work) is None:
        raise HTTPException(status_code=409, detail="Esta cuenta no puede cambiar su contraseña desde aquí.")
    dropped = drop_sessions_of(session.user)
    response.delete_cookie(SESSION_COOKIE, path="/")
    log.info("own password changed user=%s", session.user)
    return {"ok": True, "sesiones_cerradas": dropped}
