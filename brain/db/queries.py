"""ALL SQL of LPQ_VISION lives here (single-folder surgery rule). No SQL string anywhere else.

Every statement is a module constant; callers pass a dict of named parameters
(psycopg 3 syntax: %(name)s). connect() lives here too: the one door into the database.
Rows come back as dicts (row_factory=dict_row) so callers read columns by name.
"""

from __future__ import annotations

import os

import psycopg
from psycopg.rows import dict_row


def connect() -> psycopg.Connection:
    """Open a connection from DATABASE_URL (.env via env_file). The value is read, never logged."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set: is .env present and passed through env_file?")
    return psycopg.connect(url, row_factory=dict_row)


# --------------------------------------------------------------------- readiness / health

PING = "SELECT 1 AS ok"

# The existence gate of init (SPEC 1.8): to_regclass is NULL when the table is absent.
SCHEMA_PRESENT = "SELECT to_regclass('public.sites') IS NOT NULL AS present"

# The worker's startup wait: this raises until the schema exists, then returns.
WORKER_WAIT = "SELECT 1 AS ok FROM sites LIMIT 1"

# Health counts EVERY pending row, future run_after included: pending IS pending.
COUNT_PENDING_JOBS = "SELECT COUNT(*) AS n FROM jobs WHERE status = 'pending'"


# --------------------------------------------------------------------- migrations (Fase 2, SPEC 1.2)

# The per-column existence gate of brain/db/migrations.py: TRUE when the column already exists.
COLUMN_PRESENT = """
SELECT EXISTS (
  SELECT 1
  FROM information_schema.columns
  WHERE table_schema = 'public' AND table_name = %(table)s AND column_name = %(column)s
) AS present
"""

# Migration 1: the human's corrected percentages, beside the model's draft, never over it.
ADD_PLATES_LEFTOVERS_VERIFIED = "ALTER TABLE plates ADD COLUMN leftovers_verified JSONB"

# Migration 3: capture-time facts (sharpness, direction_dudosa), written once at insert.
ADD_PLATES_CAPTURE = "ALTER TABLE plates ADD COLUMN capture JSONB"


# --------------------------------------------------------------------- seeds (idempotent)

INSERT_SITE = """
INSERT INTO sites (site, ingestion)
VALUES (%(site)s, %(ingestion)s)
ON CONFLICT (site) DO NOTHING
"""

INSERT_MENU_DISH = """
INSERT INTO menu_dishes (dish_id, nombre, plate_type, componentes, contable, activo, menu_version)
VALUES (%(dish_id)s, %(nombre)s, %(plate_type)s, %(componentes)s, %(contable)s, %(activo)s, %(menu_version)s)
ON CONFLICT (dish_id) DO NOTHING
"""

INSERT_SITE_DISH_PHOTO = """
INSERT INTO site_dish_photos (site, dish_id, photo_path, condition)
VALUES (%(site)s, %(dish_id)s, %(photo_path)s, %(condition)s)
ON CONFLICT (site, dish_id, photo_path) DO NOTHING
"""


# --------------------------------------------------------------------- sites and menu reads

SELECT_SITE = """
SELECT site, ingestion, telegram_chat_id, active
FROM sites
WHERE site = %(site)s
"""

# The plan-of-record menu_version stamped at upload (SPEC 1.3).
MAX_MENU_VERSION = "SELECT COALESCE(MAX(menu_version), 1) AS v FROM menu_dishes"

# menu_builder reads the DATABASE, never menu.yaml (SPEC 1.5).
SELECT_ACTIVE_DISHES = """
SELECT dish_id, nombre, plate_type, componentes, contable, menu_version
FROM menu_dishes
WHERE activo
ORDER BY dish_id
"""

SELECT_SITE_DISH_PHOTOS = """
SELECT dish_id, photo_path, condition
FROM site_dish_photos
WHERE site = %(site)s
ORDER BY dish_id, photo_path
"""


# --------------------------------------------------------------------- plates

# The friendly duplicate path: same relative photo_path = same bytes, same site, same day.
SELECT_PLATE_ID_BY_PHOTO_PATH = "SELECT id FROM plates WHERE photo_path = %(photo_path)s"

# ts = now() of the server at INSERT (the single trusted clock). The NOT NULL provenance
# columns carry the plan of record; the result transaction overwrites them with the truth.
# ON CONFLICT DO NOTHING + RETURNING: a racing identical upload returns NO row, and the
# caller translates that into the normal duplicate response (SPEC 1.3).
INSERT_PLATE = """
INSERT INTO plates (site, camera, record_type, ts, photo_path, model, prompt_version, menu_version)
VALUES (%(site)s, %(camera)s, %(record_type)s, now(), %(photo_path)s, %(model)s, %(prompt_version)s, %(menu_version)s)
ON CONFLICT (photo_path) DO NOTHING
RETURNING id
"""

SELECT_PLATE = "SELECT * FROM plates WHERE id = %(id)s"

INSERT_JOB = """
INSERT INTO jobs (plate_id, kind, priority)
VALUES (%(plate_id)s, %(kind)s, %(priority)s)
RETURNING id
"""


# --------------------------------------------------------------------- the queue (SPEC 1.2)

# Step 1 of every loop pass: orphan rescue. Covers own boot, own crash, peer crash.
# orphan_timeout is a datetime.timedelta (psycopg adapts it to an interval).
RESCUE_ORPHANS = """
UPDATE jobs SET status = 'pending'
WHERE status = 'working' AND started_at < now() - %(orphan_timeout)s
"""

# Step 2: the claim, verbatim. The heart of the queue: valid for 1..N workers.
CLAIM_JOB = """
UPDATE jobs SET status='working', started_at=now(), attempts=attempts+1
WHERE id = (
  SELECT id FROM jobs
  WHERE status='pending' AND run_after <= now()
  ORDER BY priority, created_at
  FOR UPDATE SKIP LOCKED
  LIMIT 1
)
RETURNING *
"""

# Step 4, executed with MARK_JOB_DONE in ONE transaction: the result and the provenance
# ACTUALLY used are welded to the job's completion. A finished row tells the physical truth.
WRITE_ANALYZE_RESULT = """
UPDATE plates SET
  dish_predicted = %(dish_predicted)s,
  leftovers      = %(leftovers)s,
  confidence     = %(confidence)s,
  validator      = %(validator)s,
  model          = %(model)s,
  prompt_version = %(prompt_version)s,
  menu_version   = %(menu_version)s
WHERE id = %(plate_id)s
"""

# The express lane's twin (T-E5): the grade lands in its own JSONB, the dish id in dish_predicted,
# the parse repairs/flags in validator (the worker owns validator on both lanes), all welded to
# MARK_JOB_DONE in ONE transaction with the presentation prompt's own version.
WRITE_PRESENTATION_RESULT = """
UPDATE plates SET
  dish_predicted = %(dish_predicted)s,
  presentation   = %(presentation)s,
  validator      = %(validator)s,
  model          = %(model)s,
  prompt_version = %(prompt_version)s,
  menu_version   = %(menu_version)s
WHERE id = %(plate_id)s
"""

MARK_JOB_DONE = """
UPDATE jobs SET status = 'done', finished_at = now()
WHERE id = %(job_id)s
"""

# A failed attempt: the ROW waits out its backoff on the rail; the worker never sleeps.
# backoff is a datetime.timedelta computed in exactly one place (the worker loop).
REQUEUE_JOB = """
UPDATE jobs SET status = 'pending', run_after = now() + %(backoff)s, last_error = %(last_error)s
WHERE id = %(job_id)s
"""

# After the last allowed attempt: the job steps aside with its note; the rail moves on.
MARK_JOB_FAILED = """
UPDATE jobs SET status = 'failed', finished_at = now(), last_error = %(last_error)s
WHERE id = %(job_id)s
"""
