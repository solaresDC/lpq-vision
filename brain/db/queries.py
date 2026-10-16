"""ALL SQL of LPQ_VISION lives here (single-folder surgery rule). No SQL string anywhere else.

Every statement is a module constant; callers pass a dict of named parameters
(psycopg 3 syntax: %(name)s). connect() lives here too: the one door into the database.
Rows come back as dicts (row_factory=dict_row) so callers read columns by name.

Fase 3 adds every statement of the admin era at once: the migrations' table steps, accounts,
the bitácora, the one-day hour changes (Section 3.13.1; the one-night extension was the first kind),
the gafete, the camera links and the phones (Section 3.13.2), the papelera, the usage write and the
spend dashboard, menu editing and reference photos, and the bot's new cursors. The shared plate filter
now leaves 'discarded' rows out of review, stats and gallery: only the papelera lists them.
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


# --------------------------------------------------------------------- migrations (Fase 3, SPEC 1.2)

# The per-table existence gate: qualified is 'public.<table>'.
TABLE_PRESENT = "SELECT to_regclass(%(qualified)s) IS NOT NULL AS present"

# Accounts in the database. The reserved 'cuarto' row (role machine) IS the machine floor.
CREATE_USERS_TABLE = """
CREATE TABLE users (
  usuario       TEXT PRIMARY KEY,
  password_hash TEXT NOT NULL,
  role          TEXT NOT NULL CHECK (role IN ('admin', 'manager', 'machine')),
  site          TEXT REFERENCES sites(site),
  active        BOOLEAN NOT NULL DEFAULT TRUE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

# The internal bitácora. usuario NULL = the system acted; the name never reaches a public message.
CREATE_ADMIN_LOG_TABLE = """
CREATE TABLE admin_log (
  id      BIGSERIAL PRIMARY KEY,
  ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
  usuario TEXT,
  action  TEXT NOT NULL,
  detail  JSONB
)
"""

# The bot's hours table, born in 3.11 for the one-night extension (until = tonight's new close). From 3.13.1
# it holds every one-day hour change, /extender's included: each new row is the WHOLE new state of one
# service day (dia, abre_at, until = its close, cerrado), and the latest row of a day wins. The table keeps
# its first name; rows from before 3.13.1 have no dia and are ignored (only test rows on the laptop).
CREATE_HORARIO_EXTENSIONES_TABLE = """
CREATE TABLE horario_extensiones (
  id         BIGSERIAL PRIMARY KEY,
  site       TEXT NOT NULL REFERENCES sites(site),
  until      TIMESTAMPTZ NOT NULL,
  chat_id    TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

ADD_PLATES_DISCARDED_AT = "ALTER TABLE plates ADD COLUMN discarded_at TIMESTAMPTZ"
ADD_JOBS_USAGE = "ALTER TABLE jobs ADD COLUMN usage JSONB"
ADD_SITES_DEVICE_KEY = "ALTER TABLE sites ADD COLUMN device_key TEXT"

# Section 3.13.1: the one-day hour changes. dia = the service day a row changes; abre_at = its opening
# (NULL when cerrado); cerrado = closed all day (its until is then that day's regular close, for the sweep).
ADD_HORARIO_DIA = "ALTER TABLE horario_extensiones ADD COLUMN dia DATE"
ADD_HORARIO_ABRE_AT = "ALTER TABLE horario_extensiones ADD COLUMN abre_at TIMESTAMPTZ"
ADD_HORARIO_CERRADO = "ALTER TABLE horario_extensiones ADD COLUMN cerrado BOOLEAN NOT NULL DEFAULT false"

# Section 3.14: deleting a plate (the papelera's purge, Eliminar ya) makes the foreign key look for its jobs; without
# this index each deleted plate reads the whole jobs table.
CREATE_JOBS_PLATE_INDEX = "CREATE INDEX idx_jobs_plate ON jobs (plate_id)"

# Section 3.14: the gasto reads the analyzed jobs of the last 7, 30 or 90 days; this index keeps that read the size
# of the range, not of the whole history.
CREATE_JOBS_DONE_FINISHED_INDEX = "CREATE INDEX idx_jobs_done_finished ON jobs (finished_at) WHERE status = 'done'"

# Section 3.13.2: the camera links. One live link per camera; its code only lets a phone ASK to be that camera (the
# admin's Aceptar decides). dias NULL and vence_at NULL = permanente.
CREATE_CAMARA_ENLACES_TABLE = """
CREATE TABLE camara_enlaces (
  site      TEXT NOT NULL REFERENCES sites(site),
  camera    TEXT NOT NULL,
  codigo    TEXT NOT NULL UNIQUE,
  dias      INT CHECK (dias IS NULL OR dias > 0),
  vence_at  TIMESTAMPTZ,
  creado_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (site, camera)
)
"""

# Section 3.13.2: one row per phone while it captures a camera or waits for the admin. pase = the sha256 of the
# phone's cookie (the cookie itself is never stored); site and camera = the camera it captures, ONE phone per camera
# (UNIQUE; empty pairs never collide); pide_* = its one request waiting for the admin.
CREATE_TELEFONOS_TABLE = """
CREATE TABLE telefonos (
  id          BIGSERIAL PRIMARY KEY,
  pase        TEXT NOT NULL UNIQUE,
  nombre      TEXT NOT NULL,
  site        TEXT REFERENCES sites(site),
  camera      TEXT,
  pide_site   TEXT REFERENCES sites(site),
  pide_camera TEXT,
  pide_at     TIMESTAMPTZ,
  creado_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (site, camera),
  CHECK ((site IS NULL) = (camera IS NULL)),
  CHECK ((pide_site IS NULL) = (pide_camera IS NULL) AND (pide_site IS NULL) = (pide_at IS NULL)),
  CHECK (site IS NOT NULL OR pide_site IS NOT NULL)
)
"""

# Owner ruling 3.9.2: accounts can be deleted, recoverable for USER_RECOVERY_DAYS. uid is the permanent
# account number: born with the row (existing rows get theirs at this migration), never reused.
ADD_USERS_UID = "ALTER TABLE users ADD COLUMN uid BIGSERIAL UNIQUE"
ADD_USERS_DELETED_AT = "ALTER TABLE users ADD COLUMN deleted_at TIMESTAMPTZ"
ADD_USERS_ERASE_WARNED_AT = "ALTER TABLE users ADD COLUMN erase_warned_at TIMESTAMPTZ"
ADD_ADMIN_LOG_USUARIO_UID = "ALTER TABLE admin_log ADD COLUMN usuario_uid BIGINT"

# 3.9.4: nobody can SEE a password (only its hash exists); the admin sees WHEN it last changed and WHO changed it.
ADD_USERS_PASSWORD_CHANGED_AT = "ALTER TABLE users ADD COLUMN password_changed_at TIMESTAMPTZ"
ADD_USERS_PASSWORD_CHANGED_BY = "ALTER TABLE users ADD COLUMN password_changed_by TEXT"

# 3.10.1 (owner ruling): the UNIVERSAL reference photos, one set per dish for every site without local ones.
CREATE_BRAND_DISH_PHOTOS_TABLE = """
CREATE TABLE brand_dish_photos (
  dish_id    TEXT NOT NULL REFERENCES menu_dishes(dish_id),
  photo_path TEXT NOT NULL,
  condition  TEXT NOT NULL DEFAULT 'normal',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (dish_id, photo_path)
)
"""

# 3.10.2 (owner ruling): the recetario book. orden = the dish's page (NULL = never placed: last, by name); notas =
# "notas para el modelo" (NULL = never written, '' = cleared by a person); dish_name_history = the names a dish had
# before (its clave never changes).
ADD_MENU_DISHES_ORDEN = "ALTER TABLE menu_dishes ADD COLUMN orden INT"
ADD_MENU_DISHES_NOTAS = "ALTER TABLE menu_dishes ADD COLUMN notas TEXT"
CREATE_DISH_NAME_HISTORY_TABLE = """
CREATE TABLE dish_name_history (
  id          BIGSERIAL PRIMARY KEY,
  dish_id     TEXT NOT NULL REFERENCES menu_dishes(dish_id),
  nombre      TEXT NOT NULL,
  hasta       TIMESTAMPTZ NOT NULL DEFAULT now(),
  usuario     TEXT,
  usuario_uid BIGINT
)
"""


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

# The plan-of-record menu_version stamped at upload (SPEC 1.3) and, from Fase 3, the version clock
# menu_builder stamps: MAX over ALL dishes, never rewinding; 1 on an empty table.
MAX_MENU_VERSION = "SELECT COALESCE(MAX(menu_version), 1) AS v FROM menu_dishes"

# menu_builder reads the DATABASE, never menu.yaml (SPEC 1.5); from 3.10.2 each dish's notas travel with it.
SELECT_ACTIVE_DISHES = """
SELECT dish_id, nombre, plate_type, componentes, contable, menu_version, notas
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

# The universal level (menu_builder's fallback and the mostrador's list): every dish's brand photos.
SELECT_BRAND_DISH_PHOTOS = """
SELECT dish_id, photo_path, condition
FROM brand_dish_photos
ORDER BY dish_id, photo_path
"""


# --------------------------------------------------------------------- sites, destinations and the gafete (Fase 3)

SELECT_SITES_ALL = "SELECT site, ingestion, telegram_chat_id, active FROM sites ORDER BY site"

# The destinations map's input (api and bot): every active site with a chat.
SELECT_SITE_CHATS = """
SELECT site, telegram_chat_id
FROM sites
WHERE active AND telegram_chat_id IS NOT NULL
ORDER BY site
"""

# /foto, /cola and /extender resolve the site from the group that asked.
SELECT_SITES_BY_CHAT = "SELECT site FROM sites WHERE active AND telegram_chat_id = %(chat_id)s ORDER BY site"

# chat_id NULL removes the destination (absent-when-off).
SET_SITE_CHAT = "UPDATE sites SET telegram_chat_id = %(chat_id)s WHERE site = %(site)s RETURNING site"

EDIT_SITE = """
UPDATE sites SET
  ingestion = COALESCE(%(ingestion)s::text, ingestion),
  active    = COALESCE(%(active)s::boolean, active)
WHERE site = %(site)s
RETURNING site, ingestion, active
"""

# Crear restaurante: the site is born WITH its gafete, inside the same all-or-nothing transaction.
INSERT_SITE_FULL = """
INSERT INTO sites (site, ingestion, device_key)
VALUES (%(site)s, %(ingestion)s, %(device_key)s)
"""

# The gafete's reads and writes. The value is compared in constant time by the caller, never logged.
SELECT_DEVICE_KEY = "SELECT device_key FROM sites WHERE site = %(site)s AND active"
SELECT_SITE_KEYS = "SELECT site, device_key FROM sites WHERE active AND device_key IS NOT NULL ORDER BY site"
SELECT_SITES_WITHOUT_KEY = "SELECT site FROM sites WHERE device_key IS NULL ORDER BY site"
SET_DEVICE_KEY = "UPDATE sites SET device_key = %(device_key)s WHERE site = %(site)s"


# --------------------------------------------------------------------- accounts (Fase 3, SPEC 1.3)

COUNT_USERS = "SELECT COUNT(*) AS n FROM users"

SELECT_USER = """
SELECT usuario, password_hash, role, site, active, uid, deleted_at
FROM users
WHERE usuario = %(usuario)s
"""

# The users CRUD never lists the reserved machine row, nor the deleted accounts (they have their own list).
LIST_USERS = """
SELECT usuario, uid, role, site, active, created_at, password_changed_at, password_changed_by
FROM users
WHERE role <> 'machine' AND deleted_at IS NULL
ORDER BY usuario
"""

# "Eliminadas": recoverable until deleted_at + USER_RECOVERY_DAYS.
LIST_DELETED_USERS = """
SELECT usuario, uid, role, site, deleted_at
FROM users
WHERE role <> 'machine' AND deleted_at IS NOT NULL
ORDER BY deleted_at
"""

INSERT_USER = """
INSERT INTO users (usuario, password_hash, role, site)
VALUES (%(usuario)s, %(password_hash)s, %(role)s, %(site)s)
RETURNING uid
"""

# The mostrador's reset: never the machine row.
RESET_USER_PASSWORD = """
UPDATE users SET password_hash = %(password_hash)s, password_changed_at = now(), password_changed_by = %(changed_by)s
WHERE usuario = %(usuario)s AND role <> 'machine'
RETURNING usuario
"""

# Mi cuenta: a person changes its OWN password (the api checked the current one first).
SET_OWN_PASSWORD = """
UPDATE users SET password_hash = %(password_hash)s, password_changed_at = now(), password_changed_by = %(usuario)s
WHERE usuario = %(usuario)s AND active AND deleted_at IS NULL AND role <> 'machine'
RETURNING uid
"""

# set_password over ssh: writes the hash AND reactivates (the owner can always get back in).
SET_PASSWORD_AND_REACTIVATE = """
UPDATE users SET password_hash = %(password_hash)s, active = TRUE, deleted_at = NULL, erase_warned_at = NULL,
  password_changed_at = now(), password_changed_by = 'set_password'
WHERE usuario = %(usuario)s
RETURNING usuario, role
"""

# The two reserved names set_password may BIRTH (admin, cuarto): upsert, reactivated.
UPSERT_RESERVED_USER = """
INSERT INTO users (usuario, password_hash, role, site, active)
VALUES (%(usuario)s, %(password_hash)s, %(role)s, NULL, TRUE)
ON CONFLICT (usuario) DO UPDATE SET password_hash = EXCLUDED.password_hash, active = TRUE, deleted_at = NULL,
  erase_warned_at = NULL, password_changed_at = now(), password_changed_by = 'set_password'
RETURNING usuario, role
"""

# Floor 1 may only BIRTH the cuarto row while it does not exist: no row back = it already existed.
BIRTH_MACHINE_USER = """
INSERT INTO users (usuario, password_hash, role, site, active)
VALUES (%(usuario)s, %(password_hash)s, 'machine', NULL, TRUE)
ON CONFLICT (usuario) DO NOTHING
RETURNING usuario
"""

# The machine room changes the cuarto password (Section 3.15: you are already inside the wall). Only the active
# machine row; no row back = there is no cuarto to change.
SET_MACHINE_PASSWORD = """
UPDATE users SET password_hash = %(password_hash)s, password_changed_at = now(), password_changed_by = %(changed_by)s
WHERE usuario = %(usuario)s AND role = 'machine' AND active
RETURNING usuario
"""

SET_USER_ACTIVE = """
UPDATE users SET active = %(active)s
WHERE usuario = %(usuario)s AND role <> 'machine'
RETURNING usuario
"""

SET_USER_ROLE = """
UPDATE users SET role = %(role)s, site = %(site)s
WHERE usuario = %(usuario)s AND role <> 'machine'
RETURNING usuario
"""

# The last active admin can never be deactivated or demoted: the caller checks this count first.
COUNT_ACTIVE_ADMINS = "SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND active"

# Delete: the account closes at once (a deleted account is inactive, so login and the admin count skip it).
SOFT_DELETE_USER = """
UPDATE users SET deleted_at = now(), active = FALSE, erase_warned_at = NULL
WHERE usuario = %(usuario)s AND role <> 'machine' AND deleted_at IS NULL
RETURNING usuario, uid, deleted_at
"""

# Recuperar: back to an active account.
RECOVER_USER = """
UPDATE users SET deleted_at = NULL, erase_warned_at = NULL, active = TRUE
WHERE usuario = %(usuario)s AND role <> 'machine' AND deleted_at IS NOT NULL
RETURNING usuario, uid
"""

# The worker's sweep. warn_after and retention are timedeltas; SKIP LOCKED keeps replicas off the same rows.
USERS_TO_WARN = """
SELECT usuario, uid, deleted_at
FROM users
WHERE deleted_at IS NOT NULL AND erase_warned_at IS NULL AND role <> 'machine'
  AND deleted_at < now() - %(warn_after)s AND deleted_at >= now() - %(retention)s
ORDER BY deleted_at
FOR UPDATE SKIP LOCKED
"""

MARK_USER_WARNED = "UPDATE users SET erase_warned_at = now() WHERE usuario = %(usuario)s"

ERASE_DELETED_USERS = """
DELETE FROM users
WHERE deleted_at IS NOT NULL AND deleted_at < now() - %(retention)s AND role <> 'machine'
RETURNING usuario, uid
"""


# --------------------------------------------------------------------- the bitácora (Fase 3)

INSERT_ADMIN_LOG = """
INSERT INTO admin_log (usuario, action, detail)
VALUES (%(usuario)s, %(action)s, %(detail)s)
RETURNING id
"""

# A person's action: the name AND the permanent account number (system rows keep INSERT_ADMIN_LOG).
INSERT_ADMIN_LOG_BY = """
INSERT INTO admin_log (usuario, usuario_uid, action, detail)
VALUES (%(usuario)s, %(usuario_uid)s, %(action)s, %(detail)s)
RETURNING id
"""

# The password history (3.9.5): every bitácora row that SET a password, newest first. The hash is never here;
# only who, whose and when. set_password over ssh writes no row (it shows in users.password_changed_*).
PASSWORD_HISTORY = """
SELECT ts, usuario, usuario_uid, action, detail
FROM admin_log
WHERE action IN ('user.create', 'site.create', 'user.password', 'user.password_self')
ORDER BY ts DESC, id DESC
LIMIT %(limit)s
"""

# One account's password history, by its permanent number (compared as text: detail is JSONB).
PASSWORD_HISTORY_OF = """
SELECT ts, usuario, usuario_uid, action, detail
FROM admin_log
WHERE (action IN ('user.create', 'user.password', 'user.password_self') AND detail->>'uid' = %(uid)s)
   OR (action = 'site.create' AND detail->>'manager_uid' = %(uid)s)
ORDER BY ts DESC, id DESC
LIMIT %(limit)s
"""

ADMIN_LOG_RECENT = """
SELECT id, ts, usuario, action, detail
FROM admin_log
ORDER BY id DESC
LIMIT %(limit)s
"""

# The machine room's reboot (Section 3.15): when the butler last took one (its row is written right after its "ok", or
# when it took the line without a readable answer: it may have rebooted, so its 10 minutes count).
LAST_REBOOT_AT = "SELECT MAX(ts) AS ts FROM admin_log WHERE action = 'reboot'"

# The bot's read-only cursor: the action and its detail, NEVER the name.
ADMIN_LOG_MAX_ID = "SELECT COALESCE(MAX(id), 0) AS id FROM admin_log"

ADMIN_LOG_AFTER = """
SELECT id, ts, action, detail
FROM admin_log
WHERE id > %(after_id)s
ORDER BY id
"""

# The bot's start (Section 3.13.2): the rows of its lookback window already there, which it never announces; a row
# of that window whose transaction commits later is announced.
ADMIN_LOG_BETWEEN = """
SELECT id, ts, action, detail
FROM admin_log
WHERE id > %(after_id)s AND id <= %(upto)s
ORDER BY id
"""


# --------------------------------------------------------------------- the one-day hour changes (Fase 3)

# One change = the whole new state of one service day. The bot writes it in the same transaction as its
# bitácora row (INSERT_ADMIN_LOG).
INSERT_HORARIO_CAMBIO = """
INSERT INTO horario_extensiones (site, dia, abre_at, until, cerrado, chat_id)
VALUES (%(site)s, %(dia)s, %(abre_at)s, %(until)s, %(cerrado)s, %(chat_id)s)
RETURNING id
"""

# The changes of the service days that can still matter (window: a timedelta): every row of each day from
# the window's start on, whole days only, since the latest row of a day is that day's state. Rows without
# dia (written before 3.13.1) are left out.
SELECT_RECENT_HORARIO = """
SELECT id, site, dia, abre_at, until, cerrado, created_at
FROM horario_extensiones
WHERE dia >= (now() - %(window)s)::date
ORDER BY site, created_at, id
"""

# The worker's sweep (grace: a timedelta). A day's rows leave together, once every one of them closed more
# than the grace ago: deleting a day's latest row alone would bring back an older state of that day. Rows
# without dia (before 3.13.1) leave one by one, as always.
PURGE_OLD_EXTENSIONS = """
DELETE FROM horario_extensiones h
WHERE h.until < now() - %(grace)s
  AND NOT EXISTS (
    SELECT 1 FROM horario_extensiones o
    WHERE o.site = h.site AND o.dia = h.dia AND o.until >= now() - %(grace)s
  )
"""


# --------------------------------------------------------------------- camera links and phones (Section 3.13.2)

# Every link with its state now (the mostrador's rows).
SELECT_ENLACES_ALL = """
SELECT site, camera, codigo, dias, vence_at, (vence_at IS NOT NULL AND vence_at <= now()) AS vencido
FROM camara_enlaces
ORDER BY site, camera
"""

LOCK_ENLACE = """
SELECT site, camera, codigo, dias, vence_at, (vence_at IS NOT NULL AND vence_at <= now()) AS vencido
FROM camara_enlaces
WHERE site = %(site)s AND camera = %(camera)s
FOR UPDATE
"""

# What a phone opened: the link behind a code, with its site's state.
SELECT_ENLACE_BY_CODIGO = """
SELECT e.site, e.camera, (e.vence_at IS NOT NULL AND e.vence_at <= now()) AS vencido, s.active
FROM camara_enlaces AS e
JOIN sites AS s ON s.site = e.site
WHERE e.codigo = %(codigo)s
"""

# Dame el enlace for a camera with no link yet: a link that someone else created a moment before wins (no row back).
# vence = noon of its last day on the site's clock, computed by the api (Section 3.17.1); NULL = permanente.
INSERT_ENLACE = """
INSERT INTO camara_enlaces (site, camera, codigo, dias, vence_at)
VALUES (%(site)s, %(camera)s, %(codigo)s, %(dias)s, %(vence)s)
ON CONFLICT (site, camera) DO NOTHING
RETURNING site, camera, codigo, dias, vence_at, false AS vencido
"""

# A new code for a camera (Dame el enlace when it expired; Cambiar enlace): the old code stops opening at once, and
# the phones already inside stay. dias NULL = permanente.
UPSERT_ENLACE = """
INSERT INTO camara_enlaces (site, camera, codigo, dias, vence_at)
VALUES (%(site)s, %(camera)s, %(codigo)s, %(dias)s, %(vence)s)
ON CONFLICT (site, camera) DO UPDATE
SET codigo = EXCLUDED.codigo, dias = EXCLUDED.dias, vence_at = EXCLUDED.vence_at, creado_at = now()
RETURNING site, camera, codigo, dias, vence_at, false AS vencido
"""

# The admin's duración: the same code, its last day counted again from the site's today.
SET_ENLACE_DIAS = """
UPDATE camara_enlaces
SET dias = %(dias)s, vence_at = %(vence)s
WHERE site = %(site)s AND camera = %(camera)s
RETURNING site, camera, codigo, dias, vence_at, false AS vencido
"""

DELETE_ENLACE = "DELETE FROM camara_enlaces WHERE site = %(site)s AND camera = %(camera)s"

# The phone behind a pass (the sha256 of its cookie), with whether its camera's site and its request's site are active.
SELECT_TELEFONO_BY_PASE = """
SELECT t.id, t.nombre, t.site, t.camera, t.pide_site, t.pide_camera, t.pide_at,
       COALESCE(s.active, false) AS site_active, COALESCE(p.active, false) AS pide_active
FROM telefonos AS t
LEFT JOIN sites AS s ON s.site = t.site
LEFT JOIN sites AS p ON p.site = t.pide_site
WHERE t.pase = %(pase)s
"""

LOCK_TELEFONO_BY_PASE = """
SELECT id, pase, nombre, site, camera, pide_site, pide_camera, pide_at
FROM telefonos
WHERE pase = %(pase)s
FOR UPDATE
"""

SELECT_TELEFONO = "SELECT id, nombre, site, camera, pide_site, pide_camera, pide_at FROM telefonos WHERE id = %(id)s"

LOCK_TELEFONO = """
SELECT id, pase, nombre, site, camera, pide_site, pide_camera, pide_at
FROM telefonos
WHERE id = %(id)s
FOR UPDATE
"""

# The phone that captures a camera (one at most).
SELECT_TELEFONO_DE_CAMARA = "SELECT id, nombre FROM telefonos WHERE site = %(site)s AND camera = %(camera)s"

# The other phones waiting for one camera (id -1 = count them all).
COUNT_PEDIDOS_DE_CAMARA = """
SELECT COUNT(*) AS n FROM telefonos WHERE pide_site = %(site)s AND pide_camera = %(camera)s AND id <> %(id)s
"""

# One camera's phones change one writer at a time: a request (pedir), Aceptar, Quitar la cámara and Relevo a Pi take
# this lock first, until their transaction ends. So a request never lands on a camera that just got its phone or was
# removed, and the wait list keeps its limit. Always before any telefonos row lock (one order: no deadlock).
LOCK_CAMARA = "SELECT pg_advisory_xact_lock(hashtextextended(%(site)s::text || '/' || %(camera)s::text, 3132))"

# Every phone of one camera, the one capturing it and the ones waiting for it, locked in one order (by id).
LOCK_TELEFONOS_DE_CAMARA = """
SELECT id, pase, nombre, site, camera, pide_site, pide_camera, pide_at
FROM telefonos
WHERE (site = %(site)s AND camera = %(camera)s) OR (pide_site = %(site)s AND pide_camera = %(camera)s)
ORDER BY id
FOR UPDATE
"""

# Every phone (the mostrador's rows): who captures each camera and who waits for it, oldest request first.
SELECT_TELEFONOS_ALL = """
SELECT id, nombre, site, camera, pide_site, pide_camera, pide_at
FROM telefonos
ORDER BY pide_at NULLS FIRST, id
"""

INSERT_TELEFONO_PIDE = """
INSERT INTO telefonos (pase, nombre, pide_site, pide_camera, pide_at)
VALUES (%(pase)s, %(nombre)s, %(site)s, %(camera)s, now())
RETURNING id
"""

SET_TELEFONO_PIDE = """
UPDATE telefonos SET nombre = %(nombre)s, pide_site = %(site)s, pide_camera = %(camera)s, pide_at = now()
WHERE id = %(id)s
"""

SET_TELEFONO_NOMBRE = "UPDATE telefonos SET nombre = %(nombre)s WHERE id = %(id)s"

# Aceptar: the phone becomes that camera (its previous camera, if any, is free from this moment).
BIND_TELEFONO = """
UPDATE telefonos SET site = %(site)s, camera = %(camera)s, pide_site = NULL, pide_camera = NULL, pide_at = NULL
WHERE id = %(id)s
"""

CLEAR_TELEFONO_PIDE = "UPDATE telefonos SET pide_site = NULL, pide_camera = NULL, pide_at = NULL WHERE id = %(id)s"
UNBIND_TELEFONO = "UPDATE telefonos SET site = NULL, camera = NULL WHERE id = %(id)s"
DELETE_TELEFONO = "DELETE FROM telefonos WHERE id = %(id)s"


# --------------------------------------------------------------------- menu editing and reference photos (Fase 3)

SELECT_ALL_DISHES = """
SELECT dish_id, nombre, plate_type, componentes, contable, activo, menu_version
FROM menu_dishes
ORDER BY dish_id
"""

SELECT_DISH = """
SELECT dish_id, nombre, plate_type, componentes, contable, activo, menu_version
FROM menu_dishes
WHERE dish_id = %(dish_id)s
"""

# A definition edit; menu_version is the caller's decision (global MAX+1 only when the SET of
# component names changed, else unchanged).
UPDATE_DISH_DEFINITION = """
UPDATE menu_dishes SET
  nombre       = %(nombre)s,
  plate_type   = %(plate_type)s,
  componentes  = %(componentes)s,
  menu_version = %(menu_version)s
WHERE dish_id = %(dish_id)s
RETURNING dish_id
"""

# A toggle never bumps the version.
SET_DISH_ACTIVE = "UPDATE menu_dishes SET activo = %(activo)s WHERE dish_id = %(dish_id)s RETURNING dish_id"

SELECT_ALL_SITE_DISH_PHOTOS = """
SELECT site, dish_id, photo_path, condition
FROM site_dish_photos
ORDER BY site, dish_id, photo_path
"""

SELECT_DISH_PHOTOS_OF_SITE = """
SELECT photo_path, condition
FROM site_dish_photos
WHERE site = %(site)s AND dish_id = %(dish_id)s
ORDER BY photo_path
"""

# Delete = detach ONE site's row; the files die only when no row references them (copy-on-diverge).
DELETE_SITE_DISH_PHOTO = """
DELETE FROM site_dish_photos
WHERE site = %(site)s AND dish_id = %(dish_id)s AND photo_path = %(photo_path)s
"""

# Replace = repoint ONE site's row to the new pair; a shared file is never touched.
REPOINT_SITE_DISH_PHOTO = """
UPDATE site_dish_photos SET photo_path = %(new_path)s, condition = %(condition)s
WHERE site = %(site)s AND dish_id = %(dish_id)s AND photo_path = %(photo_path)s
"""

# A light copy's references across BOTH levels: its pair dies only when this is 0.
COUNT_PHOTO_REFS = """
SELECT (SELECT COUNT(*) FROM site_dish_photos WHERE photo_path = %(photo_path)s)
     + (SELECT COUNT(*) FROM brand_dish_photos WHERE photo_path = %(photo_path)s) AS n
"""

# Crear restaurante's menu clone: the new site's rows point at the SAME files as the closest site.
CLONE_SITE_PHOTOS = """
INSERT INTO site_dish_photos (site, dish_id, photo_path, condition)
SELECT %(site)s, dish_id, photo_path, condition
FROM site_dish_photos
WHERE site = %(from_site)s
ON CONFLICT DO NOTHING
"""

INSERT_BRAND_DISH_PHOTO = """
INSERT INTO brand_dish_photos (dish_id, photo_path, condition)
VALUES (%(dish_id)s, %(photo_path)s, %(condition)s)
ON CONFLICT (dish_id, photo_path) DO NOTHING
"""

DELETE_BRAND_DISH_PHOTO = "DELETE FROM brand_dish_photos WHERE dish_id = %(dish_id)s AND photo_path = %(photo_path)s"

REPOINT_BRAND_DISH_PHOTO = """
UPDATE brand_dish_photos SET photo_path = %(new_path)s, condition = %(condition)s
WHERE dish_id = %(dish_id)s AND photo_path = %(photo_path)s
"""

# The recetario book (owner ruling 3.10.2): every dish in page order; a dish never placed goes last, by name.
SELECT_MENU_BOOK = """
SELECT dish_id, nombre, plate_type, componentes, activo, menu_version, orden, notas
FROM menu_dishes
ORDER BY orden NULLS LAST, nombre, dish_id
"""

SELECT_DISH_BOOK = """
SELECT dish_id, nombre, plate_type, componentes, activo, menu_version, orden, notas
FROM menu_dishes
WHERE dish_id = %(dish_id)s
"""

# Before a new page goes last: every dish takes its current page number 1..N, in the book's order.
NUMBER_MENU_PAGES = """
UPDATE menu_dishes AS m SET orden = r.n
FROM (SELECT dish_id, row_number() OVER (ORDER BY orden NULLS LAST, nombre, dish_id) AS n FROM menu_dishes) AS r
WHERE m.dish_id = r.dish_id AND m.orden IS DISTINCT FROM r.n
"""

NEXT_MENU_PAGE = "SELECT COALESCE(MAX(orden), 0) + 1 AS n FROM menu_dishes"

SET_DISH_ORDEN = "UPDATE menu_dishes SET orden = %(orden)s WHERE dish_id = %(dish_id)s"

SET_DISH_NOTAS = "UPDATE menu_dishes SET notas = %(notas)s WHERE dish_id = %(dish_id)s"

# scripts/notas_from_menu.py writes only where nobody ever wrote (NULL), checked again at write time.
SET_DISH_NOTAS_IF_NULL = "UPDATE menu_dishes SET notas = %(notas)s WHERE dish_id = %(dish_id)s AND notas IS NULL"

SELECT_DISHES_WITHOUT_NOTAS = "SELECT dish_id FROM menu_dishes WHERE notas IS NULL ORDER BY dish_id"

INSERT_DISH_NAME_HISTORY = """
INSERT INTO dish_name_history (dish_id, nombre, usuario, usuario_uid)
VALUES (%(dish_id)s, %(nombre)s, %(usuario)s, %(usuario_uid)s)
"""

SELECT_DISH_NAME_HISTORY = """
SELECT dish_id, nombre, hasta
FROM dish_name_history
ORDER BY dish_id, hasta DESC, id DESC
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

# Step 4, executed with the completion in ONE transaction: the result and the provenance
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
# the completion in ONE transaction with the presentation prompt's own version.
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

# The Fase-2 completion (no usage): kept byte-compatible for the Fase-2 worker path.
MARK_JOB_DONE = """
UPDATE jobs SET status = 'done', finished_at = now()
WHERE id = %(job_id)s
"""

# The Fase-3 completion: the job's token usage lands in the SAME transaction as the result.
MARK_JOB_DONE_WITH_USAGE = """
UPDATE jobs SET status = 'done', finished_at = now(), usage = %(usage)s
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

# Reintentar (Fase 3): failed -> pending with attempts reset, the whole scope at once (site NULL =
# every site: admin only; a manager passes its own). A discarded plate is never retried: the plates with a
# failed job are read FOR SHARE (Section 3.14), so a Descartar committing meanwhile is waited for and seen.
RETRY_FAILED = """
UPDATE jobs AS j SET
  status = 'pending', attempts = 0, run_after = now(), last_error = NULL,
  started_at = NULL, finished_at = NULL
FROM plates AS p
WHERE p.id = j.plate_id
  AND j.status = 'failed'
  AND j.plate_id IN (
    SELECT pl.id FROM plates AS pl
    WHERE pl.id IN (SELECT plate_id FROM jobs WHERE status = 'failed')
      AND pl.review_status <> 'discarded'
      AND (%(site)s::text IS NULL OR pl.site = %(site)s)
    FOR SHARE OF pl
  )
RETURNING j.id, p.site
"""

# The post-reboot checklist's queue depths.
QUEUE_DEPTHS = "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status ORDER BY status"

# The mostrador's Cola (Section 3.14), scoped through the plate (site NULL = every site), read every 5 s while the
# tab is open: only jobs not done are read (the claim index serves the status). Fallidos counts what Reintentar
# would take: a failed job whose plate sits in the papelera is never retried, so it is not counted. A plate
# discarded while its analysis waits is still analyzed (the claim is the queue's verbatim one): Esperando counts it.
COLA_COUNTS = """
SELECT
  COUNT(*) FILTER (WHERE j.status = 'pending') AS esperando,
  COUNT(*) FILTER (WHERE j.status = 'working') AS en_proceso,
  COUNT(*) FILTER (WHERE j.status = 'failed' AND p.review_status <> 'discarded') AS fallidos
FROM jobs AS j
JOIN plates AS p ON p.id = j.plate_id
WHERE j.status IN ('pending', 'working', 'failed')
  AND (%(site)s::text IS NULL OR p.site = %(site)s)
"""

COLA_FAILED_LIST = """
SELECT j.id AS job, j.plate_id AS plate, j.kind, j.attempts, j.last_error, j.finished_at,
       p.site, p.camera, p.record_type
FROM jobs AS j
JOIN plates AS p ON p.id = j.plate_id
WHERE j.status = 'failed' AND p.review_status <> 'discarded'
  AND (%(site)s::text IS NULL OR p.site = %(site)s)
ORDER BY j.finished_at DESC NULLS LAST, j.id DESC
LIMIT %(limit)s
"""


# --------------------------------------------------------------------- review / stats / gallery (Fase 2, SPEC 1.3)

# The shared filter clause. Every optional filter is NULL-tolerant, so ONE statement serves every
# filter combination and no SQL is ever assembled outside this file. `site` is the SCOPED site
# (a site account is forced to its own; admin passes what it asked or NULL = all). date_to is
# inclusive: ts < date_to + 1 day. Casts make a Python None a typed NULL. From Fase 3 a discarded
# row is invisible to every reader of this clause: only the papelera lists it.
_PLATE_FILTERS = """
  (%(site)s::text IS NULL OR site = %(site)s)
  AND (%(record_type)s::text IS NULL OR record_type = %(record_type)s)
  AND (%(date_from)s::date IS NULL OR ts >= %(date_from)s::date)
  AND (%(date_to)s::date IS NULL OR ts < (%(date_to)s::date + 1))
  AND review_status <> 'discarded'
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
# (leftovers stays untouched; leftovers_verified NULL = the model was right). A discarded row is
# only restorable through the papelera, never through review.
VERIFY_PLATE = f"""
UPDATE plates SET
  dish_verified      = %(dish_verified)s,
  leftovers_verified = %(leftovers_verified)s,
  review_status      = 'verified'
WHERE id = %(id)s AND review_status <> 'discarded' AND {_SCOPE}
RETURNING *
"""

SET_REVIEW_STATUS = f"""
UPDATE plates SET review_status = %(status)s
WHERE id = %(id)s AND review_status <> 'discarded' AND {_SCOPE}
RETURNING *
"""

# Bulk "correcto": the model was right on the whole selection; rows without a prediction yet
# have nothing to confirm and are skipped (the caller reports the count).
BULK_VERIFY_AS_IS = f"""
UPDATE plates SET
  dish_verified      = dish_predicted,
  leftovers_verified = NULL,
  review_status      = 'verified'
WHERE id = ANY(%(ids)s) AND dish_predicted IS NOT NULL AND review_status <> 'discarded' AND {_SCOPE}
"""

BULK_SET_REVIEW_STATUS = f"""
UPDATE plates SET review_status = %(status)s
WHERE id = ANY(%(ids)s) AND review_status <> 'discarded' AND {_SCOPE}
"""

# Descartar (Fase 3): into the papelera, with the retention clock started.
DISCARD_PLATE = f"""
UPDATE plates SET review_status = 'discarded', discarded_at = now()
WHERE id = %(id)s AND review_status <> 'discarded' AND {_SCOPE}
RETURNING *
"""

BULK_DISCARD = f"""
UPDATE plates SET review_status = 'discarded', discarded_at = now()
WHERE id = ANY(%(ids)s) AND review_status <> 'discarded' AND {_SCOPE}
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

# The panel's "bajo el umbral" tile (Fase 3): analyzed return rows whose confidence ranks under
# confianza_minima (min_rank: alta 3, media 2, baja 1).
STATS_LOW_CONFIDENCE = f"""
SELECT COUNT(*) AS n
FROM plates
WHERE record_type = 'return'
  AND review_status <> 'not_plate'
  AND confidence IS NOT NULL
  AND (CASE confidence WHEN 'alta' THEN 3 WHEN 'media' THEN 2 ELSE 1 END) < %(min_rank)s
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


# --------------------------------------------------------------------- the papelera (Fase 3, SPEC 1.5)

PAPELERA_COUNT = f"SELECT COUNT(*) AS n FROM plates WHERE review_status = 'discarded' AND {_SCOPE}"

PAPELERA_LIST = f"""
SELECT id, site, camera, record_type, ts, discarded_at, dish_predicted, dish_verified,
       confidence, presentation
FROM plates
WHERE review_status = 'discarded' AND {_SCOPE}
ORDER BY discarded_at DESC, id DESC
LIMIT %(limit)s OFFSET %(offset)s
"""

# Restore: back to pendientes, corrections kept, the retention clock cleared.
PAPELERA_RESTORE = f"""
UPDATE plates SET review_status = 'unreviewed', discarded_at = NULL
WHERE id = ANY(%(ids)s) AND review_status = 'discarded' AND {_SCOPE}
RETURNING id
"""

# Eliminar-ya, step 1: the chosen discarded rows the diff names, one line per plate (Section 3.14).
PAPELERA_SELECT_IDS = f"""
SELECT id, site, camera, record_type, discarded_at, dish_predicted, dish_verified
FROM plates
WHERE id = ANY(%(ids)s) AND review_status = 'discarded' AND {_SCOPE}
ORDER BY id
"""

# Eliminar-ya: lock the chosen discarded rows (and read their photos) before deleting them.
PAPELERA_LOCK_FOR_DELETE = f"""
SELECT id, photo_path
FROM plates
WHERE id = ANY(%(ids)s) AND review_status = 'discarded' AND {_SCOPE}
FOR UPDATE
"""

# The worker's retention purge (retention: a timedelta): multi-worker safe through SKIP LOCKED.
PURGE_DUE_DISCARDED = """
SELECT id, photo_path
FROM plates
WHERE review_status = 'discarded' AND discarded_at < now() - %(retention)s
ORDER BY discarded_at, id
LIMIT %(limit)s
FOR UPDATE SKIP LOCKED
"""

# jobs.plate_id has no cascade and schema.sql stays sealed: the plate's jobs die FIRST.
DELETE_JOBS_OF_PLATES = "DELETE FROM jobs WHERE plate_id = ANY(%(ids)s)"
DELETE_PLATES_BY_IDS = "DELETE FROM plates WHERE id = ANY(%(ids)s) AND review_status = 'discarded'"


# --------------------------------------------------------------------- spend (Fase 3, SPEC 1.5): gasto de filas vivas

# One site's usage per LOCAL day (tz: the site's timezone name) and model, from `since` (timestamptz).
SPEND_BY_DAY = """
SELECT
  (j.finished_at AT TIME ZONE %(tz)s)::date AS day,
  p.model,
  COUNT(*) AS jobs,
  COALESCE(SUM((j.usage->>'calls')::bigint), 0) AS calls,
  COALESCE(SUM((j.usage->>'prompt_tokens')::bigint), 0) AS prompt_tokens,
  COALESCE(SUM((j.usage->>'completion_tokens')::bigint), 0) AS completion_tokens,
  COALESCE(SUM((j.usage->>'cache_creation_input_tokens')::bigint), 0) AS cache_creation_input_tokens,
  COALESCE(SUM((j.usage->>'cache_read_input_tokens')::bigint), 0) AS cache_read_input_tokens
FROM jobs AS j
JOIN plates AS p ON p.id = j.plate_id
WHERE j.status = 'done' AND j.usage IS NOT NULL AND p.site = %(site)s AND j.finished_at >= %(since)s
GROUP BY 1, 2
ORDER BY 1 DESC, 2
"""

# Leer ficha's spend (Section 3.14): its tokens live in the menu.ficha bitácora rows (a call that never
# answered has none), per LOCAL day of the brand's timezone and model.
GASTO_FICHAS = """
SELECT
  (ts AT TIME ZONE %(tz)s)::date AS day,
  detail->>'modelo' AS model,
  COUNT(*) AS lecturas,
  COALESCE(SUM((detail->>'prompt_tokens')::bigint), 0) AS prompt_tokens,
  COALESCE(SUM((detail->>'completion_tokens')::bigint), 0) AS completion_tokens,
  COALESCE(SUM((detail->>'cache_creation_input_tokens')::bigint), 0) AS cache_creation_input_tokens,
  COALESCE(SUM((detail->>'cache_read_input_tokens')::bigint), 0) AS cache_read_input_tokens
FROM admin_log
WHERE action = 'menu.ficha' AND detail ? 'prompt_tokens' AND ts >= %(since)s
GROUP BY 1, 2
ORDER BY 1 DESC, 2
"""

# A price may be entered for a keyed model, an already priced one, or one a plate or a ficha reading used.
MODEL_USED = """
SELECT EXISTS (SELECT 1 FROM plates WHERE model = %(model)s)
    OR EXISTS (SELECT 1 FROM admin_log WHERE action = 'menu.ficha' AND detail->>'modelo' = %(model)s) AS used
"""


# --------------------------------------------------------------------- the bot: READ-ONLY here (its one table is above)

COUNT_WORKING_JOBS = "SELECT COUNT(*) AS n FROM jobs WHERE status = 'working'"

# The silent-worker clock: when did the worker last finish anything.
BOT_LAST_DONE_AT = "SELECT MAX(finished_at) AS at FROM jobs WHERE status = 'done'"

# /cola, resolved to the group's site: the last plate of THAT site the worker finished.
BOT_LAST_DONE_SITE = """
SELECT j.plate_id, COALESCE(p.dish_verified, p.dish_predicted) AS dish,
       EXTRACT(EPOCH FROM (now() - j.finished_at)) AS age_s
FROM jobs AS j
JOIN plates AS p ON p.id = j.plate_id
WHERE j.status = 'done' AND p.site = %(site)s
ORDER BY j.finished_at DESC
LIMIT 1
"""

# /cola, resolved to the group's site: that site's own waiting and in-process plates (Section 3.11).
BOT_SITE_QUEUE = """
SELECT COUNT(*) FILTER (WHERE j.status = 'pending') AS pending,
       COUNT(*) FILTER (WHERE j.status = 'working') AS working
FROM jobs AS j
JOIN plates AS p ON p.id = j.plate_id
WHERE p.site = %(site)s AND j.status IN ('pending', 'working')
"""

# Failures announced by finished_at (a retried job keeps its id: its second failure must not vanish).
# The cursor is the pair (finished_at, id); the baseline is the newest failure at boot.
BOT_FAILED_CURSOR = """
SELECT finished_at, id
FROM jobs
WHERE status = 'failed' AND finished_at IS NOT NULL
ORDER BY finished_at DESC, id DESC
LIMIT 1
"""

BOT_FAILED_AFTER = """
SELECT id, plate_id, attempts, last_error, finished_at
FROM jobs
WHERE status = 'failed' AND finished_at IS NOT NULL
  AND (%(after_at)s::timestamptz IS NULL
       OR (finished_at, id) > (%(after_at)s::timestamptz, %(after_id)s::bigint))
ORDER BY finished_at, id
"""

# Dirty lens: the last n bursts of one camera and their sharpness (only rows that carry it).
BOT_RECENT_SHARPNESS = """
SELECT id, (capture->>'sharpness')::float AS sharpness
FROM plates
WHERE site = %(site)s AND camera = %(camera)s AND capture ? 'sharpness'
ORDER BY ts DESC, id DESC
LIMIT %(n)s
"""


# --------------------------------------------------------------------- the bake-off (Fase 2, T-E3): READ-ONLY

# The exam's truth set: verified RETURN rows with a real dish; the human's percentages when they
# exist, else the model's draft the human confirmed. limit NULL = every row.
BAKEOFF_VERIFIED_ROWS = """
SELECT id, site, photo_path, dish_verified, COALESCE(leftovers_verified, leftovers) AS truth_pct
FROM plates
WHERE review_status = 'verified'
  AND record_type = 'return'
  AND dish_verified IS NOT NULL
  AND dish_verified <> 'desconocido'
ORDER BY ts DESC, id DESC
LIMIT %(limit)s
"""
