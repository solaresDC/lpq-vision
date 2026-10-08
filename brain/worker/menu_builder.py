"""Per-site menu reference: universal definitions + that site's reference photos.

Reads the DATABASE (menu_dishes + site_dish_photos), never menu.yaml, so the
menu_version the worker stamps is the one actually used (SPEC 1.5). Inheritance: a dish
without a photo for this site borrows the photos stored under BRAND_SITE, if any. The
reference is a plain-text block (the model's universe) plus the photo bytes that travel
with it inside the stable prefix.

The version stamp (Fase 3) is MAX(menu_version) over ALL dishes, active or not: a version
clock never rewinds, so deactivating the top-version dish can never resurrect a number that
once meant a different menu. With every dish active (today) it equals the Fase-2 number.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import psycopg

from brain.capture.backends import PHOTO_ROOT
from brain.db import queries as q

log = logging.getLogger("lpq.worker.menu_builder")

# The UNIVERSAL level (owner ruling 3.10.1): the brand's photos live in brand_dish_photos and serve every
# site that has no LOCAL photo of a dish. "brand" is a reserved slug (no site can carry it): it is how the
# api names that level, never a sites row.
BRAND_SITE = "brand"


@dataclass(frozen=True)
class RefPhoto:
    dish_id: str
    path: str            # relative to PHOTO_ROOT
    condition: str       # 'normal' | 'lampara'
    jpeg: bytes


@dataclass(frozen=True)
class MenuReference:
    site: str
    menu_version: int
    dishes: dict[str, list[str]]     # dish_id -> component names, the validator's universe
    text: str                        # the block the model reads
    photos: tuple[RefPhoto, ...]     # site photos (own or inherited), in the order they travel


def _group_photos(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["dish_id"]].append(row)
    return grouped


def _load_photo(row: dict[str, Any]) -> RefPhoto | None:
    path = PHOTO_ROOT / row["photo_path"]
    try:
        jpeg = path.read_bytes()
    except FileNotFoundError:
        # A missing reference photo degrades the reference, it never stops an analysis.
        log.warning("reference photo missing on disk, skipped: %s", row["photo_path"])
        return None
    return RefPhoto(dish_id=row["dish_id"], path=row["photo_path"], condition=row["condition"], jpeg=jpeg)


def _render_text(site: str, menu_version: int, dishes: list[dict[str, Any]], photos: list[RefPhoto]) -> str:
    lines = [
        "REFERENCIA DEL MENU",
        f"Sitio: {site} | menu_version: {menu_version}",
        "Platillos activos (usa el dish_id exacto como valor de \"dish\"):",
    ]
    for dish in dishes:
        comps = ", ".join(f"{c['nombre']} ({c['porcion_g']} g)" for c in dish["componentes"])
        lines.append(f"- dish_id: {dish['dish_id']} | nombre: {dish['nombre']}")
        lines.append(f"  componentes: {comps}")
    if photos:
        lines.append("Fotos de referencia (arriba, en este orden):")
        for i, photo in enumerate(photos, start=1):
            lines.append(f"  {i}. {photo.dish_id} (luz: {photo.condition})")
    else:
        lines.append("Fotos de referencia: ninguna; trabaja a partir de las definiciones.")
    return "\n".join(lines)


def build_reference(conn: psycopg.Connection, site: str) -> MenuReference:
    """Universal layer + site layer (with brand inheritance) for ONE site, from the database."""
    dishes = conn.execute(q.SELECT_ACTIVE_DISHES).fetchall()
    own = _group_photos(conn.execute(q.SELECT_SITE_DISH_PHOTOS, {"site": site}).fetchall())
    brand = _group_photos(conn.execute(q.SELECT_BRAND_DISH_PHOTOS).fetchall())

    photos: list[RefPhoto] = []
    for dish in dishes:
        rows = own.get(dish["dish_id"]) or brand.get(dish["dish_id"], [])
        for row in rows:
            loaded = _load_photo(row)
            if loaded is not None:
                photos.append(loaded)

    # The version clock: MAX over ALL dishes (active or not), the same number upload stamps as the
    # plan of record. It never rewinds (SPEC 1.5 and HQ call 35).
    menu_version = int(conn.execute(q.MAX_MENU_VERSION).fetchone()["v"])
    universe = {d["dish_id"]: [c["nombre"] for c in d["componentes"]] for d in dishes}

    return MenuReference(
        site=site,
        menu_version=menu_version,
        dishes=universe,
        text=_render_text(site, menu_version, dishes, photos),
        photos=tuple(photos),
    )
