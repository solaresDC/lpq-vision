"""LPQ_VISION api/admin_menu.py: the Menú tab of the mostrador, the recetario book (SPEC 1.5; owner rulings 3.10.1
and 3.10.2; a family of the admin hub).

GET  /api/admin/menu?site=                   the book: every dish in page order, with the names and labels people
                                             read, its notes, its earlier names, its UNIVERSAL photos and, for a
                                             site, its LOCAL photos and which set the model uses
GET  /api/admin/menu/photo?path=             a reference photo's ORIGINAL at full resolution, for the panels (its
                                             light copy only when no original exists)
POST /api/admin/menu/dishes                  a new page (admin): the clave is made from the name and each component
                                             key from its label; born switched on at the current MAX version, on
                                             the last page
POST /api/admin/menu/dishes/{dish_id}/edit   a page's full definition (admin): a rename keeps the clave and files
                                             the old name; a new SET of component keys moves the dish to global
                                             MAX+1, behind the double confirm; a new label never does
POST /api/admin/menu/dishes/{dish_id}/active on/off (admin; off asks for the double confirm); never bumps
POST /api/admin/menu/order                   Ordenar páginas (admin): every dish exactly once, the same in every site
POST /api/admin/menu/ficha                   Leer ficha (admin, multipart): ONE photo of ONE recetario page, read by
                                             the active model into a PROPOSAL for a new page; nothing is saved
POST /api/admin/menu/photos                  upload a PAIR (multipart); site=brand is the UNIVERSAL level (admin
                                             only); with `replace`, that one row moves to the new pair
POST /api/admin/menu/photos/delete           detach one photo row of either level (double confirm)

The recetario book (owner ruling 3.10.2): nobody types or sees an internal key. A dish's clave (dish_id) is made
here from its name at birth (accents dropped, snake_case, starting with a letter, never the model's word for an
unknown plate) and never changes; a rename files the old name in dish_name_history. A component's key (the JSON
key the model answers with) is made here from its label the first time and never changes; the label people read
lives beside it, so renaming a component never bumps the version. "Tipo de plato" stays out of the page until a
báscula exists: GET leaves it out, and an edit that does not send it keeps the stored value. "Notas para el
modelo" travel with the dish to the model (menu_builder); NULL = never written, '' = cleared by a person. Leer
ficha calls the model ONCE, outside the queue, with prompt_ficha_v1; the adapter is imported at the first read,
so the api's boot never loads it.

menu.yaml is only the seed and the backup road: the worker reads the database every job, so a change here is live
on the next plate. The version clock is MAX(menu_version) over every dish: a new page is born at the current MAX (no
bump); a toggle, a photo, a rename, a label, a portion or a note never bumps; only a change in the SET of a dish's
component keys moves it to global MAX+1 (each plate's menu_version then tells which universe judged it).

Reference photos (owner ruling 3.10.1): two levels. UNIVERSAL photos (table brand_dish_photos; the api calls the
level "brand", a reserved slug no site can carry) belong to the admin and serve every site, current and future,
that has no LOCAL photo of the dish. LOCAL photos (site_dish_photos) belong to their site and win over the
universal ones. Every upload writes a PAIR sharing one stem: the ORIGINAL, exactly as received, under
reference/<dish>/original/ (shown in the panels, never modified, never sent to the model) and the LIGHT copy
(long side REF_DIET_MAX_SIDE, JPEG REF_DIET_QUALITY) under reference/<dish>/, which rows point to and the model
reads (never shown). Copy-on-diverge: replacing repoints ONE row to a new pair; a file another row still uses is
never touched. GC: after a detach or a repoint, a pair that no row of either level references dies together,
whoever placed it. A manager reads the book and the universal photos, and handles ONLY its own site's local
photos.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
import unicodedata
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import cv2
import numpy as np
import psycopg
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from api.admin import propose, require_admin, require_mostrador, write_scope
from api.auth import Session, scoped_site
from brain.capture.backends import PHOTO_ROOT
from brain.db import queries as q
from brain.validator.models import (
    COMPONENTES_MAX,
    ETIQUETA_MAX_CHARS,
    FICHA_PROMPT_FILE,
    NOMBRE_MAX_CHARS,
    NOTAS_MAX_CHARS,
    PORCION_MAX_G,
    ComponenteForm,
    DishCreateRequest,
    DishEditRequest,
    FichaLectura,
    MenuOrderRequest,
    RefPhotoRequest,
    SwitchRequest,
    load_config,
)
from brain.validator.repair import UNKNOWN_DISH
from brain.worker.menu_builder import BRAND_SITE

log = logging.getLogger("lpq.api.admin_menu")

router = APIRouter()

# --- named constants (SPEC section 4) -------------------------------------------------
REFERENCE_DIR = "reference"
ORIGINAL_DIR = "original"
UPLOAD_PREFIX = "up-"              # the stems the uploader writes (a hand-placed pair carries any other stem)
REF_DIET_MAX_SIDE = 1568           # the model's light copy: Anthropic scales any longer side down to about this anyway
REF_DIET_QUALITY = 85
REF_UPLOAD_MAX_MB = 15             # one reference photo, or one recetario page for Leer ficha
CONDITIONS = ("normal", "lampara")
JPEG_MAGIC = b"\xff\xd8\xff"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
CLAVE_MAX = 40                     # a made key's length before any _2 suffix (dish clave or component key)
CLAVE_FALLBACK = "platillo"        # the clave of a name with no letters, and the prefix that keeps a clave legal
COMPONENT_FALLBACK = "componente"  # the key of a label with no letters
FICHA_MAX_TOKENS = 2048            # a recetario page's JSON: up to COMPONENTES_MAX components, each with a doubt
_FENCE_RE = re.compile(r"```[a-zA-Z]*")
_NOT_KEY_RE = re.compile(r"[^a-z0-9]+")


# --- helpers ------------------------------------------------------------------------------

def _read(fn: Callable[[psycopg.Connection], Any]) -> Any:
    with q.connect() as conn:
        return fn(conn)


def _tx(fn: Callable[[psycopg.Connection], Any]) -> Any:
    with q.connect() as conn, conn.transaction():
        return fn(conn)


def _bitacora(conn: psycopg.Connection, actor: Session, action: str, detail: dict[str, Any]) -> None:
    conn.execute(q.INSERT_ADMIN_LOG_BY, {
        "usuario": actor.user, "usuario_uid": actor.uid, "action": action, "detail": Jsonb(detail),
    })


def _dish_or_404(conn: psycopg.Connection, dish_id: str) -> dict[str, Any]:
    row = conn.execute(q.SELECT_DISH_BOOK, {"dish_id": dish_id}).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Ese platillo ya no existe: recarga la página.")
    return row


def _site_or_404(conn: psycopg.Connection, site: str) -> None:
    if conn.execute(q.SELECT_SITE, {"site": site}).fetchone() is None:
        raise HTTPException(status_code=404, detail=f"sitio desconocido: {site}")


def _names(componentes: list[dict[str, Any]]) -> list[str]:
    return [c["nombre"] for c in componentes]


def _safe_diet(path: str) -> PurePosixPath | None:
    """A light copy's relative path is exactly reference/<dish>/<file>: an original (one level deeper), an
    absolute path or a climb out of the photos root is refused by shape."""
    p = PurePosixPath(path)
    if p.is_absolute() or ".." in p.parts or len(p.parts) != 3 or p.parts[0] != REFERENCE_DIR:
        return None
    return p


def _original_of(diet_rel: str) -> Path | None:
    """The ORIGINAL of a light copy: the file with the same stem under original/ (found by name, never opened here)."""
    p = PurePosixPath(diet_rel)
    matches = sorted((PHOTO_ROOT / str(p.parent) / ORIGINAL_DIR).glob(p.stem + ".*"))
    return matches[0] if matches else None


def _photo_item(path: str, condition: str, shared: list[str]) -> dict[str, Any]:
    return {
        "photo_path": path,
        "condition": condition,
        "compartida_con": shared,
        "original": _original_of(path) is not None,
    }


# --- names, claves and keys (owner ruling 3.10.2) ----------------------------------------------

def _fold(text: str) -> str:
    """A name the way people compare names: no accents, no case, single spaces ("Jitómate" == "jitomate")."""
    plain = "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))
    return " ".join(plain.casefold().split())


def _slug(text: str) -> str:
    """ASCII snake_case: accents dropped (ñ is n), every run of other characters one underscore."""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    return _NOT_KEY_RE.sub("_", plain).strip("_")[:CLAVE_MAX].rstrip("_")


def _unique(base: str, taken: set[str]) -> str:
    if base not in taken:
        return base
    n = 2
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"


def _new_clave(nombre: str, taken: set[str]) -> str:
    """A dish's clave, made ONCE from its name: it starts with a letter and is never the model's word for an
    unknown plate ("Desconocido" becomes platillo_desconocido)."""
    base = _slug(nombre) or CLAVE_FALLBACK
    if not base[0].isalpha() or base == UNKNOWN_DISH:
        base = f"{CLAVE_FALLBACK}_{base}"[:CLAVE_MAX].rstrip("_")
    return _unique(base, taken)


def _new_key(etiqueta: str, taken: set[str]) -> str:
    """A component's key, made ONCE from its label: the JSON key the model answers with, unique in its dish."""
    base = _slug(etiqueta) or COMPONENT_FALLBACK
    if not base[0].isalpha():
        base = f"c_{base}"[:CLAVE_MAX].rstrip("_")
    return _unique(base, taken)


def _readable(key: str) -> str:
    """The label of a component saved before labels existed: its key in plain words."""
    return key.replace("_", " ").capitalize()


def _label(component: dict[str, Any]) -> str:
    return component.get("etiqueta") or _readable(component["nombre"])


def _plan(current: list[dict[str, Any]], wanted: list[ComponenteForm]) -> list[dict[str, Any]]:
    """The definition to store. A component the page sends back with its key keeps it; a new one gets a key made
    from its label (a label equal to one this edit removes gets that key back: same name, same component). A
    key this dish does not have means the page is stale. Two labels that differ only in accents or case are the
    same label."""
    existing = set(_names(current))
    kept = [c.nombre for c in wanted if c.nombre]
    if any(key not in existing for key in kept):
        raise HTTPException(status_code=409, detail="El platillo cambió mientras lo editabas: recarga la página.")
    seen: set[str] = set()
    for c in wanted:
        folded = _fold(c.etiqueta)
        if folded in seen:
            raise HTTPException(status_code=400, detail=f"El componente «{c.etiqueta}» aparece dos veces.")
        seen.add(folded)
    taken = set(kept)
    out: list[dict[str, Any]] = []
    for c in wanted:
        key = c.nombre or _new_key(c.etiqueta, taken)
        taken.add(key)
        out.append({"nombre": key, "etiqueta": c.etiqueta, "porcion_g": c.porcion_g})
    return out


def _same_name(conn: psycopg.Connection, nombre: str, own: str | None) -> dict[str, Any] | None:
    """Another dish (on or off) whose name reads the same, ignoring accents and case."""
    target = _fold(nombre)
    for d in conn.execute(q.SELECT_MENU_BOOK).fetchall():
        if d["dish_id"] != own and _fold(d["nombre"]) == target:
            return d
    return None


def _same_name_error(other: dict[str, Any]) -> HTTPException:
    off = "" if other["activo"] else " (apagado)"
    return HTTPException(status_code=409, detail=f"Ya hay un platillo que se llama «{other['nombre']}»{off}: elige otro nombre.")


# --- the book, read -------------------------------------------------------------------------

@router.get("/api/admin/menu")
async def menu(site: str | None = None, session: Session = Depends(require_mostrador)) -> dict[str, Any]:
    """Every dish in page order (a dish never placed: last, by name). dish_id travels so the page can call the
    endpoints; the page never shows it. Without a báscula there is no plate_type here (owner ruling 3.10.2)."""
    scope = scoped_site(session, site)            # a manager is forced to its own site; the admin may choose none

    def work(conn: psycopg.Connection) -> dict[str, Any]:
        if scope:
            _site_or_404(conn, scope)
        dishes = conn.execute(q.SELECT_MENU_BOOK).fetchall()
        version = int(conn.execute(q.MAX_MENU_VERSION).fetchone()["v"])
        local = conn.execute(q.SELECT_ALL_SITE_DISH_PHOTOS).fetchall()
        brand = conn.execute(q.SELECT_BRAND_DISH_PHOTOS).fetchall()
        history = conn.execute(q.SELECT_DISH_NAME_HISTORY).fetchall()
        users_of: dict[str, list[str]] = {}
        for p in local:
            users_of.setdefault(p["photo_path"], []).append(p["site"])
        earlier: dict[str, list[dict[str, Any]]] = {}
        for h in history:
            earlier.setdefault(h["dish_id"], []).append({"nombre": h["nombre"], "hasta": h["hasta"].isoformat()})
        out: list[dict[str, Any]] = []
        for page, d in enumerate(dishes, start=1):
            item: dict[str, Any] = {
                "dish_id": d["dish_id"],
                "pagina": page,
                "nombre": d["nombre"],
                "antes": earlier.get(d["dish_id"], []),
                "componentes": [
                    {"nombre": c["nombre"], "etiqueta": _label(c), "porcion_g": c["porcion_g"]} for c in d["componentes"]
                ],
                "notas": d["notas"] or "",
                "activo": bool(d["activo"]),
                "menu_version": int(d["menu_version"]),
                "universales": [
                    _photo_item(b["photo_path"], b["condition"], []) for b in brand if b["dish_id"] == d["dish_id"]
                ],
            }
            if scope:
                mine = [p for p in local if p["site"] == scope and p["dish_id"] == d["dish_id"]]
                item["locales"] = [
                    _photo_item(p["photo_path"], p["condition"], [s for s in users_of.get(p["photo_path"], []) if s != scope])
                    for p in mine
                ]
                item["usa"] = "local" if mine else ("universal" if item["universales"] else "ninguna")
            out.append(item)
        return {"menu_version": version, "dishes": out}

    body = await asyncio.to_thread(_read, work)
    return {
        "site": scope,
        "brand": BRAND_SITE,
        "menu_version": body["menu_version"],
        "dishes": body["dishes"],
        "conditions": list(CONDITIONS),
        "diet_max_side": REF_DIET_MAX_SIDE,
        "upload_max_mb": REF_UPLOAD_MAX_MB,
        "limites": {
            "nombre": NOMBRE_MAX_CHARS,
            "etiqueta": ETIQUETA_MAX_CHARS,
            "porcion_g": PORCION_MAX_G,
            "componentes": COMPONENTES_MAX,
            "notas": NOTAS_MAX_CHARS,
        },
    }


@router.get("/api/admin/menu/photo")
async def ref_photo(path: str, session: Session = Depends(require_mostrador)) -> FileResponse:
    """What the panels show: the photo's ORIGINAL at full resolution (the light copy only when no original exists).
    `path` is a light copy a row points to: anything else is 404. A universal photo is visible to every account;
    a local one to the admin and to its own site's manager."""
    safe = _safe_diet(path)
    if safe is None:
        raise HTTPException(status_code=404, detail="foto de referencia no encontrada")

    def work(conn: psycopg.Connection) -> tuple[list[str], bool]:
        local = [r["site"] for r in conn.execute(q.SELECT_ALL_SITE_DISH_PHOTOS).fetchall() if r["photo_path"] == path]
        brand = any(r["photo_path"] == path for r in conn.execute(q.SELECT_BRAND_DISH_PHOTOS).fetchall())
        return local, brand

    sites, in_brand = await asyncio.to_thread(_read, work)
    if not sites and not in_brand:
        raise HTTPException(status_code=404, detail="foto de referencia no encontrada")
    if not session.is_admin and not in_brand and session.site not in sites:
        raise HTTPException(status_code=403, detail="esa foto no es de tu sitio")
    original = await asyncio.to_thread(_original_of, path)
    file = original if original is not None else PHOTO_ROOT / str(safe)
    if not file.is_file():
        raise HTTPException(status_code=404, detail="la foto falta en el disco")
    media = "image/png" if file.suffix.lower() == ".png" else "image/jpeg"
    return FileResponse(file, media_type=media, headers={"Cache-Control": "private, max-age=300"})


# --- pages (admin) ------------------------------------------------------------------------------

@router.post("/api/admin/menu/dishes")
async def create_dish(body: DishCreateRequest, session: Session = Depends(require_admin)) -> dict[str, Any]:
    """A new page (Guardar after Leer ficha or Crear a mano): the clave is made from the name and each component
    key from its label; born switched on at the current MAX version (no bump), on the last page. Its notes are
    written now ('' when empty), so the one-time copy of the menu.yaml descriptions never touches it."""
    if any(c.nombre for c in body.componentes):
        raise HTTPException(status_code=400, detail="Un platillo nuevo no trae claves internas: recarga la página.")

    def work(conn: psycopg.Connection) -> dict[str, Any]:
        other = _same_name(conn, body.nombre, None)
        if other is not None:
            raise _same_name_error(other)
        clave = _new_clave(body.nombre, {d["dish_id"] for d in conn.execute(q.SELECT_ALL_DISHES).fetchall()})
        componentes = _plan([], body.componentes)
        born = int(conn.execute(q.MAX_MENU_VERSION).fetchone()["v"])
        conn.execute(q.NUMBER_MENU_PAGES)            # every page gets its current number, so the new one goes last
        page = int(conn.execute(q.NEXT_MENU_PAGE).fetchone()["n"])
        cur = conn.execute(q.INSERT_MENU_DISH, {
            "dish_id": clave,
            "nombre": body.nombre,
            "plate_type": body.plate_type,
            "componentes": Jsonb(componentes),
            "contable": None,
            "activo": True,
            "menu_version": born,
        })
        if cur.rowcount == 0:
            raise HTTPException(status_code=409, detail="Otro cambio llegó al mismo tiempo: vuelve a guardar.")
        conn.execute(q.SET_DISH_ORDEN, {"dish_id": clave, "orden": page})
        conn.execute(q.SET_DISH_NOTAS, {"dish_id": clave, "notas": (body.notas or "").strip()})
        _bitacora(conn, session, "dish.create", {
            "dish_id": clave, "nombre": body.nombre, "componentes": _names(componentes),
            "menu_version": born, "pagina": page,
        })
        return {"dish_id": clave, "nombre": body.nombre, "menu_version": born, "activo": True, "pagina": page}

    result = await asyncio.to_thread(_tx, work)
    log.info("dish created %s version=%d page=%d by=%s", result["dish_id"], result["menu_version"], result["pagina"], session.user)
    return result


@router.post("/api/admin/menu/dishes/{dish_id}/edit")
async def edit_dish(dish_id: str, body: DishEditRequest, session: Session = Depends(require_admin)) -> dict[str, Any]:
    """A page's full new definition. Same SET of component keys (names, labels, portions, notes): saved as is,
    same version. A different SET: the double confirm, then global MAX+1. A rename keeps the clave and files the
    old name. notas and plate_type change only when the page sends them."""
    sent = body.model_fields_set

    def check(conn: psycopg.Connection) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        current = _dish_or_404(conn, dish_id)
        other = _same_name(conn, body.nombre, dish_id)
        if other is not None:
            raise _same_name_error(other)
        return current, _plan(current["componentes"], body.componentes)

    current, wanted = await asyncio.to_thread(_read, check)

    async def apply() -> dict[str, Any]:
        def work(conn: psycopg.Connection) -> dict[str, Any]:
            now, componentes = check(conn)               # again, inside the transaction: the page may be stale
            bump = set(_names(now["componentes"])) != set(_names(componentes))
            version = int(conn.execute(q.MAX_MENU_VERSION).fetchone()["v"]) + 1 if bump else int(now["menu_version"])
            conn.execute(q.UPDATE_DISH_DEFINITION, {
                "dish_id": dish_id,
                "nombre": body.nombre,
                "plate_type": body.plate_type if "plate_type" in sent else now["plate_type"],
                "componentes": Jsonb(componentes),
                "menu_version": version,
            })
            renamed = body.nombre != now["nombre"]
            if renamed:
                conn.execute(q.INSERT_DISH_NAME_HISTORY, {
                    "dish_id": dish_id, "nombre": now["nombre"], "usuario": session.user, "usuario_uid": session.uid,
                })
            notas = (body.notas or "").strip()
            notas_changed = "notas" in sent and notas != (now["notas"] or "")
            if notas_changed:
                conn.execute(q.SET_DISH_NOTAS, {"dish_id": dish_id, "notas": notas})
            detail: dict[str, Any] = {
                "dish_id": dish_id,
                "antes": [{"nombre": c["nombre"], "etiqueta": _label(c), "porcion_g": c["porcion_g"]} for c in now["componentes"]],
                "despues": componentes,
                "menu_version": version,
                "sube_version": bump,
            }
            if renamed:
                detail["nombre_antes"] = now["nombre"]
                detail["nombre"] = body.nombre
            if notas_changed:
                detail["notas_cambiadas"] = True
            _bitacora(conn, session, "dish.edit", detail)
            return {"dish_id": dish_id, "nombre": body.nombre, "menu_version": version, "bump": bump}

        result = await asyncio.to_thread(_tx, work)
        log.info("dish edited %s version=%d bump=%s by=%s", dish_id, result["menu_version"], result["bump"], session.user)
        return result

    old_keys = _names(current["componentes"])
    new_keys = _names(wanted)
    if set(old_keys) == set(new_keys):
        return await apply()
    next_version = int(await asyncio.to_thread(_read, lambda c: c.execute(q.MAX_MENU_VERSION).fetchone()["v"])) + 1
    old_labels = {c["nombre"]: _label(c) for c in current["componentes"]}
    new_labels = {c["nombre"]: c["etiqueta"] for c in wanted}
    added = [new_labels[k] for k in new_keys if k not in old_labels]
    removed = [old_labels[k] for k in old_keys if k not in new_labels]
    lista = []
    if added:
        lista.append("Se agregan: " + ", ".join(added))
    if removed:
        lista.append("Se quitan: " + ", ".join(removed))
    return propose(
        session,
        action="cambiar los componentes de " + current["nombre"],
        diff={
            "resumen": (
                f"Cambia el conjunto de componentes de {current['nombre']}: el menú pasa a la versión {next_version} y "
                "los platos nuevos se analizan con la definición nueva. Los platos ya analizados conservan su versión."
            ),
            "cambios": [{
                "que": "componentes",
                "antes": ", ".join(old_labels[k] for k in old_keys),
                "despues": ", ".join(new_labels[k] for k in new_keys),
            }],
            "lista": lista,
        },
        run=apply,
    )


@router.post("/api/admin/menu/dishes/{dish_id}/active")
async def set_dish_active(dish_id: str, body: SwitchRequest, session: Session = Depends(require_admin)) -> dict[str, Any]:
    """On/off never bumps the version. Switching a dish off asks for the double confirm."""
    row = await asyncio.to_thread(_read, lambda c: _dish_or_404(c, dish_id))

    async def apply() -> dict[str, Any]:
        def work(conn: psycopg.Connection) -> None:
            _dish_or_404(conn, dish_id)
            conn.execute(q.SET_DISH_ACTIVE, {"dish_id": dish_id, "activo": body.on})
            _bitacora(conn, session, "dish.activate" if body.on else "dish.deactivate", {"dish_id": dish_id})

        await asyncio.to_thread(_tx, work)
        log.info("dish %s %s by=%s", dish_id, "on" if body.on else "off", session.user)
        return {"dish_id": dish_id, "activo": body.on}

    if not body.on and row["activo"]:
        return propose(
            session,
            action="apagar " + row["nombre"],
            diff={
                "resumen": (
                    f"El modelo deja de considerar {row['nombre']}: un plato de este platillo se clasificará como otro "
                    "o como desconocido. La versión del menú no cambia, y se puede volver a prender."
                ),
                "cambios": [{"que": row["nombre"], "antes": "prendido", "despues": "apagado"}],
            },
            run=apply,
        )
    return await apply()


@router.post("/api/admin/menu/order")
async def set_order(body: MenuOrderRequest, session: Session = Depends(require_admin)) -> dict[str, Any]:
    """Ordenar páginas: the brand's one order, the same in every site. Every dish exactly once, the first page
    first; a list that misses or repeats a dish means the page is stale."""
    def work(conn: psycopg.Connection) -> list[str]:
        before = [d["dish_id"] for d in conn.execute(q.SELECT_MENU_BOOK).fetchall()]
        if len(body.orden) != len(set(body.orden)) or set(body.orden) != set(before):
            raise HTTPException(status_code=409, detail="El menú cambió mientras ordenabas: recarga la página.")
        for page, dish_id in enumerate(body.orden, start=1):
            conn.execute(q.SET_DISH_ORDEN, {"dish_id": dish_id, "orden": page})
        _bitacora(conn, session, "menu.order", {"antes": before, "despues": body.orden})
        return body.orden

    orden = await asyncio.to_thread(_tx, work)
    log.info("menu pages reordered n=%d by=%s", len(orden), session.user)
    return {"orden": orden}


# --- Leer ficha (admin; owner ruling 3.10.2) -------------------------------------------------------

def _lectura(text: str) -> FichaLectura | None:
    """The model's answer as a FichaLectura: code fences and prose around the outermost {...} are dropped."""
    cleaned = _FENCE_RE.sub("", text or "")
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    try:
        return FichaLectura.model_validate(data)
    except ValidationError:
        return None


@router.post("/api/admin/menu/ficha")
async def read_ficha(photo: UploadFile = File(...), session: Session = Depends(require_admin)) -> dict[str, Any]:
    """ONE photo of ONE recetario page, shrunk like a reference light copy, read by the active model in ONE call
    outside the queue (one timeout, no retry). The answer is a PROPOSAL for a new page: nothing is saved here and
    the photo is never stored. One menu.ficha bitácora row keeps the tokens, whether the reading worked or not."""
    data = await photo.read(REF_UPLOAD_MAX_MB * 1024 * 1024 + 1)
    if not data:
        raise HTTPException(status_code=400, detail="La foto llegó vacía.")
    if len(data) > REF_UPLOAD_MAX_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"La foto pesa más de {REF_UPLOAD_MAX_MB} MB.")
    _ext, img = await asyncio.to_thread(_decode, data)
    page = await asyncio.to_thread(_diet, img)
    cfg = await asyncio.to_thread(load_config)
    prompt = await asyncio.to_thread(FICHA_PROMPT_FILE.read_text, "utf-8")

    from brain.adapter import llm                  # lazy: litellm loads at the first read, never at the api's boot

    model = cfg.active_model
    started = time.monotonic()
    try:
        result = await asyncio.to_thread(
            llm.complete, model, llm.build_ficha_messages(prompt, page), llm.provider_for(model),
            max_tokens=FICHA_MAX_TOKENS, timeout_s=cfg.machine.llm_timeout_s,
        )
    except Exception as exc:
        log.warning("ficha read failed model=%s error=%s", model, type(exc).__name__)
        await asyncio.to_thread(_tx, lambda c: _bitacora(c, session, "menu.ficha", {
            "ok": False, "modelo": model, "error": type(exc).__name__,
        }))
        raise HTTPException(status_code=502, detail="El modelo no respondió: intenta otra vez en un momento.") from None
    seconds = round(time.monotonic() - started, 1)
    lectura = _lectura(result.text)

    def record(conn: psycopg.Connection) -> dict[str, Any] | None:
        detail: dict[str, Any] = {"ok": lectura is not None, "modelo": result.model, "segundos": seconds, **result.usage()}
        if lectura is not None:
            detail.update({"nombre": lectura.nombre, "componentes": len(lectura.componentes), "dudas": len(lectura.dudas)})
        _bitacora(conn, session, "menu.ficha", detail)
        return _same_name(conn, lectura.nombre, None) if lectura is not None and lectura.nombre else None

    other = await asyncio.to_thread(_tx, record)
    if lectura is None:
        log.info("ficha unreadable model=%s seconds=%s by=%s", result.model, seconds, session.user)
        raise HTTPException(
            status_code=502,
            detail="No se pudo leer la ficha: prueba con otra foto (la página completa, derecha, con luz y sin reflejos).",
        )
    log.info("ficha read model=%s seconds=%s components=%d by=%s", result.model, seconds, len(lectura.componentes), session.user)
    return {
        "lectura": lectura.model_dump(),
        "segundos": seconds,
        "ya_existe": other["nombre"] if other is not None else None,
    }


# --- reference photos: universal (site=brand, admin) and local (own site for a manager) ---------

def _decode(data: bytes) -> tuple[str, np.ndarray]:
    if data.startswith(JPEG_MAGIC):
        ext = "jpg"
    elif data.startswith(PNG_MAGIC):
        ext = "png"
    else:
        raise HTTPException(status_code=400, detail="La foto debe ser JPEG o PNG.")
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(status_code=400, detail="No se pudo leer la imagen: ¿está dañada?")
    return ext, img


def _diet(img: np.ndarray) -> bytes:
    h, w = img.shape[:2]
    scale = min(1.0, REF_DIET_MAX_SIDE / max(h, w))
    if scale < 1.0:
        img = cv2.resize(img, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, REF_DIET_QUALITY])
    if not ok:
        raise RuntimeError("light copy encode failed")
    return buf.tobytes()


def _atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _write_pair(dish_id: str, ext: str, original: bytes, diet: bytes) -> tuple[str, list[Path]]:
    """The pair under one stem: the original exactly as received, and the light copy. Returns the light copy's
    relative path and both files written."""
    stem = UPLOAD_PREFIX + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(3)
    diet_rel = f"{REFERENCE_DIR}/{dish_id}/{stem}.jpg"
    original_path = PHOTO_ROOT / REFERENCE_DIR / dish_id / ORIGINAL_DIR / f"{stem}.{ext}"
    diet_path = PHOTO_ROOT / diet_rel
    _atomic(original_path, original)        # the original: exactly as received, never modified afterwards
    _atomic(diet_path, diet)
    return diet_rel, [diet_path, original_path]


def _gc_pair(conn: psycopg.Connection, diet_rel: str) -> list[str]:
    """A pair that no row of EITHER level references dies together (its light copy and its original), whoever
    placed it (owner ruling 3.10.1). A file some row still uses is never touched."""
    p = PurePosixPath(diet_rel)
    if _safe_diet(diet_rel) is None:
        return []
    if int(conn.execute(q.COUNT_PHOTO_REFS, {"photo_path": diet_rel}).fetchone()["n"]) > 0:
        return []
    diet_path = PHOTO_ROOT / diet_rel
    removed: list[str] = []
    for path in [diet_path, *sorted((diet_path.parent / ORIGINAL_DIR).glob(p.stem + ".*"))]:
        if path.is_file():
            path.unlink(missing_ok=True)
            removed.append(str(path.relative_to(PHOTO_ROOT)))
    if removed:
        log.info("reference pair collected %s files=%d", diet_rel, len(removed))
    return removed


@router.post("/api/admin/menu/photos")
async def upload_photo(
    photo: UploadFile = File(...),
    site: str = Form(...),
    dish_id: str = Form(...),
    condition: str = Form("normal"),
    replace: str | None = Form(None),
    session: Session = Depends(require_mostrador),
) -> dict[str, Any]:
    """Upload a pair. site=brand: the UNIVERSAL level (write_scope refuses it to a manager). Without `replace`: a
    new row of that level. With `replace` (a light path that level uses): that ONE row moves to the new pair
    (copy-on-diverge), and the old pair is collected once nobody uses it."""
    target = write_scope(session, site)
    if not target:
        raise HTTPException(status_code=400, detail="falta el sitio (o brand para las fotos universales)")
    universal = target == BRAND_SITE
    if condition not in CONDITIONS:
        raise HTTPException(status_code=400, detail="condición: normal o lampara")
    data = await photo.read(REF_UPLOAD_MAX_MB * 1024 * 1024 + 1)
    if not data:
        raise HTTPException(status_code=400, detail="La foto llegó vacía.")
    if len(data) > REF_UPLOAD_MAX_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"La foto pesa más de {REF_UPLOAD_MAX_MB} MB.")

    def check(conn: psycopg.Connection) -> None:
        if not universal:
            _site_or_404(conn, target)
        _dish_or_404(conn, dish_id)

    await asyncio.to_thread(_read, check)
    ext, img = await asyncio.to_thread(_decode, data)
    diet = await asyncio.to_thread(_diet, img)
    diet_rel, written = await asyncio.to_thread(_write_pair, dish_id, ext, data, diet)

    def work(conn: psycopg.Connection) -> None:
        if replace:
            params: dict[str, Any] = {"dish_id": dish_id, "photo_path": replace, "new_path": diet_rel, "condition": condition}
            if universal:
                cur = conn.execute(q.REPOINT_BRAND_DISH_PHOTO, params)
            else:
                cur = conn.execute(q.REPOINT_SITE_DISH_PHOTO, {**params, "site": target})
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="Esa foto ya no está ahí: recarga la página.")
        elif universal:
            conn.execute(q.INSERT_BRAND_DISH_PHOTO, {"dish_id": dish_id, "photo_path": diet_rel, "condition": condition})
        else:
            conn.execute(q.INSERT_SITE_DISH_PHOTO, {
                "site": target, "dish_id": dish_id, "photo_path": diet_rel, "condition": condition,
            })
        _bitacora(conn, session, "photo.replace" if replace else "photo.add", {
            "nivel": "universal" if universal else "local",
            "site": None if universal else target,
            "dish_id": dish_id, "photo_path": diet_rel, "antes": replace, "condition": condition,
        })

    try:
        await asyncio.to_thread(_tx, work)
    except Exception:
        for path in written:            # the new pair never got its row: it leaves no orphan behind
            path.unlink(missing_ok=True)
        raise
    removed = await asyncio.to_thread(_read, lambda c: _gc_pair(c, replace)) if replace else []
    log.info("reference photo %s %s/%s -> %s by=%s", "replaced" if replace else "added",
             "universal" if universal else target, dish_id, diet_rel, session.user)
    return {
        "nivel": "universal" if universal else "local",
        "site": None if universal else target,
        "dish_id": dish_id, "photo_path": diet_rel, "condition": condition,
        "reemplazo": replace, "borrados": removed,
    }


@router.post("/api/admin/menu/photos/delete")
async def delete_photo(body: RefPhotoRequest, session: Session = Depends(require_mostrador)) -> dict[str, Any]:
    """Detach ONE row of either level (site=brand: universal, admin only), behind the double confirm. Its pair
    dies once no row of either level references it."""
    target = write_scope(session, body.site)
    if not target:
        raise HTTPException(status_code=400, detail="falta el sitio (o brand para las fotos universales)")
    universal = target == BRAND_SITE
    if not body.photo_path:
        raise HTTPException(status_code=400, detail="falta la foto a quitar")
    path = body.photo_path

    def look(conn: psycopg.Connection) -> tuple[str, list[dict[str, Any]], list[str], bool, bool]:
        nombre = _dish_or_404(conn, body.dish_id)["nombre"]
        local_all = conn.execute(q.SELECT_ALL_SITE_DISH_PHOTOS).fetchall()
        brand_all = conn.execute(q.SELECT_BRAND_DISH_PHOTOS).fetchall()
        if universal:
            mine = [r for r in brand_all if r["dish_id"] == body.dish_id]
        else:
            mine = [r for r in local_all if r["site"] == target and r["dish_id"] == body.dish_id]
        others = [r["site"] for r in local_all if r["photo_path"] == path and (universal or r["site"] != target)]
        also_brand = (not universal) and any(r["photo_path"] == path for r in brand_all)
        dish_has_brand = any(r["dish_id"] == body.dish_id for r in brand_all)
        return nombre, mine, others, also_brand, dish_has_brand

    nombre, mine, others, also_brand, dish_has_brand = await asyncio.to_thread(_read, look)
    if not any(r["photo_path"] == path for r in mine):
        raise HTTPException(status_code=404, detail="Esa foto no está ahí.")
    lista = []
    if others or also_brand:
        quienes = list(others) + (["las universales"] if also_brand else [])
        lista.append("Otros la usan (" + ", ".join(quienes) + "): el archivo se queda.")
    else:
        lista.append("Nadie más la usa: se borran su original y su copia ligera.")
    if len(mine) == 1:
        if universal:
            lista.append("Era la última foto universal de este platillo: las sucursales sin foto local se quedan sin referencia.")
        elif dish_has_brand:
            lista.append("Era la última foto local: este sitio vuelve a usar las fotos universales.")
        else:
            lista.append("Era la última foto de este platillo en el sitio: el modelo trabajará solo con la definición.")

    async def run() -> dict[str, Any]:
        def work(conn: psycopg.Connection) -> None:
            if universal:
                cur = conn.execute(q.DELETE_BRAND_DISH_PHOTO, {"dish_id": body.dish_id, "photo_path": path})
            else:
                cur = conn.execute(q.DELETE_SITE_DISH_PHOTO, {"site": target, "dish_id": body.dish_id, "photo_path": path})
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="Esa foto ya no está ahí.")
            _bitacora(conn, session, "photo.delete", {
                "nivel": "universal" if universal else "local",
                "site": None if universal else target,
                "dish_id": body.dish_id, "photo_path": path,
            })

        await asyncio.to_thread(_tx, work)
        removed = await asyncio.to_thread(_read, lambda c: _gc_pair(c, path))
        log.info("reference photo detached %s/%s %s by=%s", "universal" if universal else target, body.dish_id, path, session.user)
        return {"nivel": "universal" if universal else "local", "dish_id": body.dish_id, "photo_path": path, "borrados": removed}

    where = "universal de " + nombre if universal else "local de " + nombre + " en " + target
    return propose(
        session,
        action="quitar una foto " + where,
        diff={
            "resumen": "La foto deja de ser referencia " + ("universal" if universal else "de " + target) + " de " + nombre + ".",
            "cambios": [{"que": "una foto de " + nombre, "antes": "universal" if universal else "local de " + target, "despues": "(quitada)"}],
            "lista": lista,
        },
        run=run,
    )
