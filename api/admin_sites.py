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
GET  /api/admin/users                                the accounts (admin; the machine row is never listed)
POST /api/admin/users                                create an account (admin)
POST /api/admin/users/{usuario}/password|active|role new password, activate or deactivate, role and site (admin)

A manager sees and edits ONLY its own site's functions and cameras (and its relevo); the gafete, the active
switch, Crear restaurante and the accounts are the admin's. Every write leaves a bitácora row. A write that
spans the database and config.yaml is all or nothing (_db_and_config): under the writer's lock, the database
work and the config write share ONE transaction; a config bounce rolls the database back, and a failed commit
puts the previous config text back. An account is never deleted (it is deactivated); the last active admin can
never be deactivated or demoted, nobody can do that to themselves, and any change to an account drops its open
sessions at once. A gafete is never logged and never written to the bitácora: it reaches only the admin's
screen, on request.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Callable
from typing import Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from psycopg import errors as pg_errors
from psycopg.types.json import Jsonb

from api import config_writer as cw
from api.admin import alog, log_action, propose, require_admin, require_mostrador, write_scope
from api.auth import ROLE_MACHINE, Session, drop_sessions_of, make_hash
from brain.db import queries as q
from brain.db.init import DEVICE_KEY_BYTES
from brain.validator.models import (
    CameraUpsertRequest,
    Config,
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


def _bitacora(conn: psycopg.Connection, usuario: str | None, action: str, detail: dict[str, Any]) -> None:
    """A bitácora row INSIDE the caller's transaction: it lands only if the action lands."""
    conn.execute(q.INSERT_ADMIN_LOG, {"usuario": usuario, "action": action, "detail": Jsonb(detail)})


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
        conn.execute(q.INSERT_USER, {
            "usuario": body.manager_usuario, "password_hash": digest, "role": "manager", "site": body.site,
        })
        cloned = 0
        if body.clone_menu_from:
            cloned = conn.execute(q.CLONE_SITE_PHOTOS, {"site": body.site, "from_site": body.clone_menu_from}).rowcount
        _bitacora(conn, session.user, "site.create", {
            "site": body.site, "manager": body.manager_usuario, "cameras": [c.name for c in body.cameras],
            "funciones": body.funciones.model_dump(), "menu_de": body.clone_menu_from, "fotos": cloned,
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
            _bitacora(conn, session.user, "site.edit", {"site": site, "ingestion": body.ingestion, "active": body.active})

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
    await alog(session, "site.funciones", {"site": target, **body.model_dump()})
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
    await alog(session, action, {"site": target, "camera": body.name, "source": body.source, "role": body.role})
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
        await alog(session, "camera.delete", {"site": target, "camera": camera})
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


async def relevo(site: str, camera: str, source: str, usuario: str | None, road: str) -> dict[str, Any]:
    """The ONE relevo, shared by both roads (the mostrador's switch here; /captura's emergency road from 3.13).
    The camera's source flips, and its kitchen_edge goes with the old device (calibrate again). One write,
    one bitácora row; the bot announces the relevo from that row."""
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
    await asyncio.to_thread(log_action, usuario, "relevo", {
        "site": site, "camera": camera, "source": source, "road": road,
        "calibracion_borrada": cam.kitchen_edge is not None,
    })
    log.info("relevo %s/%s -> %s road=%s", site, camera, source, road)
    return {"site": site, "camera": camera, "source": source, "changed": True}


@router.post("/api/admin/sites/{site}/cameras/{camera}/source")
async def camera_source(site: str, camera: str, body: SourceRequest, session: Session = Depends(require_mostrador)) -> dict[str, Any]:
    target = write_scope(session, site)
    return await relevo(target, camera, body.source, session.user, "mostrador")


# --- the gafete (admin) ------------------------------------------------------------------------

@router.post("/api/admin/sites/{site}/gafete/reveal")
async def reveal_gafete(site: str, session: Session = Depends(require_admin)) -> dict[str, Any]:
    row = await asyncio.to_thread(_read, lambda c: c.execute(q.SELECT_DEVICE_KEY, {"site": site}).fetchone())
    if row is None or not row["device_key"]:
        raise HTTPException(status_code=404, detail=f"El sitio {site} no existe, está inactivo o no tiene gafete.")
    await alog(session, "gafete.reveal", {"site": site})
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
            _bitacora(conn, session.user, "gafete.regenerate", {"site": site})

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

def _person_row(conn: psycopg.Connection, usuario: str) -> dict[str, Any]:
    """An account a person uses; the machine row answers 404 here, exactly like a missing name."""
    row = conn.execute(q.SELECT_USER, {"usuario": usuario}).fetchone()
    if row is None or row["role"] == ROLE_MACHINE:
        raise HTTPException(status_code=404, detail=f"No existe la cuenta {usuario}.")
    return row


def _site_exists(conn: psycopg.Connection, site: str | None) -> None:
    if site and conn.execute(q.SELECT_SITE, {"site": site}).fetchone() is None:
        raise HTTPException(status_code=400, detail=f"sitio desconocido: {site}")


def _last_admin(conn: psycopg.Connection, row: dict[str, Any]) -> bool:
    return row["role"] == "admin" and bool(row["active"]) and int(conn.execute(q.COUNT_ACTIVE_ADMINS).fetchone()["n"]) <= 1


@router.get("/api/admin/users")
async def list_users(session: Session = Depends(require_admin)) -> dict[str, Any]:
    rows = await asyncio.to_thread(_read, lambda c: c.execute(q.LIST_USERS).fetchall())
    return {"users": [
        {"usuario": r["usuario"], "role": r["role"], "site": r["site"], "active": bool(r["active"]),
         "created_at": r["created_at"].isoformat()}
        for r in rows
    ]}


@router.post("/api/admin/users")
async def create_user(body: UserCreateRequest, session: Session = Depends(require_admin)) -> dict[str, Any]:
    digest = await asyncio.to_thread(make_hash, body.password)

    def work(conn: psycopg.Connection) -> None:
        _site_exists(conn, body.site)
        conn.execute(q.INSERT_USER, {"usuario": body.usuario, "password_hash": digest, "role": body.role, "site": body.site})
        _bitacora(conn, session.user, "user.create", {"usuario": body.usuario, "role": body.role, "site": body.site})

    try:
        await asyncio.to_thread(_tx, work)
    except pg_errors.UniqueViolation:
        raise HTTPException(status_code=409, detail=f"Ya existe una cuenta {body.usuario}.") from None
    log.info("user created %s role=%s by=%s", body.usuario, body.role, session.user)
    return {"usuario": body.usuario, "role": body.role, "site": body.site, "active": True}


@router.post("/api/admin/users/{usuario}/password")
async def reset_password(usuario: str, body: PasswordRequest, session: Session = Depends(require_admin)) -> dict[str, Any]:
    digest = await asyncio.to_thread(make_hash, body.password)

    def work(conn: psycopg.Connection) -> None:
        _person_row(conn, usuario)
        conn.execute(q.RESET_USER_PASSWORD, {"usuario": usuario, "password_hash": digest})
        _bitacora(conn, session.user, "user.password", {"usuario": usuario})

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
        _bitacora(conn, session.user, "user.activate" if body.on else "user.deactivate", {"usuario": usuario})

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
        _bitacora(conn, session.user, "user.role", {
            "usuario": usuario,
            "antes": {"role": row["role"], "site": row["site"]},
            "despues": {"role": body.role, "site": body.site},
        })

    await asyncio.to_thread(_tx, work)
    dropped = drop_sessions_of(usuario)
    log.info("user role %s -> %s by=%s", usuario, body.role, session.user)
    return {"usuario": usuario, "role": body.role, "site": body.site, "sesiones_cerradas": dropped}
