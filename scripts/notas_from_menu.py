"""The one-time copy of owner ruling 3.10.2: the component descriptions that lived only as YAML comments in
config/menu.yaml become each dish's "notas para el modelo", and the components of the dishes seeded from
menu.yaml get their labels, with accents. Runs INSIDE the fastapi container:

    docker compose -f compose.mac.yaml exec -T fastapi python -m scripts.notas_from_menu [--dry-run]

On the laptop in 3.10.2; on the server at the deploy (3.18). What it writes, in one transaction:
- Notes: ONLY for a dish whose notas IS NULL (never written). A note a person wrote or cleared ('') is never
  touched, and the write itself checks NULL again. One line per described component, "Etiqueta: descripción.",
  in the dish's order; menu_builder gives them to the model as one line under the dish.
- Labels: ONLY for a component of the dishes in LABELS that has none yet. Keys, portions, names and menu
  versions never change, so nothing bumps the menu version.
- One bitácora row (a system row) listing what it wrote.
A second run writes nothing. --dry-run prints the same plan and writes nothing. YAML loaders drop comments, so
menu.yaml is read line by line, and every description found is checked against the validated menu first.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from brain.db import queries as q
from brain.validator.models import MENU_PATH, NOTAS_MAX_CHARS, Componente, load_menu

# The labels of the dishes seeded from menu.yaml (3.5.1), the way the kitchen says them.
LABELS: dict[str, dict[str, str]] = {
    "tartine_aguacate": {
        "pan_multigrano": "Pan multigrano",
        "aguacate_preparado": "Aguacate preparado",
        "jitomate_cherry": "Jitomate cherry",
        "germinados": "Germinados",
    },
    "omelette_champinones": {
        "omelette_huevo": "Omelette (huevo)",
        "relleno_champinones": "Relleno de champiñones",
        "ensalada_espinaca": "Ensalada de espinaca",
        "pan_tostado": "Pan tostado",
    },
    "ensalada_queso_cabra": {
        "queso_cabra": "Queso de cabra",
        "flauta_avellana": "Flauta de avellana",
        "jitomate": "Jitomate",
        "espinaca": "Espinaca",
        "calabaza": "Calabaza",
        "higo": "Higo",
        "vinagreta_frambuesa": "Vinagreta de frambuesa",
        "germen_betabel": "Germen de betabel",
        "hemp": "Hemp",
        "perejil": "Perejil",
    },
    "tartine_chipotle_club": {
        "pan_trigo": "Pan de trigo",
        "pollo_escalopa": "Pollo en escalopa",
        "queso_oaxaca": "Queso Oaxaca",
        "aguacate": "Aguacate",
        "arugula": "Arúgula",
        "tocino": "Tocino",
        "jitomate": "Jitomate",
        "cebolleta": "Cebolleta",
        "aderezo_chipotle": "Aderezo chipotle",
        "ensalada_tartine": "Ensalada tartine",
        "vinagreta_casa": "Vinagreta de la casa",
        "perejil": "Perejil",
    },
}

# menu.yaml's comments were written without accents; people read the notes, so these words get theirs back.
ACCENTS = {
    "aqui": "aquí",
    "arugula": "arúgula",
    "champinones": "champiñones",
    "cunas": "cuñas",
    "frio": "frío",
    "laminas": "láminas",
    "rabano": "rábano",
    "triangulo": "triángulo",
    "triangulos": "triángulos",
}
BITACORA_ACTION = "menu.notas_from_menu"

_ACCENT_RE = re.compile(r"\b(?:" + "|".join(sorted(ACCENTS, key=len, reverse=True)) + r")\b", re.IGNORECASE)
_DISH_RE = re.compile(r"^  ([a-z0-9_]+):\s*(?:#.*)?\Z")
_COMPONENT_RE = re.compile(r"^\s+- \{\s*nombre:\s*([a-z0-9_]+)\s*,[^}]*\}\s*#\s*(.+?)\s*\Z")


def _accents(text: str) -> str:
    def fix(match: re.Match[str]) -> str:
        word = match.group(0)
        fixed = ACCENTS[word.lower()]
        return fixed[:1].upper() + fixed[1:] if word[:1].isupper() else fixed

    return _ACCENT_RE.sub(fix, text)


def _sentence(text: str) -> str:
    text = _accents(" ".join(text.split()))
    return text if text.endswith((".", "!", "?")) else text + "."


def _readable(key: str) -> str:
    """The label of a component nobody labeled: its key in plain words (the page shows the same)."""
    return key.replace("_", " ").capitalize()


def descriptions(path: Path) -> dict[str, dict[str, str]]:
    """{dish_id: {component key: description}} from the comments of menu.yaml's `menu:` section."""
    found: dict[str, dict[str, str]] = {}
    section: str | None = None
    dish: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            section = line.split(":", 1)[0].strip()
            dish = None
            continue
        if section != "menu":
            continue
        top = _DISH_RE.match(line)
        if top:
            dish = top.group(1)
            continue
        comp = _COMPONENT_RE.match(line)
        if comp and dish:
            found.setdefault(dish, {})[comp.group(1)] = comp.group(2)
    return found


Plan = tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]


def plan(conn: psycopg.Connection, described: dict[str, dict[str, str]]) -> Plan:
    """What a run would write: (label changes, notes, problems). Reads only."""
    rows = conn.execute(q.SELECT_ALL_DISHES).fetchall()
    without = {r["dish_id"] for r in conn.execute(q.SELECT_DISHES_WITHOUT_NOTAS).fetchall()}
    labels: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    problems: list[str] = []
    for row in rows:
        dish_id = row["dish_id"]
        preset = LABELS.get(dish_id, {})
        componentes = [dict(c) for c in row["componentes"]]
        filled = 0
        for c in componentes:
            if not c.get("etiqueta") and c["nombre"] in preset:
                c["etiqueta"] = preset[c["nombre"]]
                filled += 1
        for c in componentes:
            try:
                Componente.model_validate(c)      # a label the page could not show stops the run, before any write
            except ValidationError as exc:
                problems.append(f"{dish_id}/{c.get('nombre')}: {exc.errors()[0]['msg']}")
        if filled:
            labels.append({"row": row, "componentes": componentes, "filled": filled})
        texts = described.get(dish_id, {})
        if dish_id not in without or not texts:
            continue
        lines = [
            f"{c.get('etiqueta') or _readable(c['nombre'])}: {_sentence(texts[c['nombre']])}"
            for c in componentes if c["nombre"] in texts
        ]
        if not lines:
            continue
        notas = "\n".join(lines)
        if len(notas) > NOTAS_MAX_CHARS:
            problems.append(f"{dish_id}: the notes would be {len(notas)} characters, max {NOTAS_MAX_CHARS}")
        notes.append({"dish_id": dish_id, "notas": notas, "lines": len(lines)})
    return labels, notes, problems


def main() -> int:
    parser = argparse.ArgumentParser(description="copy menu.yaml's component descriptions into the dishes' notes, once")
    parser.add_argument("--dry-run", action="store_true", help="print what it would write and write nothing")
    args = parser.parse_args()

    menu = load_menu()
    described = descriptions(MENU_PATH)
    for dish_id, texts in described.items():
        known = set(menu.menu[dish_id].component_names) if dish_id in menu.menu else set()
        strays = sorted(set(texts) - known)
        if strays:
            print(f"menu.yaml was misread ({dish_id}: {strays}): nothing written")
            return 1

    with q.connect() as conn, conn.transaction():
        labels, notes, problems = plan(conn, described)
        verb = "would write" if args.dry_run or problems else "writing"
        for item in labels:
            row = item["row"]
            print(f"{verb} labels: {row['dish_id']} ({item['filled']} of {len(item['componentes'])} components)")
        for item in notes:
            print(f"{verb} notes: {item['dish_id']} ({item['lines']} lines, {len(item['notas'])} characters)")
            for line in item["notas"].splitlines():
                print("    " + line)
        if problems:
            for problem in problems:
                print("problem: " + problem)
            print("nothing written: fix the problem(s) above first")
            return 1
        if not labels and not notes:
            print("nothing to write: every note was already written and every label is in place")
            return 0
        if args.dry_run:
            print("dry run: nothing written")
            return 0
        for item in labels:
            row = item["row"]
            conn.execute(q.UPDATE_DISH_DEFINITION, {
                "dish_id": row["dish_id"],
                "nombre": row["nombre"],
                "plate_type": row["plate_type"],
                "componentes": Jsonb(item["componentes"]),
                "menu_version": row["menu_version"],
            })
        written = [
            item["dish_id"] for item in notes
            if conn.execute(q.SET_DISH_NOTAS_IF_NULL, {"dish_id": item["dish_id"], "notas": item["notas"]}).rowcount
        ]
        conn.execute(q.INSERT_ADMIN_LOG, {
            "usuario": None,
            "action": BITACORA_ACTION,
            "detail": Jsonb({"notas": written, "etiquetas": {i["row"]["dish_id"]: i["filled"] for i in labels}}),
        })
    print(f"done: labels on {len(labels)} dish(es), notes on {len(written)} dish(es); "
          f"one bitácora row ({BITACORA_ACTION})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
