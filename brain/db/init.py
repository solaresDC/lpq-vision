"""Idempotent database init, gated on existence (SPEC 1.8), plus the migrations and the Fase-3 boot duties
on EVERY boot.

Owner: the fastapi service calls run() at startup, BEFORE serving. If the `sites` table is absent,
schema.sql runs verbatim and the seeds follow. EITHER WAY, inside the same ONE transaction:
brain.db.migrations.run() applies every missing step; bootstrap_users() imports the two temporal .env
hashes into `users` ONCE, only while that table is empty (the recovery road, inert once any account
exists); ensure_device_keys() births a gafete for every site that has none. A half-built state can
never exist, and a database that already has everything passes through in milliseconds.

What the last run did is kept in `last_report` for the api's post-reboot checklist (/api/admin/status).
Never logged: hashes, gafetes, env values. Only user names and counts.

`python -m brain.db.init` by hand is a debug tool only: the system never depends on anyone remembering it.

Seeds: `sites` from config.yaml's sites block (the one source of truth for sites) and `menu_dishes` +
`site_dish_photos` from menu.yaml, all with ON CONFLICT DO NOTHING. From Fase 3 a seeded dish is born
at the current MAX menu_version, and photo rows are seeded ONLY for dishes the same seed inserts: the
database is the menu's edited truth, and a re-seed never resurrects a row the admin detached.
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from brain.db import migrations
from brain.db import queries as q
from brain.validator.models import Config, Menu, load_config, load_menu

log = logging.getLogger("lpq.db.init")

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# A gafete: 32 random bytes, URL-safe (43 characters). Never logged, never printed by the server.
DEVICE_KEY_BYTES = 32

# The temporal .env hashes the FIRST Fase-3 boot imports while `users` is empty (SPEC 1.2 step 1):
# (user name, env key, role, site). The keys die at the ceremony; this road then stays inert forever.
ENV_BOOTSTRAP: tuple[tuple[str, str, str, str | None], ...] = (
    ("admin", "ADMIN_PASSWORD_HASH", "admin", None),
    ("demo", "DEMO_PASSWORD_HASH", "manager", "demo"),
)

# What the last run() did, for /api/admin/status. Refilled by every run().
last_report: dict[str, Any] = {}


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
    """Insert sites, NEW dishes and the reference photos of those new dishes. Returns how many rows were NEW.

    An existing dish is never touched and gets no photo row: from Fase 3 its definition and photos belong
    to the mostrador (a re-seed must never revert the admin nor resurrect a row the admin detached).
    """
    counts = {"sites": 0, "dishes": 0, "photos": 0}

    for site, site_cfg in config.sites.items():
        cur = conn.execute(q.INSERT_SITE, {"site": site, "ingestion": site_cfg.ingestion})
        counts["sites"] += cur.rowcount

    # A new dish is born at the current MAX, like CREAR PLATILLO (1 on a fresh install).
    born_at = int(conn.execute(q.MAX_MENU_VERSION).fetchone()["v"])
    new_dishes: set[str] = set()
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
                "menu_version": born_at,
            },
        )
        if cur.rowcount:
            new_dishes.add(dish_id)
    counts["dishes"] = len(new_dishes)

    for site, site_menu in menu.sites.items():
        for dish_id, fotos in site_menu.fotos_ref.items():
            if dish_id not in new_dishes:
                continue
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


def bootstrap_users(conn: psycopg.Connection) -> list[str]:
    """Import the temporal .env hashes into `users`, ONLY while the table is empty. Returns the names born.

    The hash is copied as the human typed it into .env (quotes and spaces stripped), never printed.
    A site account is imported only when its site exists (the users.site foreign key).
    """
    if int(conn.execute(q.COUNT_USERS).fetchone()["n"]) > 0:
        return []
    born: list[str] = []
    for usuario, env_key, role, site in ENV_BOOTSTRAP:
        value = os.environ.get(env_key, "").strip().strip("'\"")
        if not value:
            continue
        if site is not None and conn.execute(q.SELECT_SITE, {"site": site}).fetchone() is None:
            log.warning("users bootstrap: %s skipped, its site %s does not exist", usuario, site)
            continue
        conn.execute(
            q.INSERT_USER,
            {"usuario": usuario, "password_hash": value, "role": role, "site": site},
        )
        born.append(usuario)
    if born:
        conn.execute(
            q.INSERT_ADMIN_LOG,
            {"usuario": None, "action": "users.bootstrap", "detail": Jsonb({"usuarios": born})},
        )
        log.info("users bootstrapped from the temporal .env keys (once): %s", born)
    return born


def ensure_device_keys(conn: psycopg.Connection) -> int:
    """Birth a gafete for every site that has none. Returns how many were born; the values are never logged."""
    rows = conn.execute(q.SELECT_SITES_WITHOUT_KEY).fetchall()
    for row in rows:
        conn.execute(q.SET_DEVICE_KEY, {"site": row["site"], "device_key": secrets.token_urlsafe(DEVICE_KEY_BYTES)})
    return len(rows)


def run() -> str:
    """Gate -> (schema + seeds) or nothing, THEN migrations, bootstrap and gafetes, all in one transaction.

    Returns 'created' or 'present' (the schema gate's verdict); the rest goes to the log and to last_report.
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

        # Always, gate or no gate: each migration self-gates on its table or column (SPEC 1.2).
        migrated = migrations.run(conn)
        log.info("db init: migrations %s", migrated)
        born = bootstrap_users(conn)
        keys = ensure_device_keys(conn)
        if keys:
            log.info("db init: %d gafete(s) born for sites that had none", keys)

    last_report.clear()
    last_report.update(
        {
            "schema": outcome,
            "migrations": migrated,
            "users_bootstrapped": born,
            "device_keys_born": keys,
            "at": datetime.now(timezone.utc).isoformat(),
        }
    )
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
