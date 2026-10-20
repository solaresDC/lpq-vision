"""LPQ_VISION api/admin_sites.py: sites and accounts in the mostrador (SPEC 1.3, 1.5; a family of the admin hub).

GET  /api/admin/sites?site=                          the visible sites: DB row + config block + the auditor, and per
                                                     camera its light, its phone, the phones waiting and its link
POST /api/admin/sites                                Crear restaurante (admin): ALL OR NOTHING across DB and config
POST /api/admin/sites/{site}/edit                    active / ingestion (admin; deactivating asks for the double confirm)
POST /api/admin/sites/{site}/funciones               the site's two function switches
POST /api/admin/sites/{site}/cameras                 add one camera, or change its role (admin)
POST /api/admin/sites/{site}/cameras/{camera}/delete remove one camera with its link, its phone and its requests
                                                     (admin; double confirm; never the last one)
POST /api/admin/sites/{site}/cameras/{camera}/source Relevo a Pi: a phone camera goes back to its Pi; its phone leaves
POST /api/admin/sites/{site}/cameras/{camera}/enlace Dame el enlace (Ver enlace when live): the live link, a new one
                                                     when none or expired
POST /api/admin/sites/{site}/cameras/{camera}/enlace/nuevo    Cambiar enlace (inside the link window since 3.17.5): a new
                                                     code (only the admin picks its life)
POST /api/admin/sites/{site}/cameras/{camera}/enlace/duracion the same code, its last day counted again from today
                                                     (admin; never on a link that already expired)
POST /api/admin/sites/{site}/cameras/{camera}/telefonos/{id}/aceptar  the phone becomes that camera; on a Pi
                                                     camera, the relevo (admin; never on a deactivated site)
POST /api/admin/sites/{site}/cameras/{camera}/telefonos/{id}/rechazar its request for that camera goes (admin)
POST /api/admin/sites/{site}/cameras/{camera}/telefonos/{id}/sacar    the phone leaves that camera
POST /api/admin/sites/{site}/gafete/reveal           show the site's gafete, the machines' key (admin)
POST /api/admin/sites/{site}/gafete/regenerate       a new gafete for the machines (admin, double confirm)
GET  /api/admin/users                                the accounts and the "Eliminadas" (admin; never the machine row)
POST /api/admin/users                                create an account (admin)
POST /api/admin/users/{usuario}/password|active|role new password, activate or deactivate, role and site (admin)
POST /api/admin/users/{usuario}/delete               delete: typed name + double confirm; recoverable (admin)
POST /api/admin/users/{usuario}/recover              Recuperar a deleted account (admin)

A manager sees ONLY its own site and edits its functions; of its cameras it gets the light, the mirilla, the
links, Sacar and Relevo a Pi (Section 3.13.2: cameras are added, removed and re-roled by the admin, and Aceptar
and Rechazar are the admin's); the gafete, the active switch, Crear restaurante and the accounts are the
admin's. Every write leaves a bitácora row carrying the actor's name AND permanent account number (users.uid,
never reused). A write that spans the database and config.yaml is all or nothing (_db_and_config). Accounts
(owner ruling 3.9.2, amending SPEC §3): deleting one asks for its exact name, then the double confirm; it closes
at once, stays recoverable for USER_RECOVERY_DAYS with its name reserved, and the worker's sweep erases it after
warning the admin chat. Never yourself, never the last active admin, never the machine row; a deleted account
can't be edited until it is recovered; any change to an account drops its open sessions. A gafete is never
logged and never written to the bitácora: it reaches only the admin's screen, on request. A link's code never
reaches a log or the bitácora either, and a phone's pass reaches neither: only its sha256 lives in telefonos.
Section 3.17.1: a link ends at 12:00 of its last day on the site's own clock (owner, 16 Oct), never a count of hours
from the click: 1 día made today ends tomorrow at noon. personalizado lasts 1 to 365 days, or until a chosen day
within a year. Each site's view carries its timezone (zona) and the link answers the site's today (hoy), so the page
shows and picks days on the site's calendar.
Section 3.17.3 (owner, 17 Oct): a link that expired never works again. Its duration can't be changed: «Dame el enlace»
makes a new code instead (until 3.17.3 the same code got a new end, and the old link opened again).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from psycopg import errors as pg_errors
from psycopg.types.json import Jsonb

from api import config_writer as cw
from api import routes as lane
from api.admin import log_action, propose, require_admin, require_mostrador, write_scope
from api.auth import ROLE_MACHINE, Session, drop_sessions_of, forget_fuera, make_hash, note_fuera
from brain.db import queries as q
from brain.db.init import DEVICE_KEY_BYTES
from brain.validator import knobs
from brain.validator.models import (
    DEFAULT_TIMEZONE,
    DURACION_DEFAULT,
    DURACIONES,
    ENLACE_ALFABETO,
    ENLACE_DIAS_MAX,
    ENLACE_LARGO,
    ENLACE_VENCE_HORA,
    USER_RECOVERY_DAYS,
    CameraConfig,
    CameraUpsertRequest,
    Config,
    ConfirmNameRequest,
    EnlaceDuracionRequest,
    EnlaceNuevoRequest,
    Funciones,
    PasswordRequest,
    SiteConfig,
    SiteCreateRequest,
    SiteEditRequest,
    SourceRequest,
    SwitchRequest,
    UserCreateRequest,
    UserRoleRequest,
    load_config,
)

log = logging.getLogger("lpq.api.admin_sites")

router = APIRouter()

SOURCE_WORDS = {"phone": "teléfono", "pi": "Pi"}
ROLE_WORDS = {"return": "regreso", "outgoing": "salida", "both": "ambas"}
MESES = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")
CODIGO_INTENTOS = 3                # a new link's code collides with another (1 in 10^11): try a fresh one
VENCIDO_SIN_DURACION = ("Ese enlace ya venció y no vuelve a servir: cierra esta ventana y pulsa «Dame el enlace» "
                        "para uno nuevo.")        # Section 3.17.3: an expired link gets no new end


# --- helpers ------------------------------------------------------------------------------

async def _config() -> Config:
    return await asyncio.to_thread(load_config)


def _site_cfg(cfg: Config, site: str) -> SiteConfig:
    site_cfg = cfg.sites.get(site)
    if site_cfg is None:
        raise HTTPException(status_code=404, detail=f"sitio desconocido: {site}")
    return site_cfg


def _read(fn: Callable[[psycopg.Connection], Any]) -> Any:
    with q.connect() as conn:
        return fn(conn)


def _tx(fn: Callable[[psycopg.Connection], Any]) -> Any:
    with q.connect() as conn, conn.transaction():
        return fn(conn)


def _bitacora(conn: psycopg.Connection, actor: Session | None, action: str, detail: dict[str, Any]) -> None:
    """A bitácora row INSIDE the caller's transaction (it lands only if the action lands), with the actor's
    name and permanent account number."""
    conn.execute(q.INSERT_ADMIN_LOG_BY, {
        "usuario": actor.user if actor else None,
        "usuario_uid": actor.uid if actor else None,
        "action": action,
        "detail": Jsonb(detail),
    })


async def _write(ops: list[cw.Op]) -> None:
    try:
        await cw.write(ops)
    except cw.WriterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


def _db_and_config_sync(db_work: Callable[[psycopg.Connection], Any], ops: list[cw.Op],
                        check: Callable[[Config], None] | None = None) -> Any:
    written: cw.WriteResult | None = None
    try:
        if check is not None:
            check(load_config())                   # the config the ops were built on, read again under the lock
        with q.connect() as conn, conn.transaction():
            value = db_work(conn)
            written = cw.apply_ops(ops)
        return value
    except Exception:
        if written is not None and written.written:
            cw.restore(written.previous_text)      # the database refused at commit: config goes back too
        raise


async def _db_and_config(db_work: Callable[[psycopg.Connection], Any], ops: list[cw.Op],
                         check: Callable[[Config], None] | None = None) -> Any:
    """All or nothing across the database and config.yaml, under the one writer's lock. check(config), when given,
    runs first under the same lock and raises when the config changed since the ops were built from it."""
    async with cw.LOCK:
        try:
            return await asyncio.to_thread(_db_and_config_sync, db_work, ops, check)
        except cw.WriterError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except pg_errors.UniqueViolation:
            raise HTTPException(status_code=409, detail="Ya existe un sitio o una cuenta con ese nombre.") from None


def _erase_at(deleted_at: datetime) -> datetime:
    return deleted_at + timedelta(days=USER_RECOVERY_DAYS)


def _human_date(moment: datetime) -> str:
    local = moment.astimezone(ZoneInfo(DEFAULT_TIMEZONE))
    return f"{local.day} {MESES[local.month - 1]} {local.year}"


# --- the auditor and the site view -----------------------------------------------------------

def audit(site_cfg: SiteConfig) -> list[str]:
    """Do the cameras' roles add up to the switched-on functions, and nothing more? Warnings only."""
    f = site_cfg.funciones
    cams = site_cfg.cameras
    notes: list[str] = []
    if not cams:
        notes.append("El sitio no tiene cámaras.")
    if not f.merma and not f.presentacion:
        notes.append("Las dos funciones están apagadas: el sitio no analiza nada.")
    if f.merma and not any(c.role in ("return", "both") for c in cams.values()):
        notes.append("La merma está prendida pero ninguna cámara ve platos que regresan (rol regreso o ambas).")
    if f.presentacion and not any(c.role in ("outgoing", "both") for c in cams.values()):
        notes.append("El emplatado está prendido pero ninguna cámara ve platos que salen (rol salida o ambas).")
    for name, cam in cams.items():
        if cam.role == "return" and not f.merma:
            notes.append(f"La cámara {name} solo ve regresos, pero la merma está apagada: el teléfono descarta sus fotos.")
        if cam.role == "outgoing" and not f.presentacion:
            notes.append(f"La cámara {name} solo ve salidas, pero el emplatado está apagado: el teléfono descarta sus fotos.")
        if cam.role == "both" and cam.kitchen_edge is None:
            notes.append(f"La cámara {name} ve en ambas direcciones pero no está calibrada: falta el borde-cocina (modo alineación en /camara).")
    return notes


def _enlace_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "codigo": row["codigo"],
        "dias": row["dias"],
        "vence": row["vence_at"].isoformat() if row["vence_at"] else None,
        "vencido": bool(row["vencido"]),
    }


def _camera_view(site: str, name: str, cam: CameraConfig, cfg: Config, phones: list[dict[str, Any]],
                 links: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    """One camera as the mostrador shows it (Section 3.13.2): its light, the phone that captures it, the phones
    waiting for the admin (oldest first; viene_de = the camera of this site such a phone leaves when accepted,
    otro_sitio = it leaves a camera of another site, never named) and its link. Only values that change with an
    event: the live re-read compares this JSON."""
    holder = next((p for p in phones if p["site"] == site and p["camera"] == name), None)
    waiting = [p for p in phones if p["pide_site"] == site and p["pide_camera"] == name]
    link = links.get((site, name))
    return {
        "name": name,
        "source": cam.source,
        "role": cam.role,
        "kitchen_edge": cam.kitchen_edge,
        "luz": lane.luz(site, name, cam.source, holder["id"] if holder else None, cfg),
        "telefono": {"id": holder["id"], "nombre": holder["nombre"]} if holder else None,
        "pendientes": [
            {
                "id": p["id"],
                "nombre": p["nombre"],
                "at": p["pide_at"].isoformat(),
                "viene_de": p["camera"] if p["site"] == site else None,
                "otro_sitio": p["site"] is not None and p["site"] != site,
            }
            for p in waiting
        ],
        "enlace": _enlace_view(link) if link else None,
    }


def _site_view(row: dict[str, Any], site_cfg: SiteConfig | None, cfg: Config, phones: list[dict[str, Any]],
               links: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    view: dict[str, Any] = {
        "site": row["site"],
        "active": bool(row["active"]),
        "ingestion": row["ingestion"],
        "chat_set": row["telegram_chat_id"] is not None,
        "in_config": site_cfg is not None,
        "zona": knobs.timezone_of(cfg, row["site"]),     # the site's clock: a link's day and hour are read on it
    }
    if site_cfg is None:
        view.update({"funciones": None, "cameras": [], "auditor": ["Este sitio existe en la base pero falta en config.yaml."]})
    else:
        view.update({
            "funciones": site_cfg.funciones.model_dump(),
            "cameras": [_camera_view(row["site"], name, cam, cfg, phones, links) for name, cam in site_cfg.cameras.items()],
            "auditor": audit(site_cfg),
        })
    return view


def _phones_and_links(conn: psycopg.Connection) -> tuple[list[dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    phones = conn.execute(q.SELECT_TELEFONOS_ALL).fetchall()
    links = {(r["site"], r["camera"]): r for r in conn.execute(q.SELECT_ENLACES_ALL).fetchall()}
    return phones, links


async def _view_of(site: str) -> dict[str, Any]:
    cfg = await _config()

    def work(conn: psycopg.Connection) -> tuple[Any, list[dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
        return (conn.execute(q.SELECT_SITE, {"site": site}).fetchone(), *_phones_and_links(conn))

    row, phones, links = await asyncio.to_thread(_read, work)
    if row is None:
        raise HTTPException(status_code=404, detail=f"sitio desconocido: {site}")
    return _site_view(row, cfg.sites.get(site), cfg, phones, links)


@router.get("/api/admin/sites")
async def list_sites(site: str | None = None, session: Session = Depends(require_mostrador)) -> dict[str, Any]:
    scope = (site or None) if session.is_admin else session.site
    cfg = await _config()

    def work(conn: psycopg.Connection) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
        return (conn.execute(q.SELECT_SITES_ALL).fetchall(), *_phones_and_links(conn))

    rows, phones, links = await asyncio.to_thread(_read, work)
    views = [
        _site_view(r, cfg.sites.get(r["site"]), cfg, phones, links)
        for r in rows if scope is None or r["site"] == scope
    ]
    return {"sites": views}


# --- Crear restaurante (admin): all or nothing ------------------------------------------------

@router.post("/api/admin/sites")
async def create_site(body: SiteCreateRequest, session: Session = Depends(require_admin)) -> dict[str, Any]:
    """Crear restaurante: the site, its cameras, functions, gafete, Telegram chat (owner ruling 3.10.3), menu photos
    and manager are born together in one write across the database and config.yaml, or nothing is. The chat id is
    never logged: the bitácora says only whether one was given."""
    cfg = await _config()
    if body.site in cfg.sites:
        raise HTTPException(status_code=409, detail=f"El sitio {body.site} ya existe en config.yaml.")
    digest = await asyncio.to_thread(make_hash, body.manager_password)
    key = secrets.token_urlsafe(DEVICE_KEY_BYTES)
    block = {
        "ingestion": body.ingestion,
        "funciones": cw.Flow(body.funciones.model_dump()),
        "cameras": {c.name: {"source": c.source, "role": c.role} for c in body.cameras},
    }

    def db_work(conn: psycopg.Connection) -> int:
        if body.clone_menu_from and conn.execute(q.SELECT_SITE, {"site": body.clone_menu_from}).fetchone() is None:
            raise HTTPException(status_code=400, detail=f"No existe el sitio {body.clone_menu_from} para clonar su menú.")
        conn.execute(q.INSERT_SITE_FULL, {"site": body.site, "ingestion": body.ingestion, "device_key": key})
        if body.chat_id:
            conn.execute(q.SET_SITE_CHAT, {"site": body.site, "chat_id": body.chat_id})
        manager_uid = conn.execute(q.INSERT_USER, {
            "usuario": body.manager_usuario, "password_hash": digest, "role": "manager", "site": body.site,
        }).fetchone()["uid"]
        cloned = 0
        if body.clone_menu_from:
            cloned = conn.execute(q.CLONE_SITE_PHOTOS, {"site": body.site, "from_site": body.clone_menu_from}).rowcount
        _bitacora(conn, session, "site.create", {
            "site": body.site, "manager": body.manager_usuario, "manager_uid": manager_uid,
            "cameras": [c.name for c in body.cameras], "funciones": body.funciones.model_dump(),
            "menu_de": body.clone_menu_from, "fotos": cloned, "chat": "asignado" if body.chat_id else "sin chat",
        })
        return cloned

    cloned = await _db_and_config(db_work, [cw.set_op(("sites", body.site), block, flow=False)])
    log.info("site created %s manager=%s photos=%d chat=%s user=%s",
             body.site, body.manager_usuario, cloned, "yes" if body.chat_id else "no", session.user)
    view = await _view_of(body.site)
    return {"site": body.site, "manager": body.manager_usuario, "fotos_clonadas": cloned, "view": view}


# --- the active switch (admin) ---------------------------------------------------------------

@router.post("/api/admin/sites/{site}/edit")
async def edit_site(site: str, body: SiteEditRequest, session: Session = Depends(require_admin)) -> dict[str, Any]:
    row = await asyncio.to_thread(_read, lambda c: c.execute(q.SELECT_SITE, {"site": site}).fetchone())
    if row is None:
        raise HTTPException(status_code=404, detail=f"sitio desconocido: {site}")
    cfg = await _config()

    async def apply() -> dict[str, Any]:
        def db_work(conn: psycopg.Connection) -> None:
            conn.execute(q.EDIT_SITE, {"site": site, "ingestion": body.ingestion, "active": body.active})
            _bitacora(conn, session, "site.edit", {"site": site, "ingestion": body.ingestion, "active": body.active})

        ops = [cw.set_op(("sites", site, "ingestion"), body.ingestion)] if body.ingestion and site in cfg.sites else []
        await _db_and_config(db_work, ops)
        log.info("site edited %s active=%s ingestion=%s user=%s", site, body.active, body.ingestion, session.user)
        return await _view_of(site)

    if body.active is False and row["active"]:
        return propose(
            session,
            action="desactivar el sitio " + site,
            diff={
                "resumen": (
                    f"El sitio {site} deja de recibir fotos: su gafete y los pases de sus teléfonos dejan de abrir. "
                    "Sus datos no se borran y se puede volver a activar (sus teléfonos vuelven solos)."
                ),
                "cambios": [{"que": "activo", "antes": True, "despues": False}],
            },
            run=apply,
        )
    return await apply()


# --- functions (own site for a manager) and cameras (admin) ------------------------------------

@router.post("/api/admin/sites/{site}/funciones")
async def set_funciones(site: str, body: Funciones, session: Session = Depends(require_mostrador)) -> dict[str, Any]:
    target = write_scope(session, site)
    _site_cfg(await _config(), target)
    await _write([cw.set_op(("sites", target, "funciones"), cw.Flow(body.model_dump()))])
    await asyncio.to_thread(log_action, session.user, "site.funciones", {"site": target, **body.model_dump()}, session.uid)
    return await _view_of(target)


def _camera_or_404(site_cfg: SiteConfig, site: str, camera: str) -> CameraConfig:
    cam = site_cfg.cameras.get(camera)
    if cam is None:
        raise HTTPException(status_code=404, detail=f"La cámara {camera} no existe en {site}.")
    return cam


def _release(conn: psycopg.Connection, phone: dict[str, Any], why: str) -> tuple[str, str]:
    """The phone leaves its camera, inside the caller's transaction: its row goes, unless it still waits for another
    camera. Answers (its pass, why) for the memo, noted once the transaction landed."""
    conn.execute(q.UNBIND_TELEFONO if phone["pide_site"] is not None else q.DELETE_TELEFONO, {"id": phone["id"]})
    return phone["pase"], why


def _drop_request(conn: psycopg.Connection, phone: dict[str, Any], why: str) -> tuple[str, str]:
    """The phone's request goes, inside the caller's transaction: its row too, unless it captures a camera."""
    conn.execute(q.CLEAR_TELEFONO_PIDE if phone["site"] is not None else q.DELETE_TELEFONO, {"id": phone["id"]})
    return phone["pase"], why


def _note_all(notes: list[tuple[str, str]]) -> None:
    for pase, why in notes:
        note_fuera(pase, why)


def _active_or_409(conn: psycopg.Connection, site: str) -> None:
    """A deactivated site takes no new phone and no new link (Sacar and Relevo a Pi still work: they only remove)."""
    row = conn.execute(q.SELECT_SITE, {"site": site}).fetchone()
    if row is None or not row["active"]:
        raise HTTPException(status_code=409, detail=f"El sitio {site} está desactivado: actívalo primero.")


def _same_camera(site: str, camera: str, cam: CameraConfig, gone_pi: bool = False) -> Callable[[Config], None]:
    """The check run under the writer's lock: the camera still is what the ops were built from (a relevo or a
    calibration in between would make them wrong). gone_pi: a camera already back on its Pi says so."""
    def check(cfg: Config) -> None:
        site_cfg = cfg.sites.get(site)
        now = site_cfg.cameras.get(camera) if site_cfg is not None else None
        if now is None:
            raise HTTPException(status_code=404, detail=f"La cámara {camera} no existe en {site}.")
        if gone_pi and now.source == "pi":
            raise HTTPException(status_code=409, detail=f"{camera} ya volvió a su Pi.")
        if (now.source, now.kitchen_edge) != (cam.source, cam.kitchen_edge):
            raise HTTPException(status_code=409, detail=f"La cámara {camera} cambió mientras tanto: mira la tarjeta otra vez.")
    return check


@router.post("/api/admin/sites/{site}/cameras")
async def upsert_camera(site: str, body: CameraUpsertRequest, session: Session = Depends(require_admin)) -> dict[str, Any]:
    """Add a camera (nueva: a name the site already has answers 409), or change the role of one (admin only, Section
    3.13.2). Its source changes only by the relevo: Relevo a Pi, or the admin's Aceptar of a phone."""
    site_cfg = _site_cfg(await _config(), site)
    existing = site_cfg.cameras.get(body.name)
    base = ("sites", site, "cameras", body.name)
    if existing is not None and body.nueva:
        raise HTTPException(status_code=409, detail=f"Ya hay una cámara {body.name} en {site}: su rol se cambia en su propia fila.")
    if existing is None:
        ops = [cw.set_op(base, {"source": body.source, "role": body.role}, flow=False)]
        action = "camera.add"
    else:
        if body.source != existing.source:
            raise HTTPException(
                status_code=409,
                detail=f"La fuente de {body.name} cambia con «Relevo a Pi» o aceptando un teléfono en su enlace.",
            )
        ops = [cw.set_op(base + ("role",), body.role)]
        # A calibration only means something for role 'both'.
        if existing.kitchen_edge is not None and body.role != "both":
            ops.append(cw.delete_op(base + ("kitchen_edge",)))
        action = "camera.edit"
    await _write(ops)
    await asyncio.to_thread(log_action, session.user, action, {
        "site": site, "camera": body.name, "source": body.source, "role": body.role,
    }, session.uid)
    return await _view_of(site)


@router.post("/api/admin/sites/{site}/cameras/{camera}/delete")
async def delete_camera(site: str, camera: str, session: Session = Depends(require_admin)) -> dict[str, Any]:
    """Remove a camera (admin only, double confirm, never the last one): its link stops opening, its phone leaves
    and the phones waiting for it hear that it is gone. One write across the database and config.yaml."""
    site_cfg = _site_cfg(await _config(), site)
    cam = _camera_or_404(site_cfg, site, camera)
    if len(site_cfg.cameras) == 1:
        raise HTTPException(status_code=400, detail="Un sitio necesita al menos una cámara: agrega otra antes de quitar esta.")

    async def run() -> dict[str, Any]:
        def still_there(cfg: Config) -> None:
            # Confirmed later, so checked again under the writer's lock: the camera still exists and is not the last.
            cams = cfg.sites[site].cameras if site in cfg.sites else {}
            if camera not in cams:
                raise HTTPException(status_code=404, detail=f"La cámara {camera} ya no existe en {site}.")
            if len(cams) == 1:
                raise HTTPException(status_code=400, detail="Un sitio necesita al menos una cámara: agrega otra antes de quitar esta.")

        def db_work(conn: psycopg.Connection) -> list[tuple[str, str]]:
            notes: list[tuple[str, str]] = []
            gone = f"{camera} ya no existe: se quitó en el mostrador."
            conn.execute(q.LOCK_CAMARA, {"site": site, "camera": camera})
            for phone in conn.execute(q.LOCK_TELEFONOS_DE_CAMARA, {"site": site, "camera": camera}).fetchall():
                if (phone["site"], phone["camera"]) == (site, camera):
                    notes.append(_release(conn, phone, gone))
                else:
                    notes.append(_drop_request(conn, phone, gone))
            conn.execute(q.DELETE_ENLACE, {"site": site, "camera": camera})
            _bitacora(conn, session, "camera.delete", {"site": site, "camera": camera})
            return notes

        _note_all(await _db_and_config(db_work, [cw.delete_op(("sites", site, "cameras", camera))], still_there))
        log.info("camera deleted %s/%s user=%s", site, camera, session.user)
        return await _view_of(site)

    holder = await asyncio.to_thread(
        _read, lambda c: c.execute(q.SELECT_TELEFONO_DE_CAMARA, {"site": site, "camera": camera}).fetchone())
    extra = f" Su teléfono «{holder['nombre']}» sale de la cámara." if holder else ""
    return propose(
        session,
        action="quitar la cámara " + camera + " de " + site,
        diff={
            "resumen": (
                f"La cámara {camera} deja de existir en {site}; sus fotos ya guardadas no se borran y su enlace deja "
                "de servir." + extra
            ),
            "cambios": [{
                "que": camera,
                "antes": SOURCE_WORDS.get(cam.source, cam.source) + ", " + ROLE_WORDS.get(cam.role, cam.role),
                "despues": "(sin cámara)",
            }],
        },
        run=run,
    )


@router.post("/api/admin/sites/{site}/cameras/{camera}/source")
async def camera_source(site: str, camera: str, body: SourceRequest, session: Session = Depends(require_mostrador)) -> dict[str, Any]:
    """Relevo a Pi (the admin, or the site's manager): a phone camera goes back to its Pi, its calibration goes with
    the phone, and the phone leaves the camera (its page says it stopped). One write across the database and
    config.yaml, with its bitácora row; the bot announces the relevo from that row. Toward the phone there is no
    switch: a Pi camera becomes a phone's when the admin accepts a phone in its link."""
    target = write_scope(session, site)
    if body.source != "pi":
        raise HTTPException(
            status_code=400,
            detail="Una cámara pasa a teléfono cuando el admin acepta un teléfono en su enlace («Dame el enlace» o «Ver enlace»).",
        )
    cam = _camera_or_404(_site_cfg(await _config(), target), target, camera)
    if cam.source == "pi":
        return {"site": target, "camera": camera, "source": "pi", "changed": False}
    base = ("sites", target, "cameras", camera)
    ops = [cw.set_op(base + ("source",), "pi")]
    if cam.kitchen_edge is not None:
        ops.append(cw.delete_op(base + ("kitchen_edge",)))

    def db_work(conn: psycopg.Connection) -> list[tuple[str, str]]:
        notes: list[tuple[str, str]] = []
        detail: dict[str, Any] = {
            "site": target, "camera": camera, "source": "pi", "road": "mostrador",
            "calibracion_borrada": cam.kitchen_edge is not None,
        }
        conn.execute(q.LOCK_CAMARA, {"site": target, "camera": camera})
        for phone in conn.execute(q.LOCK_TELEFONOS_DE_CAMARA, {"site": target, "camera": camera}).fetchall():
            if (phone["site"], phone["camera"]) == (target, camera):
                detail["telefono"] = phone["nombre"]
                notes.append(_release(conn, phone, f"{camera} volvió a su Pi: este teléfono dejó de capturarla."))
        _bitacora(conn, session, "relevo", detail)
        return notes

    _note_all(await _db_and_config(db_work, ops, _same_camera(target, camera, cam, gone_pi=True)))
    log.info("relevo %s/%s -> pi road=mostrador user=%s", target, camera, session.user)
    return {"site": target, "camera": camera, "source": "pi", "changed": True}


# --- the camera links (Section 3.13.2): the admin and the site's manager ------------------------

def _nuevo_codigo() -> str:
    return "".join(secrets.choice(ENLACE_ALFABETO) for _ in range(ENLACE_LARGO))


def _hoy(cfg: Config, site: str) -> tuple[ZoneInfo, date]:
    """The site's clock and its today. A link endpoint reads it ONCE and passes it on, so a request that straddles
    midnight counts every day from the same today."""
    zone = ZoneInfo(knobs.timezone_of(cfg, site))
    return zone, datetime.now(timezone.utc).astimezone(zone).date()


def _vence(zone: ZoneInfo, today: date, dias: int | None) -> datetime | None:
    """The end of a link that lasts `dias` days (owner, 16 Oct): 12:00 (ENLACE_VENCE_HORA) of the day `dias` days after
    the site's today, on the site's own clock, never a count of hours from the click. None = permanente."""
    if dias is None:
        return None
    return datetime.combine(today + timedelta(days=dias), time(ENLACE_VENCE_HORA), tzinfo=zone)


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def _fecha_text(day: date) -> str:
    return f"{day.day:02d}/{day.month:02d}/{day.year}"


def _dias_pedidos(body: EnlaceNuevoRequest | EnlaceDuracionRequest, today: date) -> int | None:
    """The days a chosen duración means: a fixed choice's own (None = permanente), personalizado's days (1 to
    ENLACE_DIAS_MAX), or the days from the site's today to the last day it chose (tomorrow to a year from today)."""
    if body.duracion != "personalizado":
        if body.dias is not None or body.fecha is not None:
            raise HTTPException(status_code=400, detail="Los días o la fecha van solo con «personalizado».")
        return DURACIONES[body.duracion]
    if (body.dias is None) == (body.fecha is None):
        raise HTTPException(status_code=400, detail="«personalizado» lleva los días o una fecha: uno de los dos.")
    if body.dias is not None:
        if not 1 <= body.dias <= ENLACE_DIAS_MAX:
            raise HTTPException(status_code=400, detail=f"Los días van de 1 a {ENLACE_DIAS_MAX}: escribe solo el número.")
        return body.dias
    dias = (body.fecha - today).days
    if not 1 <= dias <= ENLACE_DIAS_MAX:
        first, last = today + timedelta(days=1), today + timedelta(days=ENLACE_DIAS_MAX)
        raise HTTPException(status_code=400, detail="La fecha va de mañana (" + _fecha_text(first) + ") a un año desde hoy ("
                            + _fecha_text(last) + "), en la hora del sitio.")
    return dias


def _vida(zone: ZoneInfo, today: date, dias: int | None) -> dict[str, Any]:
    """What a link write stores and its bitácora row says: its days and its end (on the site's clock)."""
    return {"dias": dias, "vence": _vence(zone, today, dias)}


def _link_answer(site: str, camera: str, nuevo: bool, row: dict[str, Any], zone: ZoneInfo, today: date) -> dict[str, Any]:
    """A link endpoint's answer: the link, the site's clock and its today (the page picks days on that calendar)."""
    return {"site": site, "camera": camera, "nuevo": nuevo, "enlace": _enlace_view(row), "zona": zone.key,
            "hoy": today.isoformat()}


def _link_camera_now(conn: psycopg.Connection, site: str, camera: str) -> None:
    """A link write holds the camera's lock (the one Quitar la cámara takes) and looks at the camera again under it:
    a camera removed meanwhile answers 404, so no link outlives its camera."""
    _active_or_409(conn, site)
    conn.execute(q.LOCK_CAMARA, {"site": site, "camera": camera})
    _camera_or_404(_site_cfg(load_config(), site), site, camera)


async def _link_tx(work: Callable[[psycopg.Connection, str], Any]) -> Any:
    """Run a link write with a fresh code, again with another one if the code collides (CODIGO_INTENTOS)."""
    for attempt in range(CODIGO_INTENTOS):
        try:
            return await asyncio.to_thread(_tx, lambda conn: work(conn, _nuevo_codigo()))
        except pg_errors.UniqueViolation:
            if attempt == CODIGO_INTENTOS - 1:
                raise HTTPException(status_code=409, detail="No se pudo crear un código nuevo: vuelve a intentarlo.") from None
    return None


@router.post("/api/admin/sites/{site}/cameras/{camera}/enlace")
async def dame_enlace(site: str, camera: str, session: Session = Depends(require_mostrador)) -> dict[str, Any]:
    """Dame el enlace: the camera's live link; when it has none or it expired, a new code (an expired link keeps its
    duration; a first link lasts DURACION_DEFAULT), ending at noon of its last day. Its bitácora row only when a code
    is born (never the code)."""
    target = write_scope(session, site)
    cfg = await _config()
    _camera_or_404(_site_cfg(cfg, target), target, camera)
    zone, today = _hoy(cfg, target)

    def work(conn: psycopg.Connection, codigo: str) -> tuple[dict[str, Any], bool]:
        key = {"site": target, "camera": camera}
        _link_camera_now(conn, target, camera)
        row = conn.execute(q.LOCK_ENLACE, key).fetchone()
        if row is not None and not row["vencido"]:
            return row, False
        vida = _vida(zone, today, row["dias"] if row is not None else DURACIONES[DURACION_DEFAULT])
        if row is None:
            fresh = conn.execute(q.INSERT_ENLACE, {**key, "codigo": codigo, **vida}).fetchone()
            if fresh is None:                        # someone else made it a moment ago: that one is the link
                return conn.execute(q.LOCK_ENLACE, key).fetchone(), False
        else:
            fresh = conn.execute(q.UPSERT_ENLACE, {**key, "codigo": codigo, **vida}).fetchone()
        _bitacora(conn, session, "enlace.crea", {**key, "dias": vida["dias"], "vence": _iso(vida["vence"])})
        return fresh, True

    row, nuevo = await _link_tx(work)
    if nuevo:
        log.info("link created %s/%s user=%s", target, camera, session.user)
    return _link_answer(target, camera, nuevo, row, zone, today)


@router.post("/api/admin/sites/{site}/cameras/{camera}/enlace/nuevo")
async def cambiar_enlace(site: str, camera: str, body: EnlaceNuevoRequest,
                         session: Session = Depends(require_mostrador)) -> dict[str, Any]:
    """Cambiar enlace: a new code now; the old one stops opening at once, the phones already inside stay and the
    requests already made wait as they were. The admin may choose its duration; a manager keeps the current one."""
    target = write_scope(session, site)
    if body.duracion is not None and not session.is_admin:
        raise HTTPException(status_code=403, detail="Solo el admin elige cuánto dura un enlace.")
    if body.duracion is None and (body.dias is not None or body.fecha is not None):
        raise HTTPException(status_code=400, detail="Los días o la fecha van solo con «personalizado».")
    cfg = await _config()
    _camera_or_404(_site_cfg(cfg, target), target, camera)
    zone, today = _hoy(cfg, target)
    chosen = _dias_pedidos(body, today) if body.duracion is not None else None

    def work(conn: psycopg.Connection, codigo: str) -> dict[str, Any]:
        key = {"site": target, "camera": camera}
        _link_camera_now(conn, target, camera)
        row = conn.execute(q.LOCK_ENLACE, key).fetchone()
        if body.duracion is not None:
            dias = chosen
        else:
            dias = row["dias"] if row is not None else DURACIONES[DURACION_DEFAULT]
        vida = _vida(zone, today, dias)
        fresh = conn.execute(q.UPSERT_ENLACE, {**key, "codigo": codigo, **vida}).fetchone()
        _bitacora(conn, session, "enlace.cambia", {**key, "dias": dias, "vence": _iso(vida["vence"])})
        return fresh

    row = await _link_tx(work)
    log.info("link changed %s/%s user=%s", target, camera, session.user)
    return _link_answer(target, camera, True, row, zone, today)


@router.post("/api/admin/sites/{site}/cameras/{camera}/enlace/duracion")
async def duracion_enlace(site: str, camera: str, body: EnlaceDuracionRequest,
                          session: Session = Depends(require_admin)) -> dict[str, Any]:
    """The admin's duration of a camera's link: the same code, its last day counted again from the site's today (saved
    twice the same day, the same end: never a drift of hours). A link that already expired is refused (410), changing
    nothing (Section 3.17.3, owner 17 Oct: an expired link never works again; «Dame el enlace» makes a new code)."""
    cfg = await _config()
    _camera_or_404(_site_cfg(cfg, site), site, camera)
    zone, today = _hoy(cfg, site)
    vida = _vida(zone, today, _dias_pedidos(body, today))

    def work(conn: psycopg.Connection) -> dict[str, Any]:
        _link_camera_now(conn, site, camera)
        key = {"site": site, "camera": camera}
        link = conn.execute(q.LOCK_ENLACE, key).fetchone()
        if link is None:
            raise HTTPException(status_code=404, detail=f"{camera} aún no tiene enlace: pide uno con «Dame el enlace».")
        if link["vencido"]:
            raise HTTPException(status_code=410, detail=VENCIDO_SIN_DURACION)
        row = conn.execute(q.SET_ENLACE_DIAS, {**key, **vida}).fetchone()
        if row is None:                      # it ended between the read and the write: the query keeps it ended
            raise HTTPException(status_code=410, detail=VENCIDO_SIN_DURACION)
        _bitacora(conn, session, "enlace.duracion", {"site": site, "camera": camera, "dias": vida["dias"], "vence": _iso(vida["vence"])})
        return row

    row = await asyncio.to_thread(_tx, work)
    return _link_answer(site, camera, False, row, zone, today)


# --- the phones (Section 3.13.2): Aceptar and Rechazar (admin), Sacar ---------------------------

CHANGED = "Ese pedido cambió mientras tanto: mira la tarjeta otra vez."


@router.post("/api/admin/sites/{site}/cameras/{camera}/telefonos/{phone_id}/aceptar")
async def aceptar_telefono(site: str, camera: str, phone_id: int, session: Session = Depends(require_admin)) -> dict[str, Any]:
    """The phone becomes the camera it asked for, the one whose row was clicked (a request that changed since the
    card was drawn answers 409): the camera it captured until now (if any) is free from this moment, and the other
    phones waiting for this camera hear that it has its phone. On a Pi camera it is also the relevo: the camera
    passes to that phone and its calibration goes with the Pi (road "enlace": the bot announces a teléfono de
    emergencia). One write across the database and config.yaml; never on a deactivated site."""
    cam = _camera_or_404(_site_cfg(await _config(), site), site, camera)
    pre = await asyncio.to_thread(_read, lambda c: c.execute(q.SELECT_TELEFONO, {"id": phone_id}).fetchone())
    if pre is None:
        raise HTTPException(status_code=404, detail="Ese pedido ya no existe (¿lo atendió alguien más?).")
    if (pre["pide_site"], pre["pide_camera"]) != (site, camera):
        raise HTTPException(status_code=409, detail=CHANGED)
    relevo = cam.source != "phone"
    base = ("sites", site, "cameras", camera)
    ops: list[cw.Op] = []
    if relevo:
        ops.append(cw.set_op(base + ("source",), "phone"))
        if cam.kitchen_edge is not None:
            ops.append(cw.delete_op(base + ("kitchen_edge",)))

    def db_work(conn: psycopg.Connection) -> tuple[str, list[tuple[str, str]]]:
        _active_or_409(conn, site)
        conn.execute(q.LOCK_CAMARA, {"site": site, "camera": camera})
        others = conn.execute(q.LOCK_TELEFONOS_DE_CAMARA, {"site": site, "camera": camera}).fetchall()   # by id
        phone = next((r for r in others if r["id"] == phone_id), None)
        if phone is None or (phone["pide_site"], phone["pide_camera"]) != (site, camera):
            raise HTTPException(status_code=409, detail=CHANGED)
        if any((r["site"], r["camera"]) == (site, camera) for r in others):
            raise HTTPException(status_code=409, detail=f"{camera} ya tiene teléfono: sácalo primero.")
        antes = f"{phone['site']}/{phone['camera']}" if phone["site"] is not None else None
        conn.execute(q.BIND_TELEFONO, {"id": phone_id, "site": site, "camera": camera})
        notes = [_drop_request(conn, r, f"{camera} ya tiene teléfono.") for r in others if r["id"] != phone_id]
        _bitacora(conn, session, "telefono.acepta", {
            "site": site, "camera": camera, "nombre": phone["nombre"], "telefono": phone_id, "antes": antes,
        })
        if relevo:
            _bitacora(conn, session, "relevo", {
                "site": site, "camera": camera, "source": "phone", "road": "enlace",
                "calibracion_borrada": cam.kitchen_edge is not None, "telefono": phone["nombre"],
            })
        return phone["pase"], notes

    pase, notes = await _db_and_config(db_work, ops, _same_camera(site, camera, cam))
    forget_fuera(pase)
    _note_all(notes)
    log.info("phone accepted %s/%s telefono=%d relevo=%s user=%s", site, camera, phone_id, relevo, session.user)
    return await _view_of(site)


@router.post("/api/admin/sites/{site}/cameras/{camera}/telefonos/{phone_id}/rechazar")
async def rechazar_telefono(site: str, camera: str, phone_id: int, session: Session = Depends(require_admin)) -> dict[str, Any]:
    """The phone's request for the clicked camera goes (it may ask again by opening the link); a phone that captures
    a camera keeps it. A request that changed since the card was drawn answers 409."""

    def work(conn: psycopg.Connection) -> tuple[str, str]:
        phone = conn.execute(q.LOCK_TELEFONO, {"id": phone_id}).fetchone()
        if phone is None:
            raise HTTPException(status_code=404, detail="Ese pedido ya no existe (¿lo atendió alguien más?).")
        if (phone["pide_site"], phone["pide_camera"]) != (site, camera):
            raise HTTPException(status_code=409, detail=CHANGED)
        _bitacora(conn, session, "telefono.rechaza", {
            "site": site, "camera": camera, "nombre": phone["nombre"], "telefono": phone_id,
        })
        return _drop_request(conn, phone, f"No te aceptaron en {camera}.")

    _note_all([await asyncio.to_thread(_tx, work)])
    log.info("phone refused %s telefono=%d user=%s", site, phone_id, session.user)
    return await _view_of(site)


@router.post("/api/admin/sites/{site}/cameras/{camera}/telefonos/{phone_id}/sacar")
async def sacar_telefono(site: str, camera: str, phone_id: int, session: Session = Depends(require_mostrador)) -> dict[str, Any]:
    """Sacar (the site's manager, or the admin): the phone leaves the clicked camera; to come back it opens the link
    again and waits for the admin's Aceptar. A phone that is no longer on that camera answers 409."""
    target = write_scope(session, site)

    def work(conn: psycopg.Connection) -> tuple[str, str]:
        phone = conn.execute(q.LOCK_TELEFONO, {"id": phone_id}).fetchone()
        if phone is None or phone["site"] != target:
            raise HTTPException(status_code=404, detail=f"Ese teléfono ya no está en ninguna cámara de {target}.")
        if phone["camera"] != camera:
            raise HTTPException(status_code=409, detail=f"Ese teléfono ya no es la cámara {camera}: mira la tarjeta otra vez.")
        _bitacora(conn, session, "telefono.saca", {
            "site": target, "camera": camera, "nombre": phone["nombre"], "telefono": phone_id,
        })
        return _release(conn, phone, f"Te sacaron de {camera}: para volver, abre el enlace de la cámara.")

    _note_all([await asyncio.to_thread(_tx, work)])
    log.info("phone out %s telefono=%d user=%s", target, phone_id, session.user)
    return await _view_of(target)


# --- the gafete (admin) ------------------------------------------------------------------------

@router.post("/api/admin/sites/{site}/gafete/reveal")
async def reveal_gafete(site: str, session: Session = Depends(require_admin)) -> dict[str, Any]:
    row = await asyncio.to_thread(_read, lambda c: c.execute(q.SELECT_DEVICE_KEY, {"site": site}).fetchone())
    if row is None or not row["device_key"]:
        raise HTTPException(status_code=404, detail=f"El sitio {site} no existe, está inactivo o no tiene gafete.")
    await asyncio.to_thread(log_action, session.user, "gafete.reveal", {"site": site}, session.uid)
    log.info("gafete revealed site=%s user=%s", site, session.user)
    return {"site": site, "gafete": row["device_key"]}


@router.post("/api/admin/sites/{site}/gafete/regenerate")
async def regenerate_gafete(site: str, session: Session = Depends(require_admin)) -> dict[str, Any]:
    row = await asyncio.to_thread(_read, lambda c: c.execute(q.SELECT_SITE, {"site": site}).fetchone())
    if row is None:
        raise HTTPException(status_code=404, detail=f"sitio desconocido: {site}")

    async def run() -> dict[str, Any]:
        key = secrets.token_urlsafe(DEVICE_KEY_BYTES)

        def work(conn: psycopg.Connection) -> None:
            conn.execute(q.SET_DEVICE_KEY, {"site": site, "device_key": key})
            _bitacora(conn, session, "gafete.regenerate", {"site": site})

        await asyncio.to_thread(_tx, work)
        log.info("gafete regenerated site=%s user=%s", site, session.user)
        return {"site": site, "gafete": key}

    return propose(
        session,
        action="generar un gafete nuevo para " + site,
        diff={
            "resumen": (
                f"Las máquinas de {site} usan el gafete nuevo: el bot lo lee solo; una Pi deja de entrar hasta que "
                "tenga el nuevo. Los teléfonos no usan gafete: siguen con su pase."
            ),
            "cambios": [{"que": "gafete", "antes": "el actual", "despues": "uno nuevo"}],
        },
        run=run,
    )


# --- accounts (admin) ----------------------------------------------------------------------------

def _person_row(conn: psycopg.Connection, usuario: str, *, allow_deleted: bool = False) -> dict[str, Any]:
    """An account a person uses. The machine row answers 404 exactly like a missing name; a deleted account
    answers 409 (recover it first) unless the caller handles deleted rows itself."""
    row = conn.execute(q.SELECT_USER, {"usuario": usuario}).fetchone()
    if row is None or row["role"] == ROLE_MACHINE:
        raise HTTPException(status_code=404, detail=f"No existe la cuenta {usuario}.")
    if row["deleted_at"] is not None and not allow_deleted:
        raise HTTPException(status_code=409, detail=f"La cuenta {usuario} está eliminada: recupérala primero.")
    return row


def _site_exists(conn: psycopg.Connection, site: str | None) -> None:
    if site and conn.execute(q.SELECT_SITE, {"site": site}).fetchone() is None:
        raise HTTPException(status_code=400, detail=f"sitio desconocido: {site}")


def _last_admin(conn: psycopg.Connection, row: dict[str, Any]) -> bool:
    return row["role"] == "admin" and bool(row["active"]) and int(conn.execute(q.COUNT_ACTIVE_ADMINS).fetchone()["n"]) <= 1


@router.get("/api/admin/users")
async def list_users(session: Session = Depends(require_admin)) -> dict[str, Any]:
    def work(conn: psycopg.Connection) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return conn.execute(q.LIST_USERS).fetchall(), conn.execute(q.LIST_DELETED_USERS).fetchall()

    rows, gone = await asyncio.to_thread(_read, work)
    return {
        "users": [
            {"usuario": r["usuario"], "uid": r["uid"], "role": r["role"], "site": r["site"],
             "active": bool(r["active"]), "created_at": r["created_at"].isoformat(),
             "password_changed_at": r["password_changed_at"].isoformat() if r["password_changed_at"] else None,
             "password_changed_by": r["password_changed_by"]}
            for r in rows
        ],
        "eliminadas": [
            {"usuario": r["usuario"], "uid": r["uid"], "role": r["role"], "site": r["site"],
             "deleted_at": r["deleted_at"].isoformat(), "borra": _erase_at(r["deleted_at"]).isoformat()}
            for r in gone
        ],
        "recovery_days": USER_RECOVERY_DAYS,
    }


@router.post("/api/admin/users")
async def create_user(body: UserCreateRequest, session: Session = Depends(require_admin)) -> dict[str, Any]:
    digest = await asyncio.to_thread(make_hash, body.password)

    def work(conn: psycopg.Connection) -> int:
        _site_exists(conn, body.site)
        uid = conn.execute(q.INSERT_USER, {
            "usuario": body.usuario, "password_hash": digest, "role": body.role, "site": body.site,
        }).fetchone()["uid"]
        _bitacora(conn, session, "user.create", {"usuario": body.usuario, "uid": uid, "role": body.role, "site": body.site})
        return uid

    try:
        uid = await asyncio.to_thread(_tx, work)
    except pg_errors.UniqueViolation:
        row = await asyncio.to_thread(_read, lambda c: c.execute(q.SELECT_USER, {"usuario": body.usuario}).fetchone())
        if row is not None and row["deleted_at"] is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Ese nombre es de una cuenta eliminada: se libera el {_human_date(_erase_at(row['deleted_at']))}.",
            ) from None
        raise HTTPException(status_code=409, detail=f"Ya existe una cuenta {body.usuario}.") from None
    log.info("user created %s role=%s by=%s", body.usuario, body.role, session.user)
    return {"usuario": body.usuario, "uid": uid, "role": body.role, "site": body.site, "active": True}


@router.post("/api/admin/users/{usuario}/password")
async def reset_password(usuario: str, body: PasswordRequest, session: Session = Depends(require_admin)) -> dict[str, Any]:
    """The admin changes an account's password, step 1 of the double confirm. Nobody can SEE a password: the
    admin reads it only while typing it. Step 2 writes the hash, records who and when, and closes its sessions."""
    row = await asyncio.to_thread(_read, lambda c: _person_row(c, usuario))
    digest = await asyncio.to_thread(make_hash, body.password)
    label = f"{usuario} (#{row['uid']})"

    async def run() -> dict[str, Any]:
        def work(conn: psycopg.Connection) -> None:
            again = _person_row(conn, usuario)
            conn.execute(q.RESET_USER_PASSWORD, {"usuario": usuario, "password_hash": digest, "changed_by": session.user})
            _bitacora(conn, session, "user.password", {"usuario": usuario, "uid": again["uid"]})

        await asyncio.to_thread(_tx, work)
        dropped = drop_sessions_of(usuario)
        log.info("user password changed %s by=%s", usuario, session.user)
        return {"usuario": usuario, "sesiones_cerradas": dropped}

    return propose(
        session,
        action="cambiar la contraseña de " + label,
        diff={
            "resumen": (
                f"¿Seguro que quieres cambiar la contraseña de {label}? Sus sesiones abiertas se cerrarán y tendrá "
                "que entrar con la nueva."
            ),
        },
        run=run,
    )


@router.post("/api/admin/users/{usuario}/active")
async def set_active(usuario: str, body: SwitchRequest, session: Session = Depends(require_admin)) -> dict[str, Any]:
    if not body.on and usuario == session.user:
        raise HTTPException(status_code=409, detail="No puedes desactivar tu propia cuenta.")

    def work(conn: psycopg.Connection) -> None:
        row = _person_row(conn, usuario)
        if not body.on and _last_admin(conn, row):
            raise HTTPException(status_code=409, detail=f"{usuario} es el último admin activo: no se puede desactivar.")
        conn.execute(q.SET_USER_ACTIVE, {"usuario": usuario, "active": body.on})
        _bitacora(conn, session, "user.activate" if body.on else "user.deactivate", {"usuario": usuario, "uid": row["uid"]})

    await asyncio.to_thread(_tx, work)
    dropped = 0 if body.on else drop_sessions_of(usuario)
    log.info("user %s %s by=%s", usuario, "activated" if body.on else "deactivated", session.user)
    return {"usuario": usuario, "active": body.on, "sesiones_cerradas": dropped}


@router.post("/api/admin/users/{usuario}/role")
async def set_role(usuario: str, body: UserRoleRequest, session: Session = Depends(require_admin)) -> dict[str, Any]:
    if usuario == session.user and body.role != "admin":
        raise HTTPException(status_code=409, detail="No puedes quitarte el rol de admin a ti mismo.")

    def work(conn: psycopg.Connection) -> None:
        row = _person_row(conn, usuario)
        _site_exists(conn, body.site)
        if body.role != "admin" and _last_admin(conn, row):
            raise HTTPException(status_code=409, detail=f"{usuario} es el último admin activo: no se puede cambiar su rol.")
        conn.execute(q.SET_USER_ROLE, {"usuario": usuario, "role": body.role, "site": body.site})
        _bitacora(conn, session, "user.role", {
            "usuario": usuario,
            "uid": row["uid"],
            "antes": {"role": row["role"], "site": row["site"]},
            "despues": {"role": body.role, "site": body.site},
        })

    await asyncio.to_thread(_tx, work)
    dropped = drop_sessions_of(usuario)
    log.info("user role %s -> %s by=%s", usuario, body.role, session.user)
    return {"usuario": usuario, "role": body.role, "site": body.site, "sesiones_cerradas": dropped}


@router.post("/api/admin/users/{usuario}/delete")
async def delete_user(usuario: str, body: ConfirmNameRequest, session: Session = Depends(require_admin)) -> dict[str, Any]:
    """Delete, step 1: the typed name must match, then the double confirm (its diff says it is permanent and
    names the erase date). Step 2 closes the account at once; it stays recoverable until the erase."""
    if body.name.strip() != usuario:
        raise HTTPException(status_code=400, detail="El nombre escrito no coincide con la cuenta: no se eliminó nada.")
    if usuario == session.user:
        raise HTTPException(status_code=409, detail="No puedes eliminar tu propia cuenta.")

    def check(conn: psycopg.Connection) -> dict[str, Any]:
        row = _person_row(conn, usuario)
        if _last_admin(conn, row):
            raise HTTPException(status_code=409, detail=f"{usuario} es el último admin activo: no se puede eliminar.")
        return row

    row = await asyncio.to_thread(_read, check)
    label = f"{usuario} (#{row['uid']})"
    erase_day = _human_date(_erase_at(datetime.now(timezone.utc)))

    async def run() -> dict[str, Any]:
        def work(conn: psycopg.Connection) -> dict[str, Any]:
            again = _person_row(conn, usuario)
            if _last_admin(conn, again):
                raise HTTPException(status_code=409, detail=f"{usuario} es el último admin activo: no se puede eliminar.")
            done = conn.execute(q.SOFT_DELETE_USER, {"usuario": usuario}).fetchone()
            _bitacora(conn, session, "user.delete", {
                "usuario": usuario, "uid": done["uid"], "borra": _erase_at(done["deleted_at"]).isoformat(),
            })
            return done

        done = await asyncio.to_thread(_tx, work)
        dropped = drop_sessions_of(usuario)
        log.info("user deleted %s by=%s (recoverable %d days)", usuario, session.user, USER_RECOVERY_DAYS)
        return {
            "usuario": usuario, "uid": done["uid"],
            "borra": _erase_at(done["deleted_at"]).isoformat(), "sesiones_cerradas": dropped,
        }

    return propose(
        session,
        action="eliminar la cuenta " + label,
        diff={
            "resumen": (
                f"Esta acción es permanente: la cuenta {label} se cierra ahora (no puede entrar y sus sesiones "
                f"terminan) y el {erase_day} se borra para siempre. Hasta entonces puedes recuperarla en «Eliminadas»."
            ),
            "cambios": [{
                "que": "cuenta " + label,
                "antes": "activa" if row["active"] else "desactivada",
                "despues": "eliminada (se borra el " + erase_day + ")",
            }],
        },
        run=run,
    )


@router.post("/api/admin/users/{usuario}/recover")
async def recover_user(usuario: str, session: Session = Depends(require_admin)) -> dict[str, Any]:
    """Recuperar: the deleted account comes back active, with its same number."""

    def work(conn: psycopg.Connection) -> dict[str, Any]:
        done = conn.execute(q.RECOVER_USER, {"usuario": usuario}).fetchone()
        if done is None:
            raise HTTPException(status_code=404, detail=f"La cuenta {usuario} no está en Eliminadas.")
        _bitacora(conn, session, "user.recover", {"usuario": usuario, "uid": done["uid"]})
        return done

    done = await asyncio.to_thread(_tx, work)
    log.info("user recovered %s by=%s", usuario, session.user)
    return {"usuario": usuario, "uid": done["uid"], "active": True}


PASSWORD_HISTORY_LIMIT = 100


def _history_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        d = r["detail"] or {}
        if r["action"] == "site.create":
            cuenta, uid, que = d.get("manager"), d.get("manager_uid"), "creada con su contraseña inicial (restaurante nuevo)"
        elif r["action"] == "user.create":
            cuenta, uid, que = d.get("usuario"), d.get("uid"), "creada con su contraseña inicial"
        elif r["action"] == "user.password":
            cuenta, uid, que = d.get("usuario"), d.get("uid"), "cambiada por otra cuenta"
        else:
            cuenta, uid, que = d.get("usuario"), d.get("uid"), "cambiada por la misma cuenta"
        out.append({
            "ts": r["ts"].isoformat(), "cuenta": cuenta, "uid": uid, "que": que,
            "por": r["usuario"], "por_uid": r["usuario_uid"],
        })
    return out


@router.get("/api/admin/password-history")
async def password_history(
    uid: int | None = None,
    limit: int = PASSWORD_HISTORY_LIMIT,
    session: Session = Depends(require_admin),
) -> dict[str, Any]:
    """Who set whose password and when, newest first (the bitácora's own rows; never a hash). limit is clamped
    to 1..PASSWORD_HISTORY_LIMIT; uid narrows it to one account (its permanent number)."""
    limit = max(1, min(limit, PASSWORD_HISTORY_LIMIT))
    if uid is None:
        rows = await asyncio.to_thread(_read, lambda c: c.execute(q.PASSWORD_HISTORY, {"limit": limit}).fetchall())
    else:
        rows = await asyncio.to_thread(
            _read, lambda c: c.execute(q.PASSWORD_HISTORY_OF, {"uid": str(uid), "limit": limit}).fetchall())
    return {"historial": _history_rows(rows), "limite": limit, "uid": uid}
