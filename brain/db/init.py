"""Idempotent database init, gated on existence (SPEC 1.8), plus the migrations on EVERY boot.

Owner: the fastapi service calls run() at startup, BEFORE serving. If the `sites`
table is absent, schema.sql runs verbatim and the seeds follow. If it is present, the
create step is skipped. EITHER WAY, brain.db.migrations.run() then applies every missing
Fase-2 migration (each gated on its own column's existence), all inside ONE transaction:
a half-built schema can never exist, and a database that already has everything passes
through in milliseconds.

`python -m brain.db.init` by hand is a debug tool only: the system never depends on
anyone remembering it.

Seeds: `sites` from config.yaml's sites block (the one source of truth for sites) and
`menu_dishes` + `site_dish_photos` from menu.yaml, all with ON CONFLICT DO NOTHING.
"""

from __future__ import annotations

import logging
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from brain.db import migrations
from brain.db import queries as q
from brain.validator.models import Config, Menu, load_config, load_menu

log = logging.getLogger("lpq.db.init")

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Every seeded dish is born with this menu_version. Bumping it is the admin's job (FASE 3).
SEED_MENU_VERSION = 1


def schema_present(conn: psycopg.Connection) -> bool:
    row = conn.execute(q.SCHEMA_PRESENT).fetchone()
    return bool(row and row["present"])


def _check_menu_sites(config: Config, menu: Menu) -> None:
    """Fail loudly BEFORE writing if menu.yaml names a site config.yaml does not know."""
    unknown = sorted(set(menu.sites) - set(config.sites))
    if unknown:
        raise ValueError(
            f"menu.yaml lists sites missing from config.yaml's sites block: {unknown}. "
            "Sites come from config.yaml only (one source of truth): add them there."
        )


def seed(conn: psycopg.Connection, config: Config, menu: Menu) -> dict[str, int]:
    """Insert sites, dishes and reference photos idempotently. Returns how many rows were NEW."""
    counts = {"sites": 0, "dishes": 0, "photos": 0}

    for site, site_cfg in config.sites.items():
        cur = conn.execute(q.INSERT_SITE, {"site": site, "ingestion": site_cfg.ingestion})
        counts["sites"] += cur.rowcount

    for dish_id, dish in menu.menu.items():
        cur = conn.execute(
            q.INSERT_MENU_DISH,
            {
                "dish_id": dish_id,
                "nombre": dish.nombre,
                "plate_type": dish.plate_type,
                "componentes": Jsonb([c.model_dump() for c in dish.componentes]),
                "contable": Jsonb(dish.contable) if dish.contable is not None else None,
                "activo": dish.activo,
                "menu_version": SEED_MENU_VERSION,
            },
        )
        counts["dishes"] += cur.rowcount

    for site, site_menu in menu.sites.items():
        for dish_id, fotos in site_menu.fotos_ref.items():
            for foto in fotos:
                cur = conn.execute(
                    q.INSERT_SITE_DISH_PHOTO,
                    {
                        "site": site,
                        "dish_id": dish_id,
                        "photo_path": foto.path,
                        "condition": foto.condition,
                    },
                )
                counts["photos"] += cur.rowcount

    return counts


def run() -> str:
    """Gate -> (schema + seeds) or nothing, THEN migrations, all in one transaction.

    Returns 'created' or 'present' (the schema gate's verdict); the migrations' own verdict
    goes to the log, per step.
    """
    config = load_config()
    menu = load_menu()
    _check_menu_sites(config, menu)

    with q.connect() as conn, conn.transaction():
        if schema_present(conn):
            log.info("db init: schema present, nothing to create")
            outcome = "present"
        else:
            log.info("db init: schema absent, applying %s and seeding", SCHEMA_PATH.name)
            # No parameters here on purpose: psycopg allows several statements in one
            # execute() only when nothing is bound, which is exactly what a DDL file is.
            conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
            counts = seed(conn, config, menu)
            log.info("db init: schema created, seeded %s", counts)
            outcome = "created"

        # Always, gate or no gate: each migration self-gates on its column (SPEC 1.2).
        migrated = migrations.run(conn)
        log.info("db init: migrations %s", migrated)
        return outcome


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    outcome = run()
    log.info("db init finished: %s", outcome)


if __name__ == "__main__":
    main()
