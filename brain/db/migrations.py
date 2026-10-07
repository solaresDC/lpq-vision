"""Schema evolution: existence-gated, ADD-only migrations, run on EVERY boot (SPEC 1.2, Fases 2 and 3).

schema.sql is the sealed birth certificate and never changes. Everything the database learns after
birth lives here as small steps: each one checks whether its column (or, from Fase 3, its table)
already exists and only then adds it, so a boot with nothing missing costs milliseconds and a
brand-new database gets everything right after the schema is created. init.run() calls run() inside
its own transaction, schema present or not: the live laptop and VPS databases evolve on their first
boot of the new code, with no human remembering anything.

Fase 2:
1. plates.leftovers_verified JSONB: the human's corrected percentages, beside the model's draft,
   never over it. Readers use COALESCE(leftovers_verified, leftovers).
2. review_status gains 'not_plate' (no DDL: the column is TEXT).
3. plates.capture JSONB: capture-time facts written ONCE at insert by upload_burst.

Fase 3 (table steps are gated on to_regclass, column steps on information_schema):
4. users: accounts in the database; the reserved 'cuarto' row (role machine) IS the machine floor.
5. admin_log: the internal bitácora; usuario NULL means the system acted (boot, reconvergence, auto-off).
6. horario_extensiones: the one-night extension, written by the bot (its ONE writable table).
7. plates.discarded_at TIMESTAMPTZ: the papelera's retention clock.
8. jobs.usage JSONB: token counts per finished job, written inside the completion transaction.
9. sites.device_key TEXT: the site's gafete; init births one for every site that has none.
10. review_status gains 'discarded' (no DDL). The four-state contract is named below.
11. users.uid BIGSERIAL UNIQUE: the permanent account number, never reused (owner ruling 3.9.2).
12. users.deleted_at and users.erase_warned_at: the recoverable delete and its one warning.
13. admin_log.usuario_uid: the acting account's number beside its name.
14. users.password_changed_at and users.password_changed_by: when a password last changed, and who changed it
    (nobody can SEE a password: only its hash exists).

Nothing here ever ALTERs or DROPs what exists: every step CREATEs a missing table or ADDs a missing column.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import psycopg

from brain.db import queries as q

log = logging.getLogger("lpq.db.migrations")

# The review_status contract of this era, named once so readers and writers agree (SPEC 1.2).
REVIEW_UNREVIEWED = "unreviewed"
REVIEW_VERIFIED = "verified"
REVIEW_NOT_PLATE = "not_plate"
REVIEW_DISCARDED = "discarded"      # the papelera: only the papelera lists these rows
REVIEW_STATUSES = (REVIEW_UNREVIEWED, REVIEW_VERIFIED, REVIEW_NOT_PLATE, REVIEW_DISCARDED)


@dataclass(frozen=True)
class ColumnMigration:
    """One ADD COLUMN step: gated on the column's absence, applied verbatim from queries.py."""

    name: str
    table: str
    column: str
    statement: str


@dataclass(frozen=True)
class TableMigration:
    """One CREATE TABLE step (Fase 3): gated on the table's absence, applied verbatim from queries.py."""

    name: str
    table: str
    statement: str


MIGRATIONS: tuple[ColumnMigration | TableMigration, ...] = (
    ColumnMigration(
        name="plates.leftovers_verified",
        table="plates",
        column="leftovers_verified",
        statement=q.ADD_PLATES_LEFTOVERS_VERIFIED,
    ),
    ColumnMigration(
        name="plates.capture",
        table="plates",
        column="capture",
        statement=q.ADD_PLATES_CAPTURE,
    ),
    TableMigration(name="users", table="users", statement=q.CREATE_USERS_TABLE),
    TableMigration(name="admin_log", table="admin_log", statement=q.CREATE_ADMIN_LOG_TABLE),
    TableMigration(
        name="horario_extensiones",
        table="horario_extensiones",
        statement=q.CREATE_HORARIO_EXTENSIONES_TABLE,
    ),
    ColumnMigration(
        name="plates.discarded_at",
        table="plates",
        column="discarded_at",
        statement=q.ADD_PLATES_DISCARDED_AT,
    ),
    ColumnMigration(
        name="jobs.usage",
        table="jobs",
        column="usage",
        statement=q.ADD_JOBS_USAGE,
    ),
    ColumnMigration(
        name="sites.device_key",
        table="sites",
        column="device_key",
        statement=q.ADD_SITES_DEVICE_KEY,
    ),
    ColumnMigration(name="users.uid", table="users", column="uid", statement=q.ADD_USERS_UID),
    ColumnMigration(name="users.deleted_at", table="users", column="deleted_at", statement=q.ADD_USERS_DELETED_AT),
    ColumnMigration(
        name="users.erase_warned_at", table="users", column="erase_warned_at", statement=q.ADD_USERS_ERASE_WARNED_AT,
    ),
    ColumnMigration(
        name="admin_log.usuario_uid", table="admin_log", column="usuario_uid", statement=q.ADD_ADMIN_LOG_USUARIO_UID,
    ),
    ColumnMigration(
        name="users.password_changed_at", table="users", column="password_changed_at",
        statement=q.ADD_USERS_PASSWORD_CHANGED_AT,
    ),
    ColumnMigration(
        name="users.password_changed_by", table="users", column="password_changed_by",
        statement=q.ADD_USERS_PASSWORD_CHANGED_BY,
    ),
)


def column_present(conn: psycopg.Connection, table: str, column: str) -> bool:
    row = conn.execute(q.COLUMN_PRESENT, {"table": table, "column": column}).fetchone()
    return bool(row and row["present"])


def table_present(conn: psycopg.Connection, table: str) -> bool:
    row = conn.execute(q.TABLE_PRESENT, {"qualified": f"public.{table}"}).fetchone()
    return bool(row and row["present"])


def run(conn: psycopg.Connection) -> dict[str, str]:
    """Apply every missing migration on this connection (the caller owns the transaction).

    Returns {migration name: 'added' | 'present'} so the boot log tells exactly what happened.
    """
    outcome: dict[str, str] = {}
    for migration in MIGRATIONS:
        if isinstance(migration, TableMigration):
            present = table_present(conn, migration.table)
        else:
            present = column_present(conn, migration.table, migration.column)
        if present:
            outcome[migration.name] = "present"
            continue
        conn.execute(migration.statement)
        outcome[migration.name] = "added"
        log.info("migration applied: %s", migration.name)
    return outcome
