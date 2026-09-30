"""The listed interactive password road (SPEC 1.3): run BY THE HUMAN inside the fastapi container.

    docker compose -f compose.mac.yaml exec fastapi python -m scripts.set_password <usuario>

The password is typed twice (getpass: never echoed, never in shell history), hashed with the api's
PBKDF2 and written straight to the users row, which is also REACTIVATED: the owner can always get
back in over ssh, even after a misclick deactivated the last admin. Only the two reserved names can
be BORN here: `admin` (role admin, no site: a fresh install's first account) and `cuarto` (role
machine: its hash IS the machine floor). Any other account must already exist (born in the
mostrador) or the tool refuses before asking for anything. A live session of that account in the
api keeps working until it logs out or expires (this process cannot reach the api's memory); the
mostrador's reset drops sessions at once. Nothing is printed but the name, the role and the outcome;
the bitácora gets one row with the name and the road, never the password or its hash.
"""

from __future__ import annotations

import getpass
import sys

from psycopg.types.json import Jsonb

from api.auth import ROLE_ADMIN, ROLE_MACHINE, make_hash
from brain.db import queries as q
from brain.validator.models import MACHINE_USER, PASSWORD_MIN_CHARS

# The only names this tool may BIRTH, and the role each is born with.
RESERVED_BIRTHS: dict[str, str] = {"admin": ROLE_ADMIN, MACHINE_USER: ROLE_MACHINE}


def _ask() -> str | None:
    """Two getpass prompts; None (nothing saved) when they differ or the password is too short."""
    first = getpass.getpass("Contraseña nueva: ")
    second = getpass.getpass("Repítela: ")
    if first != second:
        print("No coinciden: nada guardado.", file=sys.stderr)
        return None
    if len(first) < PASSWORD_MIN_CHARS:
        print(f"Muy corta: mínimo {PASSWORD_MIN_CHARS} caracteres. Nada guardado.", file=sys.stderr)
        return None
    return first


def main(argv: list[str]) -> int:
    if len(argv) != 1 or not argv[0].strip():
        print(
            f"uso: python -m scripts.set_password <usuario>   (aquí solo nacen admin y {MACHINE_USER}; "
            "las demás cuentas deben existir)",
            file=sys.stderr,
        )
        return 2
    usuario = argv[0].strip()

    with q.connect() as conn:
        row = conn.execute(q.SELECT_USER, {"usuario": usuario}).fetchone()
    if usuario not in RESERVED_BIRTHS:
        if row is None:
            print(
                f"«{usuario}» no existe: créalo en el mostrador (Cuentas y sitios). "
                f"Aquí solo nacen admin y {MACHINE_USER}.",
                file=sys.stderr,
            )
            return 1
        if row["role"] == ROLE_MACHINE:
            print(f"«{usuario}» es una cuenta de máquina: la única es {MACHINE_USER}.", file=sys.stderr)
            return 1

    password = _ask()
    if password is None:
        return 1
    password_hash = make_hash(password)

    with q.connect() as conn, conn.transaction():
        if usuario in RESERVED_BIRTHS:
            out = conn.execute(
                q.UPSERT_RESERVED_USER,
                {"usuario": usuario, "password_hash": password_hash, "role": RESERVED_BIRTHS[usuario]},
            ).fetchone()
        else:
            out = conn.execute(
                q.SET_PASSWORD_AND_REACTIVATE,
                {"usuario": usuario, "password_hash": password_hash},
            ).fetchone()
        conn.execute(
            q.INSERT_ADMIN_LOG,
            {
                "usuario": None,
                "action": "users.set_password",
                "detail": Jsonb({"usuario": usuario, "via": "set_password", "born": row is None}),
            },
        )
    print(f"Listo: contraseña guardada para «{out['usuario']}» (rol {out['role']}), cuenta activa.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
