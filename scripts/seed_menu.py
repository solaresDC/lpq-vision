"""T-E1: menu.yaml -> NEW dishes and their reference photos, INSERT-ONLY. Runs INSIDE the fastapi container:

    docker compose -f compose.mac.yaml exec fastapi python -m scripts.seed_menu

From Fase 3 the DATABASE is the menu's edited truth (the mostrador edits it). This seeder adds only the
dishes that do not exist yet, born at the current MAX menu_version, with the photo rows menu.yaml names for
THEM. An existing dish is never updated and never gets a photo row back: a re-seed must never revert the
admin nor resurrect a row the admin detached. It loads menu.yaml through the SAME validated models and
calls the SAME seed function init uses (imported, not duplicated). Every reference photo it would insert
must exist under the photos root and be small enough for the model; otherwise it fails loudly, naming the
offenders, and writes nothing. A site it inserts gets its gafete at once. Second run = zero new rows.
"""

from __future__ import annotations

import logging
import sys

from brain.capture.backends import PHOTO_ROOT
from brain.db import init as db_init
from brain.db import queries as q
from brain.validator.models import Menu, load_config, load_menu

log = logging.getLogger("lpq.scripts.seed_menu")

# The model's image limit is 5 MB; a reference photo well under it keeps the cached prefix lean.
REF_PHOTO_MAX_BYTES = 2 * 1024 * 1024


def check_photos(menu: Menu, new_dishes: set[str]) -> list[str]:
    """Every fotos_ref path of a dish this run would insert must exist and fit the cap. Returns the problems."""
    problems: list[str] = []
    for site, site_menu in menu.sites.items():
        for dish_id, fotos in site_menu.fotos_ref.items():
            if dish_id not in new_dishes:
                continue
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

    with q.connect() as conn:
        existing = {r["dish_id"] for r in conn.execute(q.SELECT_ALL_DISHES).fetchall()}
    new_dishes = set(menu.menu) - existing
    problems = check_photos(menu, new_dishes)
    if problems:
        for p in problems:
            log.error("reference photo problem: %s", p)
        log.error("nothing written: fix the %d problem(s) above and run again", len(problems))
        return 1

    with q.connect() as conn, conn.transaction():
        new = db_init.seed(conn, config, menu)
        keys = db_init.ensure_device_keys(conn)
        dishes = conn.execute(q.SELECT_ALL_DISHES).fetchall()
        photos = {site: conn.execute(q.SELECT_SITE_DISH_PHOTOS, {"site": site}).fetchall() for site in menu.sites}

    untouched = sorted(existing & set(menu.menu))
    log.info(
        "seed: new sites=%d dishes=%d photos=%d gafetes=%d; existing dishes left untouched=%s",
        new["sites"], new["dishes"], new["photos"], keys, untouched or "none",
    )
    for d in dishes:
        comps = ", ".join(f"{c['nombre']} ({c['porcion_g']} g)" for c in d["componentes"])
        log.info("dish %s | %s | menu_version %d | activo %s | %s", d["dish_id"], d["nombre"], d["menu_version"], d["activo"], comps)
    for site, rows in photos.items():
        for r in rows:
            log.info("photo %s | %s | %s (%s)", site, r["dish_id"], r["photo_path"], r["condition"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
