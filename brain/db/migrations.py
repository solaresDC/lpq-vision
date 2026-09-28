"""Fase-2 schema evolution: existence-gated, ADD-only migrations, run on EVERY boot (SPEC 1.2).

schema.sql is the sealed birth certificate and never changes. Everything the database learns
after birth lives here as small steps: each one checks whether its column already exists and
only then adds it, so a boot with nothing missing costs milliseconds and a brand-new database
gets the columns right after the schema is created. init.run() calls run() inside its own
transaction, schema present or not: the live laptop and VPS databases evolve on their first
boot of the new code, with no human remembering anything.

The three Fase-2 migrations:
1. plates.leftovers_verified JSONB: the human's corrected percentages. The model's draft in
   `leftovers` is never erased (same philosophy as dish_predicted / dish_verified). NULL = the
   human changed nothing; readers use COALESCE(leftovers_verified, leftovers).
2. review_status gains the value 'not_plate'. No DDL: the column is TEXT. The contract across
   eras is 'unreviewed' | 'verified' | 'not_plate', plus 'discarded' RESERVED for the papelera
   (FASE 3). The partial index on 'unreviewed' keeps working untouched.
3. plates.capture JSONB: capture-time facts written ONCE at insert by upload_burst and never
   touched by the worker (whose result write replaces `validator` wholesale). Absent-when-off:
   this era it carries `sharpness` and, only when the entry was ambiguous, `direction_dudosa`.
   A plain /api/upload row carries no capture value (NULL).

Every step is a column ADD. Nothing here ever ALTERs or DROPs what exists.
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
REVIEW_STATUSES = (REVIEW_UNREVIEWED, REVIEW_VERIFIED, REVIEW_NOT_PLATE)
# 'discarded' is reserved for FASE 3's papelera: named here so nobody reuses the word, never written this era.
REVIEW_DISCARDED_RESERVED = "discarded"


@dataclass(frozen=True)
class ColumnMigration:
    """One ADD COLUMN step: gated on the column's absence, applied verbatim from queries.py."""

    name: str
    table: str
    column: str
    statement: str


MIGRATIONS: tuple[ColumnMigration, ...] = (
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
)


def column_present(conn: psycopg.Connection, table: str, column: str) -> bool:
    row = conn.execute(q.COLUMN_PRESENT, {"table": table, "column": column}).fetchone()
    return bool(row and row["present"])


def run(conn: psycopg.Connection) -> dict[str, str]:
    """Apply every missing migration on this connection (the caller owns the transaction).

    Returns {migration name: 'added' | 'present'} so the boot log tells exactly what happened.
    """
    outcome: dict[str, str] = {}
    for migration in MIGRATIONS:
        if column_present(conn, migration.table, migration.column):
            outcome[migration.name] = "present"
            continue
        conn.execute(migration.statement)
        outcome[migration.name] = "added"
        log.info("migration applied: %s", migration.name)
    return outcome
