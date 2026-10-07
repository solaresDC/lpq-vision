"""LPQ_VISION api/admin_sites.py: sites and accounts in the mostrador (SPEC 1.3, 1.5; a family of the admin hub).

GET  /api/admin/sites?site=                          the visible sites: DB row + config block + the auditor
POST /api/admin/sites                                Crear restaurante (admin): ALL OR NOTHING across DB and config
POST /api/admin/sites/{site}/edit                    active / ingestion (admin; deactivating asks for the double confirm)
POST /api/admin/sites/{site}/funciones               the site's two function switches
POST /api/admin/sites/{site}/cameras                 add one camera, or change its source and role
POST /api/admin/sites/{site}/cameras/{camera}/delete remove one camera (double confirm; never the last one)
POST /api/admin/sites/{site}/cameras/{camera}/source the relevo, the mostrador's road
POST /api/admin/sites/{site}/gafete/reveal           show the site's gafete to enroll a device (admin)
POST /api/admin/sites/{site}/gafete/regenerate       a new gafete; every enrolled device enrolls again (admin, double confirm)
GET  /api/admin/users                                the accounts and the "Eliminadas" (admin; never the machine row)
POST /api/admin/users                                create an account (admin)
POST /api/admin/users/{usuario}/password|active|role new password, activate or deactivate, role and site (admin)
POST /api/admin/users/{usuario}/delete               delete: typed name + double confirm; recoverable (admin)
POST /api/admin/users/{usuario}/recover              Recuperar a deleted account (admin)

A manager sees and edits ONLY its own site's functions and cameras (and its relevo); the gafete, the active
switch, Crear restaurante and the accounts are the admin's. Every write leaves a bitácora row carrying the
actor's name AND permanent account number (users.uid, never reused). A write that spans the database and
config.yaml is all or nothing (_db_and_config). Accounts (owner ruling 3.9.2, amending SPEC §3): deleting
one asks for its exact name, then the double confirm; it closes at once, stays recoverable for
USER_RECOVERY_DAYS with its name reserved, and the worker's sweep erases it after warning the admin chat.
Never yourself, never the last active admin, never the machine row; a deleted account can't be edited until
it is recovered; any change to an account drops its open sessions. A gafete is never logged and never
written to the bitácora: it reaches only the admin's screen, on request.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from psycopg import errors as pg_errors
from psycopg.types.json import Jsonb

from api import config_writer as cw
from api.admin import log_action, propose, require_admin, require_mostrador, write_scope
from api.auth import ROLE_MACHINE, Session, drop_sessions_of, make_hash
from brain.db import queries as q
from brain.db.init import DEVICE_KEY_BYTES
from brain.validator.models import (
    DEFAULT_TIMEZONE,
    USER_RECOVERY_DAYS,
    CameraUpsertRequest,
    Config,
    ConfirmNameRequest,
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


def _db_and_config_sync(db_work: Callable[[psycopg.Connection], Any], ops: list[cw.Op]) -> Any:
    written: cw.WriteResult | None = None
    try:
        with q.connect() as conn, conn.transaction():
            value = db_work(conn)
            written = cw.apply_ops(ops)
        return value
    except Exception:
        if written is not None and written.written:
            cw.restore(written.previous_text)      # the database refused at commit: config goes back too
        raise


async def _db_and_config(db_work: Callable[[psycopg.Connection], Any], ops: list[cw.Op]) -> Any:
    """All or nothing across the database and config.yaml, under the one writer's lock."""
    async with cw.LOCK:
        try:
            return await asyncio.to_thread(_db_and_config_sync, db_work, ops)
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


def _site_view(row: dict[str, Any], site_cfg: SiteConfig | None) -> dict[str, Any]:
    view: dict[str, Any] = {
        "site": row["site"],
        "active": bool(row["active"]),
        "ingestion": row["ingestion"],
        "chat_set": row["telegram_chat_id"] is not None,
        "in_config": site_cfg is not None,
    }
    if site_cfg is None:
        view.update({"funciones": None, "cameras": [], "auditor": ["Este sitio existe en la base pero falta en config.yaml."]})
    else:
        view.update({
            "funciones": site_cfg.funciones.model_dump(),
            "cameras": [
                {"name": name, "source": cam.source, "role": cam.role, "kitchen_edge": cam.kitchen_edge}
                for name, cam in site_cfg.cameras.items()
            ],
            "auditor": audit(site_cfg),
        })
    return view


async def _view_of(site: str) -> dict[str, Any]:
    cfg = await _config()
    row = await asyncio.to_thread(_read, lambda c: c.execute(q.SELECT_SITE, {"site": site}).fetchone())
    if row is None:
        raise HTTPException(status_code=404, detail=f"sitio desconocido: {site}")
    return _site_view(row, cfg.sites.get(site))


@router.get("/api/admin/sites")
async def list_sites(site: str | None = None, session: Session = Depends(require_mostrador)) -> dict[str, Any]:
    scope = (site or None) if session.is_admin else session.site
    cfg = await _config()
    rows = await asyncio.to_thread(_read, lambda c: c.execute(q.SELECT_SITES_ALL).fetchall())
    views = [_site_view(r, cfg.sites.get(r["site"])) for r in rows if scope is None or r["site"] == scope]
    return {"sites": views}


# --- Crear restaurante (admin): all or nothing ------------------------------------------------

@router.post("/api/admin/sites")
async def create_site(body: SiteCreateRequest, session: Session = Depends(require_admin)) -> dict[str, Any]:
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
        manager_uid = conn.execute(q.INSERT_USER, {
            "usuario": body.manager_usuario, "password_hash": digest, "role": "manager", "site": body.site,
        }).fetchone()["uid"]
        cloned = 0
        if body.clone_menu_from:
            cloned = conn.execute(q.CLONE_SITE_PHOTOS, {"site": body.site, "from_site": body.clone_menu_from}).rowcount
        _bitacora(conn, session, "site.create", {
            "site": body.site, "manager": body.manager_usuario, "manager_uid": manager_uid,
            "cameras": [c.name for c in body.cameras], "funciones": body.funciones.model_dump(),
            "menu_de": body.clone_menu_from, "fotos": cloned,
        })
        return cloned

    cloned = await _db_and_config(db_work, [cw.set_op(("sites", body.site), block, flow=False)])
    log.info("site created %s manager=%s photos=%d user=%s", body.site, body.manager_usuario, cloned, session.user)
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
                    f"El sitio {site} deja de recibir fotos y sus dispositivos quedan fuera (su gafete deja de abrir). "
                    "Sus datos no se borran y se puede volver a activar."
                ),
                "cambios": [{"que": "activo", "antes": True, "despues": False}],
            },
            run=apply,
        )
    return await apply()


# --- functions and cameras (own site for a manager) --------------------------------------------

@router.post("/api/admin/sites/{site}/funciones")
async def set_funciones(site: str, body: Funciones, session: Session = Depends(require_mostrador)) -> dict[str, Any]:
    target = write_scope(session, site)
    _site_cfg(await _config(), target)
    await _write([cw.set_op(("sites", target, "funciones"), cw.Flow(body.model_dump()))])
    await asyncio.to_thread(log_action, session.user, "site.funciones", {"site": target, **body.model_dump()}, session.uid)
    return await _view_of(target)


@router.post("/api/admin/sites/{site}/cameras")
async def upsert_camera(site: str, body: CameraUpsertRequest, session: Session = Depends(require_mostrador)) -> dict[str, Any]:
    target = write_scope(session, site)
    site_cfg = _site_cfg(await _config(), target)
    existing = site_cfg.cameras.get(body.name)
    base = ("sites", target, "cameras", body.name)
    if existing is None:
        ops = [cw.set_op(base, {"source": body.source, "role": body.role}, flow=False)]
        action = "camera.add"
    else:
        ops = [cw.set_op(base + ("source",), body.source), cw.set_op(base + ("role",), body.role)]
        # A calibration is only true for the device that made it and only means something for role 'both'.
        if existing.kitchen_edge is not None and (body.role != "both" or body.source != existing.source):
            ops.append(cw.delete_op(base + ("kitchen_edge",)))
        action = "camera.edit"
    await _write(ops)
    await asyncio.to_thread(log_action, session.user, action, {
        "site": target, "camera": body.name, "source": body.source, "role": body.role,
    }, session.uid)
    return await _view_of(target)


@router.post("/api/admin/sites/{site}/cameras/{camera}/delete")
async def delete_camera(site: str, camera: str, session: Session = Depends(require_mostrador)) -> dict[str, Any]:
    target = write_scope(session, site)
    site_cfg = _site_cfg(await _config(), target)
    cam = site_cfg.cameras.get(camera)
    if cam is None:
        raise HTTPException(status_code=404, detail=f"La cámara {camera} no existe en {target}.")
    if len(site_cfg.cameras) == 1:
        raise HTTPException(status_code=400, detail="Un sitio necesita al menos una cámara: agrega otra antes de quitar esta.")

    async def run() -> dict[str, Any]:
        await _write([cw.delete_op(("sites", target, "cameras", camera))])
        await asyncio.to_thread(log_action, session.user, "camera.delete", {"site": target, "camera": camera}, session.uid)
        return await _view_of(target)

    return propose(
        session,
        action="quitar la cámara " + camera + " de " + target,
        diff={
            "resumen": f"La cámara {camera} deja de existir en {target}; sus fotos ya guardadas no se borran.",
            "cambios": [{
                "que": camera,
                "antes": SOURCE_WORDS.get(cam.source, cam.source) + ", " + ROLE_WORDS.get(cam.role, cam.role),
                "despues": "(sin cámara)",
            }],
        },
        run=run,
    )


async def relevo(site: str, camera: str, source: str, actor: Session | None, road: str) -> dict[str, Any]:
    """The ONE relevo, shared by both roads (the mostrador's switch here; /captura's emergency road from 3.13,
    which passes actor None). The camera's source flips, and its kitchen_edge goes with the old device
    (calibrate again). One write, one bitácora row; the bot announces the relevo from that row."""
    site_cfg = _site_cfg(await _config(), site)
    cam = site_cfg.cameras.get(camera)
    if cam is None:
        raise HTTPException(status_code=404, detail=f"La cámara {camera} no existe en {site}.")
    if cam.source == source:
        return {"site": site, "camera": camera, "source": source, "changed": False}
    base = ("sites", site, "cameras", camera)
    ops = [cw.set_op(base + ("source",), source)]
    if cam.kitchen_edge is not None:
        ops.append(cw.delete_op(base + ("kitchen_edge",)))
    await _write(ops)
    await asyncio.to_thread(log_action, actor.user if actor else None, "relevo", {
        "site": site, "camera": camera, "source": source, "road": road,
        "calibracion_borrada": cam.kitchen_edge is not None,
    }, actor.uid if actor else None)
    log.info("relevo %s/%s -> %s road=%s", site, camera, source, road)
    return {"site": site, "camera": camera, "source": source, "changed": True}


@router.post("/api/admin/sites/{site}/cameras/{camera}/source")
async def camera_source(site: str, camera: str, body: SourceRequest, session: Session = Depends(require_mostrador)) -> dict[str, Any]:
    target = write_scope(session, site)
    return await relevo(target, camera, body.source, session, "mostrador")


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
                f"Cada teléfono o Pi inscrito en {site} deja de entrar hasta inscribirse con el gafete nuevo "
                "(instalar primero, inscribir después)."
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
             "active": bool(r["active"]), "created_at": r["created_at"].isoformat()}
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
    digest = await asyncio.to_thread(make_hash, body.password)

    def work(conn: psycopg.Connection) -> None:
        row = _person_row(conn, usuario)
        conn.execute(q.RESET_USER_PASSWORD, {"usuario": usuario, "password_hash": digest})
        _bitacora(conn, session, "user.password", {"usuario": usuario, "uid": row["uid"]})

    await asyncio.to_thread(_tx, work)
    dropped = drop_sessions_of(usuario)
    log.info("user password reset %s by=%s", usuario, session.user)
    return {"usuario": usuario, "sesiones_cerradas": dropped}


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
