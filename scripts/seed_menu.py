"""T-E1: menu.yaml -> menu_dishes + site_dish_photos, re-runnable. Runs INSIDE the fastapi container:

    docker compose -f compose.mac.yaml exec fastapi python -m scripts.seed_menu

There /app/config, /data/photos and DATABASE_URL exist. It loads menu.yaml through the SAME
validated models and calls the SAME idempotent seed function init uses (imported, not duplicated),
then updates any EXISTING dish whose definition changed in the file (init's seed only inserts).
Every referenced reference photo must exist under the photos root and be small enough for the
model; otherwise it fails loudly naming the offenders and writes nothing. menu_version is never
touched here (bumping is FASE 3). Second run = zero new rows, zero updates.
"""

from __future__ import annotations

import logging
import sys

from psycopg.types.json import Jsonb

from brain.capture.backends import PHOTO_ROOT
from brain.db import init as db_init
from brain.db import queries as q
from brain.validator.models import load_config, load_menu

log = logging.getLogger("lpq.scripts.seed_menu")

# The model's image limit is 5 MB; a reference photo well under it keeps the cached prefix lean.
REF_PHOTO_MAX_BYTES = 2 * 1024 * 1024


def check_photos(menu) -> list[str]:
    """Every fotos_ref path must exist under PHOTO_ROOT and fit the size cap. Returns the problems."""
    problems: list[str] = []
    for site, site_menu in menu.sites.items():
        for dish_id, fotos in site_menu.fotos_ref.items():
            for foto in fotos:
                path = PHOTO_ROOT / foto.path
                if not path.is_file():
                    problems.append(f"{site}/{dish_id}: missing file {path} (copy it into the photos volume first)")
                elif path.stat().st_size > REF_PHOTO_MAX_BYTES:
                    problems.append(f"{site}/{dish_id}: {path} is {path.stat().st_size} bytes, max {REF_PHOTO_MAX_BYTES} (export it smaller)")
    return problems


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = load_config()
    menu = load_menu()

    unknown = sorted(set(menu.sites) - set(config.sites))
    if unknown:
        log.error("menu.yaml lists sites missing from config.yaml: %s", unknown)
        return 1
    problems = check_photos(menu)
    if problems:
        for p in problems:
            log.error("reference photo problem: %s", p)
        log.error("nothing written: fix the %d problem(s) above and run again", len(problems))
        return 1

    with q.connect() as conn, conn.transaction():
        new = db_init.seed(conn, config, menu)
        updated: list[str] = []
        for dish_id, dish in menu.menu.items():
            cur = conn.execute(
                q.UPDATE_MENU_DISH_IF_CHANGED,
                {
                    "dish_id": dish_id,
                    "nombre": dish.nombre,
                    "plate_type": dish.plate_type,
                    "componentes": Jsonb([c.model_dump() for c in dish.componentes]),
                    "contable": Jsonb(dish.contable) if dish.contable is not None else None,
                    "activo": dish.activo,
                },
            )
            if cur.rowcount:
                updated.append(dish_id)
        dishes = conn.execute(q.SELECT_ACTIVE_DISHES).fetchall()
        photos = {site: conn.execute(q.SELECT_SITE_DISH_PHOTOS, {"site": site}).fetchall() for site in menu.sites}

    log.info("seed: new sites=%d dishes=%d photos=%d; updated dishes=%s", new["sites"], new["dishes"], new["photos"], updated or "none")
    for d in dishes:
        comps = ", ".join(f"{c['nombre']} ({c['porcion_g']} g)" for c in d["componentes"])
        log.info("dish %s | %s | menu_version %d | %s", d["dish_id"], d["nombre"], d["menu_version"], comps)
    for site, rows in photos.items():
        for r in rows:
            log.info("photo %s | %s | %s (%s)", site, r["dish_id"], r["photo_path"], r["condition"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
