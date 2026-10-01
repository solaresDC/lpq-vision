"""LPQ_VISION api/admin_knobs.py: the mostrador's knobs (SPEC 1.4, 1.5; a family of the admin hub).

GET  /api/admin/knobs?site=      the first floor's registry, painted for the site being edited
POST /api/admin/knobs/set        one knob: the global value (admin) or one site's own value (override)
POST /api/admin/knobs/reset      "volver al global": delete one site's override line
POST /api/admin/knobs/clear      "borrar overrides (N)": step 1 of the double confirm (admin only)
POST /api/admin/raw_logging      the forensic river's switch, born with its auto-off time (admin only)

Every write goes through the ONE writer (api.config_writer) and leaves a bitácora row. The registry is the
whitelist: a name outside it, a machine-floor knob, or a special knob (it has its own endpoint) never
reaches the file from here. A manager writes ONLY its own site's overrides: a global knob (afecta a TODOS
los sitios) answers 403, and so does another site. A global change never touches an override; "volver al
global" deletes one line; "borrar overrides (N)" deletes exactly the lines its confirm showed, nothing else.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from api import config_writer as cw
from api.admin import FLOOR_MOSTRADOR, alog, propose, require_admin, require_mostrador, write_scope
from api.auth import Session, scoped_site
from brain.validator import knobs
from brain.validator.models import (
    RAW_LOG_AUTO_OFF_HOURS,
    Config,
    KnobNameRequest,
    KnobResetRequest,
    KnobWriteRequest,
    RawLoggingRequest,
    load_config,
)

log = logging.getLogger("lpq.api.admin_knobs")

router = APIRouter()


# --- helpers ----------------------------------------------------------------------------

async def _config() -> Config:
    return await asyncio.to_thread(load_config)


def _known_site(cfg: Config, site: str | None) -> None:
    if site is not None and site not in cfg.sites:
        raise HTTPException(status_code=404, detail=f"sitio desconocido: {site}")


def _editable(name: str) -> knobs.Knob:
    """The registry is the whitelist: only plain first-floor knobs are written from here."""
    knob = knobs.KNOBS.get(name)
    if knob is None:
        raise HTTPException(status_code=400, detail=f"«{name}» no es una perilla del registro: no se puede escribir.")
    if knob.floor != FLOOR_MOSTRADOR:
        raise HTTPException(status_code=403, detail=f"{name} se cambia en el cuarto de máquinas.")
    if knob.special:
        raise HTTPException(status_code=400, detail=f"{name} se cambia en su propia sección, no como perilla suelta.")
    return knob


def _described(cfg: Config, site: str | None) -> list[dict[str, Any]]:
    return knobs.describe(cfg, site=site, floor=FLOOR_MOSTRADOR)


def _item(cfg: Config, name: str, site: str | None) -> dict[str, Any]:
    return next(item for item in _described(cfg, site) if item["name"] == name)


async def _write(ops: list[cw.Op]) -> cw.WriteResult:
    try:
        return await cw.write(ops)
    except cw.WriterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


# --- the registry, painted ----------------------------------------------------------------

@router.get("/api/admin/knobs")
async def list_knobs(site: str | None = None, session: Session = Depends(require_mostrador)) -> dict[str, Any]:
    scope = scoped_site(session, site)            # a manager is forced to its own site
    cfg = await _config()
    _known_site(cfg, scope)
    described = await asyncio.to_thread(_described, cfg, scope)
    off_at = cfg.raw_logging_off_at.isoformat() if cfg.raw_logging_off_at else None
    return {
        "site": scope,
        "knobs": described,
        "raw_logging": {"on": cfg.raw_logging, "off_at": off_at},
        "auto_off_hours": RAW_LOG_AUTO_OFF_HOURS,
    }


# --- one knob -----------------------------------------------------------------------------

@router.post("/api/admin/knobs/set")
async def set_knob(body: KnobWriteRequest, session: Session = Depends(require_mostrador)) -> dict[str, Any]:
    knob = _editable(body.knob)
    site = write_scope(session, body.site)
    if not session.is_admin and not knob.per_site:
        raise HTTPException(status_code=403, detail=f"{knob.name} afecta a TODOS los sitios: solo el admin la cambia.")
    cfg = await _config()
    _known_site(cfg, site)
    try:
        path = knobs.config_path(knob.name, site)
    except knobs.KnobError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    before = knobs.effective(cfg, knob.name, site)
    if knob.optional and site is None and body.value in (None, ""):
        after: Any = None                        # an optional global back to factory: the key disappears
        ops = [cw.delete_op(path)]
    else:
        try:
            after = knobs.coerce(knob.name, body.value)
        except knobs.KnobError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        ops = [cw.set_op(path, after)]
    await _write(ops)
    await alog(session, "knob.set", {"knob": knob.name, "site": site, "antes": before, "despues": after})
    log.info("knob set %s site=%s user=%s", knob.name, site or "global", session.user)
    return {"knob": _item(await _config(), knob.name, site)}


@router.post("/api/admin/knobs/reset")
async def reset_knob(body: KnobResetRequest, session: Session = Depends(require_mostrador)) -> dict[str, Any]:
    """Volver al global: the site's own line disappears and the site inherits again."""
    knob = _editable(body.knob)
    site = write_scope(session, body.site)
    if not knob.per_site:
        raise HTTPException(status_code=400, detail=f"{knob.name} no tiene valor propio por sitio.")
    cfg = await _config()
    _known_site(cfg, site)
    overrides = cfg.sites[site].overrides
    if knob.name not in overrides:
        return {"knob": _item(cfg, knob.name, site), "changed": False}
    before = knobs.coerce(knob.name, overrides[knob.name])
    await _write([cw.delete_op(("sites", site, "overrides", knob.name))])
    await alog(session, "knob.reset", {"knob": knob.name, "site": site, "antes": before})
    log.info("knob reset %s site=%s user=%s", knob.name, site, session.user)
    return {"knob": _item(await _config(), knob.name, site), "changed": True}


@router.post("/api/admin/knobs/clear")
async def clear_overrides(body: KnobNameRequest, session: Session = Depends(require_admin)) -> dict[str, Any]:
    """Borrar overrides (N), step 1: the diff names every site and value that will die. Step 2 (the hub's
    confirm) deletes exactly those lines in ONE write; the global value and other knobs never change."""
    knob = _editable(body.knob)
    if not knob.per_site:
        raise HTTPException(status_code=400, detail=f"{knob.name} no tiene valores propios por sitio.")
    cfg = await _config()
    doomed = knobs.overrides_of(cfg, knob.name)
    if not doomed:
        raise HTTPException(status_code=400, detail=f"Ningún sitio tiene valor propio de {knob.name}: no hay nada que borrar.")
    global_now = knobs.global_value(cfg, knob.name)

    async def run() -> dict[str, Any]:
        await _write([cw.delete_op(("sites", site, "overrides", knob.name)) for site in doomed])
        await alog(session, "knob.clear_overrides", {"knob": knob.name, "sitios": doomed})
        log.info("knob overrides cleared %s sites=%d user=%s", knob.name, len(doomed), session.user)
        return {"knob": knob.name, "borrados": len(doomed)}

    return propose(
        session,
        action="borrar overrides de " + knob.name,
        diff={
            "resumen": (
                f"{len(doomed)} sitio(s) pierden su valor propio de {knob.name} y vuelven a heredar el global. "
                "El valor global y las demás perillas no cambian."
            ),
            "cambios": [{"que": site, "antes": value, "despues": global_now} for site, value in doomed.items()],
        },
        run=run,
    )


# --- the forensic river -------------------------------------------------------------------

@router.post("/api/admin/raw_logging")
async def raw_logging(body: RawLoggingRequest, session: Session = Depends(require_admin)) -> dict[str, Any]:
    """On: raw_logging true AND its off time, in one write (the api's timer switches it off then).
    Off: raw_logging false and the off time deleted (absent-when-off)."""
    if body.on:
        hours = body.hours or RAW_LOG_AUTO_OFF_HOURS
        off_at = (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(microsecond=0)
        await _write([cw.set_op(("raw_logging",), True), cw.set_op(("raw_logging_off_at",), off_at)])
        await alog(session, "raw_logging.on", {"horas": hours, "off_at": off_at.isoformat()})
        log.info("raw_logging on for %d h user=%s", hours, session.user)
        return {"on": True, "off_at": off_at.isoformat()}
    await _write([cw.set_op(("raw_logging",), False), cw.delete_op(("raw_logging_off_at",))])
    await alog(session, "raw_logging.off", {})
    log.info("raw_logging off user=%s", session.user)
    return {"on": False, "off_at": None}
