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

# The burst edition (Fase 2, T-C1): identical, plus the capture JSONB written ONCE here
# (sharpness of the winning frame, direction_dudosa when ambiguous); the worker never touches it.
INSERT_PLATE_WITH_CAPTURE = """
INSERT INTO plates (site, camera, record_type, ts, photo_path, model, prompt_version, menu_version, capture)
VALUES (%(site)s, %(camera)s, %(record_type)s, now(), %(photo_path)s, %(model)s, %(prompt_version)s, %(menu_version)s, %(capture)s)
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


# --------------------------------------------------------------------- review / stats / gallery (Fase 2, SPEC 1.3)

# The shared filter clause. Every optional filter is NULL-tolerant, so ONE statement serves every
# filter combination and no SQL is ever assembled outside this file. `site` is the SCOPED site
# (a site account is forced to its own; admin passes what it asked or NULL = all). date_to is
# inclusive: ts < date_to + 1 day. Casts make a Python None a typed NULL.
_PLATE_FILTERS = """
  (%(site)s::text IS NULL OR site = %(site)s)
  AND (%(record_type)s::text IS NULL OR record_type = %(record_type)s)
  AND (%(date_from)s::date IS NULL OR ts >= %(date_from)s::date)
  AND (%(date_to)s::date IS NULL OR ts < (%(date_to)s::date + 1))
"""

# The one-row scope: an id the session may touch, or nothing.
_SCOPE = "(%(site)s::text IS NULL OR site = %(site)s)"

# The three tab counts in one pass (the page paints Pendientes / Verificados / No-platos).
REVIEW_COUNTS = f"""
SELECT review_status, COUNT(*) AS n
FROM plates
WHERE {_PLATE_FILTERS}
GROUP BY review_status
"""

REVIEW_LIST = f"""
SELECT *
FROM plates
WHERE review_status = %(status)s AND {_PLATE_FILTERS}
ORDER BY ts DESC, id DESC
LIMIT %(limit)s OFFSET %(offset)s
"""

# The one-by-one correction: the human's dish and percentages land BESIDE the model's draft
# (leftovers stays untouched; leftovers_verified NULL = the model was right).
VERIFY_PLATE = f"""
UPDATE plates SET
  dish_verified      = %(dish_verified)s,
  leftovers_verified = %(leftovers_verified)s,
  review_status      = 'verified'
WHERE id = %(id)s AND {_SCOPE}
RETURNING *
"""

SET_REVIEW_STATUS = f"""
UPDATE plates SET review_status = %(status)s
WHERE id = %(id)s AND {_SCOPE}
RETURNING *
"""

# Bulk "correcto": the model was right on the whole selection; rows without a prediction yet
# have nothing to confirm and are skipped (the caller reports the count).
BULK_VERIFY_AS_IS = f"""
UPDATE plates SET
  dish_verified      = dish_predicted,
  leftovers_verified = NULL,
  review_status      = 'verified'
WHERE id = ANY(%(ids)s) AND dish_predicted IS NOT NULL AND {_SCOPE}
"""

BULK_SET_REVIEW_STATUS = f"""
UPDATE plates SET review_status = %(status)s
WHERE id = ANY(%(ids)s) AND {_SCOPE}
"""

# --- stats (panel): aggregates computed live, no rollup tables this era ---------------------

# Per dish and component over RETURN records: the human's truth when it exists, else the model's.
# avg_left = mean % left; return_rate = share of plates where the component came back (> 0).
STATS_COMPONENTS = f"""
SELECT
  COALESCE(dish_verified, dish_predicted) AS dish_id,
  e.key AS component,
  AVG(e.value::numeric) AS avg_left,
  100.0 * AVG(CASE WHEN e.value::numeric > 0 THEN 1 ELSE 0 END) AS return_rate,
  COUNT(*) AS n
FROM plates, jsonb_each_text(COALESCE(leftovers_verified, leftovers)) AS e
WHERE record_type = 'return'
  AND review_status <> 'not_plate'
  AND COALESCE(dish_verified, dish_predicted) IS NOT NULL
  AND COALESCE(dish_verified, dish_predicted) <> 'desconocido'
  AND {_PLATE_FILTERS}
GROUP BY 1, 2
ORDER BY 1, 2
"""

STATS_DISH_PLATES = f"""
SELECT COALESCE(dish_verified, dish_predicted) AS dish_id, COUNT(*) AS plates
FROM plates
WHERE record_type = 'return'
  AND review_status <> 'not_plate'
  AND COALESCE(dish_verified, dish_predicted) IS NOT NULL
  AND COALESCE(dish_verified, dish_predicted) <> 'desconocido'
  AND {_PLATE_FILTERS}
GROUP BY 1
ORDER BY plates DESC, dish_id
"""

SELECT_DISH_NAMES = "SELECT dish_id, nombre FROM menu_dishes"

# The live queue block (site-scoped through the plate; dates do not apply: it is NOW).
STATS_QUEUE = """
SELECT
  COUNT(*) FILTER (WHERE j.status = 'pending') AS pending,
  COUNT(*) FILTER (WHERE j.status = 'working') AS working,
  COUNT(*) FILTER (WHERE j.status = 'failed' AND j.finished_at > now() - interval '24 hours') AS failed_24h,
  COUNT(*) FILTER (WHERE j.status = 'done' AND j.finished_at > now() - interval '24 hours') AS done_24h,
  AVG(EXTRACT(EPOCH FROM (j.finished_at - p.created_at)))
    FILTER (WHERE j.status = 'done' AND j.finished_at > now() - interval '24 hours') AS avg_upload_to_done_s_24h
FROM jobs j
JOIN plates p ON p.id = j.plate_id
WHERE (%(site)s::text IS NULL OR p.site = %(site)s)
"""

# The presentation summary: graded OUTGOING rows; flagged rows (not fresh, unparseable) are counted
# apart and excluded from the average and the pass rate.
STATS_PRESENTATION = f"""
SELECT
  COUNT(*) FILTER (WHERE NOT (presentation ? 'flags')) AS graded,
  COUNT(*) FILTER (WHERE presentation ? 'flags') AS flagged,
  AVG((presentation->>'score')::numeric) FILTER (WHERE NOT (presentation ? 'flags')) AS avg_score,
  100.0 * AVG(CASE WHEN (presentation->>'pass')::boolean THEN 1 ELSE 0 END)
    FILTER (WHERE NOT (presentation ? 'flags')) AS pass_rate
FROM plates
WHERE record_type = 'outgoing'
  AND presentation IS NOT NULL
  AND review_status <> 'not_plate'
  AND {_PLATE_FILTERS}
"""

# --- gallery ---------------------------------------------------------------------------------

_GALLERY_FILTERS = f"""
  {_PLATE_FILTERS}
  AND (%(dish)s::text IS NULL OR COALESCE(dish_verified, dish_predicted) = %(dish)s)
  AND (%(review_status)s::text IS NULL OR review_status = %(review_status)s)
"""

GALLERY_COUNT = f"SELECT COUNT(*) AS n FROM plates WHERE {_GALLERY_FILTERS}"

GALLERY_LIST = f"""
SELECT id, site, camera, record_type, ts, dish_predicted, dish_verified, confidence,
       review_status, presentation, capture
FROM plates
WHERE {_GALLERY_FILTERS}
ORDER BY ts DESC, id DESC
LIMIT %(limit)s OFFSET %(offset)s
"""

# The photo stream reads the path from the ROW, never from client input (SPEC 1.3).
SELECT_PHOTO_PATH_SCOPED = f"SELECT photo_path FROM plates WHERE id = %(id)s AND {_SCOPE}"
