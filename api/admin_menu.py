"""LPQ_VISION api/admin_menu.py: the menu in the mostrador (SPEC 1.5; owner ruling 3.10.1; a family of the admin hub).

GET  /api/admin/menu?site=                   the dishes (the DATABASE is the menu's edited truth), each with its
                                             UNIVERSAL photos and, for a site, its LOCAL photos and which set the
                                             model uses
GET  /api/admin/menu/photo?path=             a reference photo's ORIGINAL at full resolution, for the panels (its
                                             light copy only when no original exists)
POST /api/admin/menu/dishes                  CREAR PLATILLO (admin): born switched on at the current MAX version
POST /api/admin/menu/dishes/{dish_id}/edit   the full definition (admin); a new SET of component names moves the
                                             dish to global MAX+1, behind the double confirm
POST /api/admin/menu/dishes/{dish_id}/active on/off (admin; off asks for the double confirm); never bumps
POST /api/admin/menu/photos                  upload a PAIR (multipart); site=brand is the UNIVERSAL level (admin
                                             only); with `replace`, that one row moves to the new pair
POST /api/admin/menu/photos/delete           detach one photo row of either level (double confirm)

menu.yaml is only the seed and the backup road now: the worker reads the database every job, so a change here
is live on the next plate. The version clock is MAX(menu_version) over every dish: CREAR PLATILLO is born at the
current MAX (no bump), a toggle or a photo never bumps, and only a change in the SET of a dish's component names
moves it to global MAX+1 (each plate's menu_version then tells which universe judged it).

Reference photos (owner ruling 3.10.1): two levels. UNIVERSAL photos (table brand_dish_photos; the api calls the
level "brand", a reserved slug no site can carry) belong to the admin and serve every site, current and future,
that has no LOCAL photo of the dish. LOCAL photos (site_dish_photos) belong to their site and win over the
universal ones. Every upload writes a PAIR sharing one stem: the ORIGINAL, exactly as received, under
reference/<dish>/original/ (shown in the panels, never modified, never sent to the model) and the LIGHT copy
(long side REF_DIET_MAX_SIDE, JPEG REF_DIET_QUALITY) under reference/<dish>/, which rows point to and the model
reads (never shown). Copy-on-diverge: replacing repoints ONE row to a new pair; a file another row still uses is
never touched. GC: after a detach or a repoint, a pair that no row of either level references dies together,
whoever placed it. A manager reads the definitions and the universal photos, and handles ONLY its own site's
local photos.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
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

from api.admin import propose, require_admin, require_mostrador, write_scope
from api.auth import Session, scoped_site
from brain.capture.backends import PHOTO_ROOT
from brain.db import queries as q
from brain.validator.models import DishCreateRequest, DishEditRequest, RefPhotoRequest, SwitchRequest
from brain.worker.menu_builder import BRAND_SITE

log = logging.getLogger("lpq.api.admin_menu")

router = APIRouter()

# --- named constants (SPEC section 4) -------------------------------------------------
REFERENCE_DIR = "reference"
ORIGINAL_DIR = "original"
UPLOAD_PREFIX = "up-"              # the stems the uploader writes (a hand-placed pair carries any other stem)
REF_DIET_MAX_SIDE = 1568           # the model's light copy: Anthropic scales any longer side down to about this anyway
REF_DIET_QUALITY = 85
REF_UPLOAD_MAX_MB = 15
CONDITIONS = ("normal", "lampara")
JPEG_MAGIC = b"\xff\xd8\xff"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


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
    row = conn.execute(q.SELECT_DISH, {"dish_id": dish_id}).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"No existe el platillo {dish_id}.")
    return row


def _site_or_404(conn: psycopg.Connection, site: str) -> None:
    if conn.execute(q.SELECT_SITE, {"site": site}).fetchone() is None:
        raise HTTPException(status_code=404, detail=f"sitio desconocido: {site}")


def _names(componentes: list[Any]) -> list[str]:
    return [c["nombre"] if isinstance(c, dict) else c.nombre for c in componentes]


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


# --- the menu, read -------------------------------------------------------------------------

@router.get("/api/admin/menu")
async def menu(site: str | None = None, session: Session = Depends(require_mostrador)) -> dict[str, Any]:
    scope = scoped_site(session, site)            # a manager is forced to its own site; the admin may choose none

    def work(conn: psycopg.Connection) -> dict[str, Any]:
        if scope:
            _site_or_404(conn, scope)
        dishes = conn.execute(q.SELECT_ALL_DISHES).fetchall()
        version = int(conn.execute(q.MAX_MENU_VERSION).fetchone()["v"])
        local = conn.execute(q.SELECT_ALL_SITE_DISH_PHOTOS).fetchall()
        brand = conn.execute(q.SELECT_BRAND_DISH_PHOTOS).fetchall()
        users_of: dict[str, list[str]] = {}
        for p in local:
            users_of.setdefault(p["photo_path"], []).append(p["site"])
        out: list[dict[str, Any]] = []
        for d in dishes:
            item: dict[str, Any] = {
                "dish_id": d["dish_id"],
                "nombre": d["nombre"],
                "plate_type": d["plate_type"],
                "componentes": [{"nombre": c["nombre"], "porcion_g": c["porcion_g"]} for c in d["componentes"]],
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


# --- definitions (admin) -------------------------------------------------------------------------

@router.post("/api/admin/menu/dishes")
async def create_dish(body: DishCreateRequest, session: Session = Depends(require_admin)) -> dict[str, Any]:
    """CREAR PLATILLO: born switched on at the current MAX version (no bump); its photos come later."""
    def work(conn: psycopg.Connection) -> int:
        born = int(conn.execute(q.MAX_MENU_VERSION).fetchone()["v"])
        cur = conn.execute(q.INSERT_MENU_DISH, {
            "dish_id": body.dish_id,
            "nombre": body.nombre,
            "plate_type": body.plate_type,
            "componentes": Jsonb([c.model_dump() for c in body.componentes]),
            "contable": None,
            "activo": True,
            "menu_version": born,
        })
        if cur.rowcount == 0:
            raise HTTPException(status_code=409, detail=f"Ya existe el platillo {body.dish_id}.")
        _bitacora(conn, session, "dish.create", {
            "dish_id": body.dish_id, "componentes": _names(body.componentes), "menu_version": born,
        })
        return born

    born = await asyncio.to_thread(_tx, work)
    log.info("dish created %s version=%d by=%s", body.dish_id, born, session.user)
    return {"dish_id": body.dish_id, "menu_version": born, "activo": True}


@router.post("/api/admin/menu/dishes/{dish_id}/edit")
async def edit_dish(dish_id: str, body: DishEditRequest, session: Session = Depends(require_admin)) -> dict[str, Any]:
    """The full new definition. Same SET of component names (portions, the visible name, the plate type): saved
    as is, same version. A different SET: the double confirm, then global MAX+1."""
    row = await asyncio.to_thread(_read, lambda c: _dish_or_404(c, dish_id))
    old_names = _names(row["componentes"])
    new_names = _names(body.componentes)
    new_set = set(new_names)

    async def apply() -> dict[str, Any]:
        def work(conn: psycopg.Connection) -> tuple[int, bool]:
            current = _dish_or_404(conn, dish_id)
            bump = set(_names(current["componentes"])) != new_set
            version = int(conn.execute(q.MAX_MENU_VERSION).fetchone()["v"]) + 1 if bump else int(current["menu_version"])
            conn.execute(q.UPDATE_DISH_DEFINITION, {
                "dish_id": dish_id,
                "nombre": body.nombre,
                "plate_type": body.plate_type,
                "componentes": Jsonb([c.model_dump() for c in body.componentes]),
                "menu_version": version,
            })
            _bitacora(conn, session, "dish.edit", {
                "dish_id": dish_id,
                "antes": [{"nombre": c["nombre"], "porcion_g": c["porcion_g"]} for c in current["componentes"]],
                "despues": [c.model_dump() for c in body.componentes],
                "menu_version": version,
                "sube_version": bump,
            })
            return version, bump

        version, bump = await asyncio.to_thread(_tx, work)
        log.info("dish edited %s version=%d bump=%s by=%s", dish_id, version, bump, session.user)
        return {"dish_id": dish_id, "menu_version": version, "bump": bump}

    if set(old_names) == new_set:
        return await apply()
    next_version = int(await asyncio.to_thread(_read, lambda c: c.execute(q.MAX_MENU_VERSION).fetchone()["v"])) + 1
    added = [n for n in new_names if n not in old_names]
    removed = [n for n in old_names if n not in new_set]
    lista = []
    if added:
        lista.append("Se agregan: " + ", ".join(added))
    if removed:
        lista.append("Se quitan: " + ", ".join(removed))
    return propose(
        session,
        action="cambiar los componentes de " + dish_id,
        diff={
            "resumen": (
                f"Cambia el conjunto de componentes de {row['nombre']}: el menú pasa a la versión {next_version} y los "
                "platos nuevos se analizan con la definición nueva. Los platos ya analizados conservan su versión."
            ),
            "cambios": [{"que": "componentes", "antes": ", ".join(old_names), "despues": ", ".join(new_names)}],
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
            action="apagar el platillo " + dish_id,
            diff={
                "resumen": (
                    f"El modelo deja de considerar {row['nombre']}: un plato de este platillo se clasificará como otro "
                    "o como desconocido. La versión del menú no cambia, y se puede volver a prender."
                ),
                "cambios": [{"que": dish_id, "antes": "prendido", "despues": "apagado"}],
            },
            run=apply,
        )
    return await apply()


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

    def look(conn: psycopg.Connection) -> tuple[list[dict[str, Any]], list[str], bool, bool]:
        local_all = conn.execute(q.SELECT_ALL_SITE_DISH_PHOTOS).fetchall()
        brand_all = conn.execute(q.SELECT_BRAND_DISH_PHOTOS).fetchall()
        if universal:
            mine = [r for r in brand_all if r["dish_id"] == body.dish_id]
        else:
            mine = [r for r in local_all if r["site"] == target and r["dish_id"] == body.dish_id]
        others = [r["site"] for r in local_all if r["photo_path"] == path and (universal or r["site"] != target)]
        also_brand = (not universal) and any(r["photo_path"] == path for r in brand_all)
        dish_has_brand = any(r["dish_id"] == body.dish_id for r in brand_all)
        return mine, others, also_brand, dish_has_brand

    mine, others, also_brand, dish_has_brand = await asyncio.to_thread(_read, look)
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

    where = "universal de " + body.dish_id if universal else "local de " + body.dish_id + " en " + target
    return propose(
        session,
        action="quitar una foto " + where,
        diff={
            "resumen": "La foto deja de ser referencia " + ("universal" if universal else "de " + target) + " de " + body.dish_id + ".",
            "cambios": [{"que": path, "antes": "universal" if universal else "local de " + target, "despues": "(quitada)"}],
            "lista": lista,
        },
        run=run,
    )
