"""LPQ_VISION api/admin.py: the ADMIN hub (SPEC 1.3, 1.5, 1.6; sizing ruling v3.8).

Every /api/admin/* endpoint lives in a FAMILY module beside this one (admin_knobs, admin_voice,
admin_sites, admin_menu, admin_ops, admin_machine). The hub owns only what they share:

- the floors: require_mostrador (any account: an admin, or a manager forced to its own site),
  require_admin, and require_machine (role admin + a live machine grant on THIS session);
  write_scope() keeps a manager's writes on its own site;
- the machine floor's door: unlock (the reserved cuarto password), lock, and the cuarto's first
  birth (floor 1 may only BIRTH it; changing it needs a grant or set_password over ssh);
- the double confirm of every dangerous action: step 1 (in the family) calls propose() and answers
  {pending_id, diff}; step 2 is POST /api/admin/confirm within CONFIRM_TTL_S, from the SAME session,
  and, on the machine floor, with a live grant at confirm time;
- the bitácora (admin_log) and the Telegram sendMessage helper: the api's own voice, so enviar-prueba
  and the pre-reboot notice work even when the bot is the sick service;
- overview and constantes, the first reads of both pages.

mount(app) includes every family of the fixed FAMILIES list; one not built yet is skipped with a clear
log line (the pattern frontend/routes.py uses for unbuilt pages). startup(app) runs each mounted
family's own startup hook, when it has one. Pending confirms live in this process's RAM (the api is ONE
process): a restart forgets them, harmlessly, and the person asks again.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import secrets
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from psycopg.types.json import Jsonb

from api.auth import (
    MACHINE_GRANT_MIN,
    ROLE_MACHINE,
    Session,
    check_machine_password,
    grant_machine,
    machine_grant_left,
    make_hash,
    require_session,
    revoke_machine,
)
from brain.db import queries as q
from brain.validator import knobs
from brain.validator.models import (
    MACHINE_USER,
    PASSWORD_MIN_CHARS,
    RAW_LOG_AUTO_OFF_HOURS,
    WORKER_SCALE_MAX,
    ConfirmRequest,
    PasswordRequest,
    UnlockRequest,
    load_config,
)
from brain.validator.textos import REBOOT_NOTICE_DELAY

log = logging.getLogger("lpq.api.admin")

# --- named constants (SPEC section 4) -------------------------------------------------
CONFIRM_TTL_S = 60
FAMILIES = ("admin_knobs", "admin_voice", "admin_sites", "admin_menu", "admin_ops", "admin_machine")
FLOOR_MOSTRADOR = "mostrador"
FLOOR_MAQUINAS = "maquinas"
TELEGRAM_API = "https://api.telegram.org"
TELEGRAM_TIMEOUT_S = 10
ENV_BOT_TOKEN = "TELEGRAM_BOT_TOKEN"
ENV_CHAT_ID = "TELEGRAM_CHAT_ID"   # temporal: only the last-resort chat, until the ceremony deletes it

# The read-only constants the constantes view shows beside the registry.
CONSTANTS: dict[str, int] = {
    "MACHINE_GRANT_MIN": MACHINE_GRANT_MIN,
    "CONFIRM_TTL_S": CONFIRM_TTL_S,
    "WORKER_SCALE_MAX": WORKER_SCALE_MAX,
    "PASSWORD_MIN_CHARS": PASSWORD_MIN_CHARS,
    "RAW_LOG_AUTO_OFF_HOURS": RAW_LOG_AUTO_OFF_HOURS,
    "REBOOT_NOTICE_DELAY_S": int(REBOOT_NOTICE_DELAY.total_seconds()),
}

router = APIRouter()
MOUNTED: list[str] = []            # the families mounted at boot, in FAMILIES order
_MODULES: list[ModuleType] = []


# --- the floors -----------------------------------------------------------------------

def require_mostrador(session: Session = Depends(require_session)) -> Session:
    """Floor 1: every account that can log in (an admin, or a manager forced to its own site)."""
    return session


def require_admin(session: Session = Depends(require_session)) -> Session:
    if not session.is_admin:
        raise HTTPException(status_code=403, detail="solo el admin puede hacer esto")
    return session


def require_machine(session: Session = Depends(require_admin)) -> Session:
    """Floor 2: role admin AND a live machine grant on this session."""
    if machine_grant_left(session) <= 0:
        raise HTTPException(status_code=403, detail="cuarto de máquinas cerrado: ábrelo con su contraseña")
    return session


def write_scope(session: Session, site: str | None) -> str | None:
    """The site a WRITE may touch. A manager: only its own (another site is 403; none means its own).
    The admin: the site it names, or None = the global value."""
    if session.is_admin:
        return site or None
    if site and site != session.site:
        raise HTTPException(status_code=403, detail=f"sitio fuera de tu alcance: {site}")
    return session.site


# --- the double confirm ---------------------------------------------------------------

@dataclass
class Pending:
    sid: str
    floor: str
    action: str
    diff: dict[str, Any]
    run: Callable[[], Awaitable[Any]]
    deadline: float                 # monotonic


_PENDING: dict[str, Pending] = {}


def _sweep_pending() -> None:
    """Forget confirms long dead (one extra TTL, so a late click still hears 'venció', not 'no existe')."""
    limit = time.monotonic() - CONFIRM_TTL_S
    for pid in [p for p, item in _PENDING.items() if item.deadline < limit]:
        _PENDING.pop(pid, None)


def propose(
    session: Session,
    *,
    action: str,
    diff: dict[str, Any],
    run: Callable[[], Awaitable[Any]],
    floor: str = FLOOR_MOSTRADOR,
) -> dict[str, Any]:
    """Step 1 of a dangerous action (call it from the event loop): park `run` behind a pending id and
    answer what the page shows in its confirm dialog. Nothing happens until step 2."""
    if floor not in (FLOOR_MOSTRADOR, FLOOR_MAQUINAS):
        raise ValueError(f"unknown floor {floor!r}")
    _sweep_pending()
    pid = secrets.token_urlsafe(18)
    _PENDING[pid] = Pending(session.sid, floor, action, diff, run, time.monotonic() + CONFIRM_TTL_S)
    log.info("confirm pending action=%s user=%s", action, session.user)
    return {"pending_id": pid, "action": action, "diff": diff, "expires_s": CONFIRM_TTL_S}


@router.post("/api/admin/confirm")
async def confirm(body: ConfirmRequest, session: Session = Depends(require_mostrador)) -> dict[str, Any]:
    """Step 2: the same session, inside the TTL; the machine floor re-checks its grant NOW."""
    pending = _PENDING.get(body.pending_id)
    if pending is None or pending.sid != session.sid:
        raise HTTPException(status_code=404, detail="no hay nada pendiente con ese número (o ya se usó): vuelve a pedirlo")
    if time.monotonic() > pending.deadline:
        _PENDING.pop(body.pending_id, None)
        raise HTTPException(status_code=410, detail="la confirmación venció: vuelve a pedirla")
    if pending.floor == FLOOR_MAQUINAS and (not session.is_admin or machine_grant_left(session) <= 0):
        raise HTTPException(status_code=403, detail="cuarto de máquinas cerrado: ábrelo y confirma otra vez")
    _PENDING.pop(body.pending_id, None)
    result = await pending.run()
    log.info("confirm done action=%s user=%s", pending.action, session.user)
    return {"ok": True, "action": pending.action, "result": result}


# --- the bitácora ---------------------------------------------------------------------

def log_action(usuario: str | None, action: str, detail: dict[str, Any] | None = None) -> int:
    """One admin_log row (usuario None = the system acted). Sync: from async code use alog()."""
    with q.connect() as conn:
        row = conn.execute(
            q.INSERT_ADMIN_LOG, {"usuario": usuario, "action": action, "detail": Jsonb(detail or {})}
        ).fetchone()
    return int(row["id"])


async def alog(session: Session | None, action: str, detail: dict[str, Any] | None = None) -> int:
    return await asyncio.to_thread(log_action, session.user if session else None, action, detail)


# --- the api's own voice --------------------------------------------------------------

def send_telegram(chat_id: str, text: str) -> tuple[bool, str]:
    """ONE sendMessage through the Bot API. Sync: call it off the event loop. Returns (ok, detail);
    detail is Telegram's own description on a refusal. The token rides only inside the request URL and
    is never logged; failures are reported by their type, never by their text."""
    token = (os.environ.get(ENV_BOT_TOKEN) or "").strip()
    if not token:
        return False, "falta el token del bot en el servidor"
    request = urllib.request.Request(
        f"{TELEGRAM_API}/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TELEGRAM_TIMEOUT_S) as response:
            payload = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read() or b"{}")
        except ValueError:
            payload = {}
        return False, str(payload.get("description") or f"Telegram respondió HTTP {exc.code}")
    except Exception as exc:
        return False, f"sin conexión con Telegram ({type(exc).__name__})"
    if payload.get("ok"):
        return True, "enviado"
    return False, str(payload.get("description") or "Telegram no aceptó el mensaje")


def destinations_input() -> dict[str, Any]:
    """What brain.validator.textos.destinations needs, read fresh: the active sites' chats (database),
    admin_chat_id and the sites in mantenimiento (config), and the temporal env chat while it exists.
    Sync: call it off the event loop. Chat ids are never logged."""
    cfg = load_config()
    with q.connect() as conn:
        rows = conn.execute(q.SELECT_SITE_CHATS).fetchall()
    return {
        "site_chats": {r["site"]: str(r["telegram_chat_id"]) for r in rows},
        "admin_chat": cfg.admin_chat_id,
        "env_chat": (os.environ.get(ENV_CHAT_ID) or "").strip() or None,
        "muted": frozenset(s for s, sc in cfg.sites.items() if sc.mantenimiento),
    }


# --- the machine floor's door ---------------------------------------------------------

@router.post("/api/admin/machine/unlock")
async def machine_unlock(body: UnlockRequest, session: Session = Depends(require_admin)) -> dict[str, int]:
    if not await asyncio.to_thread(check_machine_password, body.password):
        log.info("machine unlock refused user=%s", session.user)
        raise HTTPException(status_code=403, detail="contraseña del cuarto de máquinas incorrecta")
    seconds = grant_machine(session)
    await alog(session, "machine.unlock", {"minutes": seconds // 60})
    return {"machine_grant_s": seconds}


@router.post("/api/admin/machine/lock")
async def machine_lock(session: Session = Depends(require_admin)) -> dict[str, int]:
    revoke_machine(session)
    return {"machine_grant_s": 0}


@router.post("/api/admin/machine/birth")
async def machine_birth(body: PasswordRequest, session: Session = Depends(require_admin)) -> dict[str, str]:
    """Floor 1 may only BIRTH the cuarto password while none exists; changing it needs a live grant
    (the machine room) or set_password over ssh."""
    digest = await asyncio.to_thread(make_hash, body.password)

    def work() -> Any:
        with q.connect() as conn:
            return conn.execute(q.BIRTH_MACHINE_USER, {"usuario": MACHINE_USER, "password_hash": digest}).fetchone()

    if await asyncio.to_thread(work) is None:
        raise HTTPException(
            status_code=409,
            detail="la contraseña del cuarto ya existe: se cambia desde el cuarto (con permiso) o con set_password por ssh",
        )
    await alog(session, "machine.birth", {})
    return {"cuarto": "creado"}


# --- the pages' first reads -----------------------------------------------------------

@router.get("/api/admin/overview")
async def overview(session: Session = Depends(require_mostrador)) -> dict[str, Any]:
    """Who is here, which families exist, and the sites this account may see."""

    def work() -> tuple[Any, list[dict[str, Any]], dict[str, Any] | None]:
        cfg = load_config()
        with q.connect() as conn:
            sites = conn.execute(q.SELECT_SITES_ALL).fetchall()
            cuarto = conn.execute(q.SELECT_USER, {"usuario": MACHINE_USER}).fetchone()
        return cfg, sites, cuarto

    cfg, sites, cuarto = await asyncio.to_thread(work)
    visible: list[dict[str, Any]] = []
    for row in sites:
        if not session.is_admin and row["site"] != session.site:
            continue
        site_cfg = cfg.sites.get(row["site"])
        visible.append(
            {
                "site": row["site"],
                "ingestion": row["ingestion"],
                "active": bool(row["active"]),
                "chat_set": row["telegram_chat_id"] is not None,
                "in_config": site_cfg is not None,
                "mantenimiento": bool(site_cfg is not None and site_cfg.mantenimiento),
                "cameras": {}
                if site_cfg is None
                else {
                    name: {"source": cam.source, "role": cam.role, "kitchen_edge": cam.kitchen_edge}
                    for name, cam in site_cfg.cameras.items()
                },
            }
        )
    out: dict[str, Any] = {
        "user": session.user,
        "role": session.role,
        "admin": session.is_admin,
        "site": session.site,
        "machine_grant_s": machine_grant_left(session),
        "families": list(MOUNTED),
        "sites": visible,
    }
    if session.is_admin:
        out["cuarto_exists"] = cuarto is not None and cuarto["role"] == ROLE_MACHINE
    return out


@router.get("/api/admin/constantes")
async def constantes(session: Session = Depends(require_mostrador)) -> dict[str, Any]:
    """Read-only: the registry with its helps and values, the machine block, the named constants.
    A manager sees the first floor's knobs painted for its own site, never the machine block."""
    cfg = await asyncio.to_thread(load_config)
    if session.is_admin:
        described = await asyncio.to_thread(lambda: knobs.describe(cfg))
        return {"knobs": described, "machine": cfg.machine.model_dump(), "constants": CONSTANTS}
    described = await asyncio.to_thread(lambda: knobs.describe(cfg, site=session.site, floor=FLOOR_MOSTRADOR))
    return {"knobs": described, "constants": CONSTANTS}


# --- the families ---------------------------------------------------------------------

def mount(app: FastAPI) -> None:
    """Include the hub and every BUILT family, in FAMILIES order; an unbuilt one is skipped loudly."""
    app.include_router(router)
    for name in FAMILIES:
        full = f"api.{name}"
        try:
            module = importlib.import_module(full)
        except ModuleNotFoundError as exc:
            if exc.name != full:
                raise                    # a built family with a broken import must fail loudly
            log.info("admin: familia %s aún no construida: se omite", name)
            continue
        app.include_router(module.router)
        MOUNTED.append(name)
        _MODULES.append(module)
        log.info("admin: familia %s montada", name)


async def startup(app: FastAPI) -> None:
    """Run each mounted family's own startup hook (e.g. the boot reconvergence), in FAMILIES order."""
    for module in _MODULES:
        hook = getattr(module, "startup", None)
        if hook is not None:
            await hook(app)
