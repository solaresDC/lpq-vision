"""LPQ_VISION api/admin_voice.py: the bot's voice in the mostrador (SPEC 1.5; a family of the admin hub).

GET  /api/admin/voice                         every visible site's chat and mantenimiento; the admin chat
POST /api/admin/voice/chat/{site}             a site's Telegram chat; empty = removed      (sites.telegram_chat_id)
POST /api/admin/voice/admin_chat              the machine families' own chat (admin)     (config admin_chat_id)
POST /api/admin/voice/mantenimiento/{site}    mute a site's alerts (absent-when-off)     (config sites.<site>.mantenimiento)
POST /api/admin/voice/test                    enviar prueba: a site's chat, else the admin chat
GET  /api/admin/textos                        every bot message with its fichas (admin)
POST /api/admin/textos/save | restore | test  edit, restore or try one message (admin)

The api speaks through its OWN Telegram helper (api.admin.send_telegram), never through the bot, so a
test works even when the bot is the sick service. Where a test goes: the chosen site's chat; without a
site (admin), the admin chat, else the temporal .env chat while that key lives. Chat ids are shown on the
page (they are what the person pastes) but never logged and never written to the bitácora. A manager
handles only its own site's chat, mantenimiento and test; the admin chat and the texts are the admin's.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from api import config_writer as cw
from api.admin import ENV_CHAT_ID, alog, require_admin, require_mostrador, send_telegram, write_scope
from api.auth import Session
from brain.db import queries as q
from brain.validator import textos
from brain.validator.models import (
    ChatIdRequest,
    SwitchRequest,
    TelegramTestRequest,
    TextoKeyRequest,
    TextoSaveRequest,
    TextoTestRequest,
    load_config,
)

log = logging.getLogger("lpq.api.admin_voice")

router = APIRouter()

TEST_HEADER = "🧪 Prueba de texto «{key}» (las fichas llevan valores de ejemplo):\n"


# --- helpers ----------------------------------------------------------------------------

def _all_sites() -> list[dict[str, Any]]:
    with q.connect() as conn:
        return conn.execute(q.SELECT_SITES_ALL).fetchall()


def _site_row(site: str) -> dict[str, Any] | None:
    with q.connect() as conn:
        return conn.execute(q.SELECT_SITE, {"site": site}).fetchone()


def _env_chat() -> str | None:
    return (os.environ.get(ENV_CHAT_ID) or "").strip() or None


async def _write(ops: list[cw.Op]) -> None:
    try:
        await cw.write(ops)
    except cw.WriterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


async def _write_textos(overrides: dict[str, str]) -> None:
    try:
        await cw.write_textos(overrides)
    except (cw.WriterError, textos.TextoError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


async def _target(session: Session, site: str | None) -> tuple[str, str]:
    """(chat_id, what it will receive) for a test. A site (or any manager): that site's own chat. The
    admin without a site: the admin chat, else the temporal env chat. Nothing configured = 400."""
    if site or not session.is_admin:
        target = write_scope(session, site)
        row = await asyncio.to_thread(_site_row, target)
        if row is None:
            raise HTTPException(status_code=404, detail=f"sitio desconocido: {target}")
        if not row["telegram_chat_id"]:
            raise HTTPException(status_code=400, detail=f"El sitio {target} no tiene chat asignado: pégalo y guárdalo primero.")
        return str(row["telegram_chat_id"]), "las alertas del sitio " + target
    cfg = await asyncio.to_thread(load_config)
    if cfg.admin_chat_id:
        return cfg.admin_chat_id, "las alertas de máquina"
    env_chat = _env_chat()
    if env_chat:
        return env_chat, "las alertas de máquina (chat temporal de .env)"
    raise HTTPException(status_code=400, detail="No hay chat de admin: pégalo en Destino Telegram o elige un sitio.")


async def _send(chat_id: str, text: str) -> tuple[bool, str]:
    return await asyncio.to_thread(send_telegram, chat_id, text)


# --- destinations -------------------------------------------------------------------------

@router.get("/api/admin/voice")
async def voice(session: Session = Depends(require_mostrador)) -> dict[str, Any]:
    cfg = await asyncio.to_thread(load_config)
    rows = await asyncio.to_thread(_all_sites)
    sites = []
    for row in rows:
        if not session.is_admin and row["site"] != session.site:
            continue
        site_cfg = cfg.sites.get(row["site"])
        sites.append({
            "site": row["site"],
            "active": bool(row["active"]),
            "chat_id": None if row["telegram_chat_id"] is None else str(row["telegram_chat_id"]),
            "mantenimiento": bool(site_cfg is not None and site_cfg.mantenimiento),
            "in_config": site_cfg is not None,
        })
    out: dict[str, Any] = {"sites": sites}
    if session.is_admin:
        out["admin_chat_id"] = cfg.admin_chat_id
        out["env_chat"] = _env_chat() is not None
    return out


@router.post("/api/admin/voice/chat/{site}")
async def set_site_chat(site: str, body: ChatIdRequest, session: Session = Depends(require_mostrador)) -> dict[str, Any]:
    target = write_scope(session, site)

    def work() -> Any:
        with q.connect() as conn:
            return conn.execute(q.SET_SITE_CHAT, {"site": target, "chat_id": body.chat_id}).fetchone()

    if await asyncio.to_thread(work) is None:
        raise HTTPException(status_code=404, detail=f"sitio desconocido: {target}")
    await alog(session, "destino.sitio", {"site": target, "chat": "asignado" if body.chat_id else "quitado"})
    log.info("site chat %s site=%s user=%s", "set" if body.chat_id else "removed", target, session.user)
    return {"site": target, "chat_id": body.chat_id}


@router.post("/api/admin/voice/admin_chat")
async def set_admin_chat(body: ChatIdRequest, session: Session = Depends(require_admin)) -> dict[str, Any]:
    path = ("admin_chat_id",)
    await _write([cw.set_op(path, body.chat_id) if body.chat_id else cw.delete_op(path)])
    await alog(session, "destino.admin", {"chat": "asignado" if body.chat_id else "quitado"})
    log.info("admin chat %s user=%s", "set" if body.chat_id else "removed", session.user)
    return {"admin_chat_id": body.chat_id}


@router.post("/api/admin/voice/mantenimiento/{site}")
async def set_mantenimiento(site: str, body: SwitchRequest, session: Session = Depends(require_mostrador)) -> dict[str, Any]:
    target = write_scope(session, site)
    cfg = await asyncio.to_thread(load_config)
    if target not in cfg.sites:
        raise HTTPException(status_code=404, detail=f"sitio desconocido: {target}")
    path = ("sites", target, "mantenimiento")
    await _write([cw.set_op(path, True) if body.on else cw.delete_op(path)])
    await alog(session, "mantenimiento.on" if body.on else "mantenimiento.off", {"site": target})
    log.info("mantenimiento %s site=%s user=%s", "on" if body.on else "off", target, session.user)
    return {"site": target, "mantenimiento": body.on}


@router.post("/api/admin/voice/test")
async def test_destination(body: TelegramTestRequest, session: Session = Depends(require_mostrador)) -> dict[str, Any]:
    chat_id, que = await _target(session, body.site)
    overrides = await asyncio.to_thread(textos.load_textos)
    ok, detail = await _send(chat_id, textos.render("prueba_destino", {"que": que}, overrides))
    await alog(session, "destino.prueba", {"site": body.site or session.site, "ok": ok})
    if not ok:
        raise HTTPException(status_code=502, detail="Telegram no entregó el mensaje: " + detail)
    return {"ok": True, "enviado": que}


# --- the bot's texts (admin) ----------------------------------------------------------------

@router.get("/api/admin/textos")
async def list_textos(session: Session = Depends(require_admin)) -> dict[str, Any]:
    overrides = await asyncio.to_thread(textos.load_textos)
    return {"textos": textos.catalog_view(overrides), "max_chars": textos.TEXTO_MAX_CHARS}


@router.post("/api/admin/textos/save")
async def save_texto(body: TextoSaveRequest, session: Session = Depends(require_admin)) -> dict[str, Any]:
    try:
        textos.validate_template(body.key, body.text)
    except textos.TextoError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    overrides = dict(await asyncio.to_thread(textos.load_textos))
    if body.text == textos.FACTORY[body.key].texto:
        overrides.pop(body.key, None)            # identical to the factory text: no override (absent-when-off)
    else:
        overrides[body.key] = body.text
    await _write_textos(overrides)
    await alog(session, "texto.save", {"key": body.key})
    return {"key": body.key, "propio": body.key in overrides}


@router.post("/api/admin/textos/restore")
async def restore_texto(body: TextoKeyRequest, session: Session = Depends(require_admin)) -> dict[str, Any]:
    if body.key not in textos.FACTORY:
        raise HTTPException(status_code=404, detail=f"No existe el mensaje «{body.key}».")
    overrides = dict(await asyncio.to_thread(textos.load_textos))
    if body.key not in overrides:
        return {"key": body.key, "changed": False}
    overrides.pop(body.key)
    await _write_textos(overrides)
    await alog(session, "texto.restore", {"key": body.key})
    return {"key": body.key, "changed": True}


@router.post("/api/admin/textos/test")
async def test_texto(body: TextoTestRequest, session: Session = Depends(require_admin)) -> dict[str, Any]:
    """Send one message with its fichas' example values: the unsaved edit when `text` comes, else the saved text."""
    if body.key not in textos.FACTORY:
        raise HTTPException(status_code=404, detail=f"No existe el mensaje «{body.key}».")
    overrides = dict(await asyncio.to_thread(textos.load_textos))
    if body.text is not None:
        try:
            textos.validate_template(body.key, body.text)
        except textos.TextoError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        overrides[body.key] = body.text
    message = TEST_HEADER.format(key=body.key) + textos.render(body.key, textos.sample_values(body.key), overrides)
    chat_id, que = await _target(session, body.site)
    ok, detail = await _send(chat_id, message)
    await alog(session, "texto.prueba", {"key": body.key, "ok": ok})
    if not ok:
        raise HTTPException(status_code=502, detail="Telegram no entregó el mensaje: " + detail)
    return {"ok": True, "enviado": que}
