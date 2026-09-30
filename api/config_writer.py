"""LPQ_VISION api/config_writer.py: the ONE writer of config.yaml, and of textos.yaml (SPEC 1.4; sizing ruling v3.8).

The api is the only process that ever writes config.yaml, and this module is the only code that does it.
Every write is SURGICAL: the file's text is edited line by line, so every comment, every blank line and
every untouched value survives byte for byte; pyyaml never dumps it. Before anything lands, the whole new
text is parsed, each change is read back to prove it landed exactly where it was aimed, and the result is
validated by the same models the system loads with (brain.validator.models.Config, which also runs the
knob registry over every site override). Only then is it swapped in atomically (tmp + os.replace).
One asyncio LOCK serializes every write of the process; callers that must pair a config write with a
database transaction hold the LOCK themselves and call apply_ops(), keeping WriteResult.previous_text
to restore() if the database side fails.

Values follow one law: EVERY string is double-quoted (a bare 22:00 is the YAML number 1320, a bare
-100123 an integer, a bare on/off a boolean), numbers and booleans stay bare, and a mapping becomes a
one-line flow mapping (knob values: overrides are flat, one line per registry name) or, for structural
inserts such as a new site or camera, an indented block; a Flow mapping inside a block stays on one line
(funciones). Deleting a key removes its line and its block, and a parent mapping left empty goes too:
absent-when-off, never a hollow `overrides:` left behind.

textos.yaml is the one machine-owned exception: it is rewritten whole (brain.validator.textos.textos_yaml)
under the same lock.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import ValidationError

from brain.validator.models import CONFIG_PATH, Config
from brain.validator.textos import TEXTOS_PATH, load_textos, textos_yaml

# --- named constants ------------------------------------------------------------------
LOCK = asyncio.Lock()          # the ONE write lock of the process (config.yaml and textos.yaml)
INDENT_STEP = 2                # the indentation a NEW block uses when its parent has no children yet
_SAFE_KEY = re.compile(r"^[A-Za-z0-9_.-]+\Z")
_YAML_WORDS = frozenset({"y", "yes", "n", "no", "true", "false", "on", "off", "null", "~"})
_PLAIN_KEY = re.compile(r"([^\s:#][^:#]*?)\s*:(?:\s|\Z)")
_MISSING = object()


class WriterError(Exception):
    """A write that must not land; the message is shown to the person on the page (Spanish, or the
    models' own words for a bounce). Nothing was written when this is raised."""


class Flow(dict):
    """A mapping the writer renders on ONE line even inside a block (funciones). Nothing inside a flow
    mapping is ever edited in place: the whole value is replaced."""


@dataclass(frozen=True)
class Op:
    kind: Literal["set", "delete"]
    path: tuple[str, ...]
    value: Any = None
    flow: bool = True          # True: the value goes on one line; False: a mapping becomes an indented block


def set_op(path: tuple[str, ...], value: Any, *, flow: bool = True) -> Op:
    if value is None:
        raise WriterError("un valor vacío se escribe borrando la llave (ausente = apagado)")
    return Op("set", tuple(path), value, flow)


def delete_op(path: tuple[str, ...]) -> Op:
    return Op("delete", tuple(path))


@dataclass
class WriteResult:
    changes: list[dict[str, Any]]   # per op: path, action (inserted/replaced/deleted/absent), old (raw text), new
    previous_text: str             # the file before the write: restore() takes it back
    written: bool                  # False when the ops changed nothing


# --- rendering ------------------------------------------------------------------------

def render_key(key: str) -> str:
    """A key stays bare when plainly safe, else it is double-quoted (a model id with '/', a magic word)."""
    if _SAFE_KEY.match(key) and key.lower() not in _YAML_WORDS:
        return key
    return json.dumps(key, ensure_ascii=False)


def render_value(value: Any) -> str:
    """One scalar or flow mapping, by the system's orthography law."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, datetime):
        return json.dumps(value.isoformat())
    if isinstance(value, dict):
        return "{" + ", ".join(f"{render_key(str(k))}: {render_value(v)}" for k, v in value.items()) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(render_value(v) for v in value) + "]"
    raise WriterError(f"no sé escribir un valor de tipo {type(value).__name__}")


def _render_leaf(key: str, value: Any, flow: bool, indent: int) -> list[str]:
    if isinstance(value, dict) and value and not flow and not isinstance(value, Flow):
        out = [" " * indent + f"{render_key(key)}:"]
        for k, v in value.items():
            out += _render_leaf(str(k), v, False, indent + INDENT_STEP)
        return out
    return [" " * indent + f"{render_key(key)}: {render_value(value)}"]


def _render_path(keys: tuple[str, ...], value: Any, flow: bool, indent: int) -> list[str]:
    """Missing parents become block openers; the leaf is one line (flow) or an indented block."""
    lines: list[str] = []
    for key in keys[:-1]:
        lines.append(" " * indent + f"{render_key(key)}:")
        indent += INDENT_STEP
    return lines + _render_leaf(keys[-1], value, flow, indent)


# --- reading the text -----------------------------------------------------------------

def _split_comment(line: str) -> tuple[str, str | None]:
    """(code, comment): the comment starts at a '#' outside quotes, at the line's start or after whitespace."""
    in_single = in_double = escaped = False
    for i, ch in enumerate(line):
        if in_double:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_double = False
            continue
        if in_single:
            if ch == "'":
                in_single = False
            continue
        if ch == '"':
            in_double = True
        elif ch == "'":
            in_single = True
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i], line[i:]
    return line, None


def _parse_line(line: str) -> tuple[str | None, str]:
    """(key, inline value text) of a mapping line; key None for list items and anything else."""
    code, _ = _split_comment(line)
    s = code.strip()
    if not s or s.startswith("- "):
        return None, ""
    if s.startswith('"'):
        i, escaped = 1, False
        while i < len(s):
            ch = s[i]
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                break
            i += 1
        if i >= len(s) or not s[i + 1:].startswith(":"):
            return None, ""
        return json.loads(s[: i + 1]), s[i + 2:].strip()
    m = _PLAIN_KEY.match(s)
    if not m:
        return None, ""
    return m.group(1), s[m.end():].strip()


def _content(line: str) -> bool:
    s = line.strip()
    return bool(s) and not s.startswith("#")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _block_end(lines: list[str], start: int, indent: int) -> int:
    """Index of the first content line after `start` at indent <= `indent`: where that block ends."""
    i = start + 1
    while i < len(lines):
        if _content(lines[i]) and _indent(lines[i]) <= indent:
            return i
        i += 1
    return len(lines)


def _child_indent(lines: list[str], start: int, end: int, parent_indent: int) -> int:
    for i in range(start + 1, end):
        if _content(lines[i]):
            ind = _indent(lines[i])
            return ind if ind > parent_indent else parent_indent + INDENT_STEP
    return parent_indent + INDENT_STEP


def _find_child(lines: list[str], start: int, end: int, child_indent: int, key: str) -> int | None:
    for i in range(start + 1, end):
        if _content(lines[i]) and _indent(lines[i]) == child_indent and _parse_line(lines[i])[0] == key:
            return i
    return None


def _last_content(lines: list[str], start: int, end: int) -> int:
    last = start
    for i in range(start, end):
        if _content(lines[i]):
            last = i
    return last


def _insert_at(lines: list[str], start: int, end: int, child_indent: int) -> int:
    """After the last direct child's block (its last CONTENT line): notes that follow stay where they are."""
    last_child = None
    for i in range(start + 1, end):
        if _content(lines[i]) and _indent(lines[i]) == child_indent:
            last_child = i
    if last_child is None:
        return start + 1
    return _last_content(lines, last_child, _block_end(lines, last_child, child_indent)) + 1


def _no_descent(path: tuple[str, ...], depth: int) -> WriterError:
    return WriterError(
        f"«{'.'.join(path[: depth + 1])}» tiene su valor en la misma línea: se reemplaza completo, no por dentro."
    )


# --- the two edits --------------------------------------------------------------------

def _replace(lines: list[str], i: int, indent: int, key: str, value: Any, flow: bool) -> str:
    code, comment = _split_comment(lines[i])
    inline = _parse_line(lines[i])[1]
    stop = i + 1 if inline else _last_content(lines, i, _block_end(lines, i, indent)) + 1
    new = _render_leaf(key, value, flow, indent)
    if len(new) == 1 and comment:
        column = len(code)
        new[0] = (new[0].ljust(column) if len(new[0]) < column else new[0] + "  ") + comment
    lines[i:stop] = new
    return inline if inline else "<bloque>"


def _set(lines: list[str], path: tuple[str, ...], value: Any, flow: bool) -> tuple[str, str | None]:
    start, end, indent = -1, len(lines), -INDENT_STEP
    for depth, key in enumerate(path):
        child_indent = _child_indent(lines, start, end, indent)
        i = _find_child(lines, start, end, child_indent, key)
        if i is None:
            # A new TOP-LEVEL key lands just before `sites:` (the file's last and longest block), so
            # operator state stays readable; a deeper key goes after its parent's last child.
            at = _find_child(lines, start, end, child_indent, "sites") if depth == 0 else None
            if at is None:
                at = _insert_at(lines, start, end, child_indent)
            lines[at:at] = _render_path(path[depth:], value, flow, child_indent)
            return "inserted", None
        if depth == len(path) - 1:
            return "replaced", _replace(lines, i, child_indent, key, value, flow)
        if _parse_line(lines[i])[1]:
            raise _no_descent(path, depth)
        start, end, indent = i, _block_end(lines, i, child_indent), child_indent
    raise WriterError("ruta vacía: nada que escribir")


def _delete(lines: list[str], path: tuple[str, ...]) -> tuple[str, str | None]:
    start, end, indent = -1, len(lines), -INDENT_STEP
    ancestors: list[tuple[int, int]] = []          # (line index, own indent) of every parent on the path
    for depth, key in enumerate(path):
        child_indent = _child_indent(lines, start, end, indent)
        i = _find_child(lines, start, end, child_indent, key)
        if i is None:
            return "absent", None
        if depth == len(path) - 1:
            inline = _parse_line(lines[i])[1]
            stop = i + 1 if inline else _last_content(lines, i, _block_end(lines, i, child_indent)) + 1
            del lines[i:stop]
            # A parent mapping left without children goes too (absent-when-off), deepest first.
            for a_index, a_indent in reversed(ancestors):
                a_end = _block_end(lines, a_index, a_indent)
                if any(_content(lines[j]) for j in range(a_index + 1, a_end)):
                    break
                del lines[a_index]      # only the emptied parent's own line: notes below it stay put
            return "deleted", inline if inline else "<bloque>"
        if _parse_line(lines[i])[1]:
            raise _no_descent(path, depth)
        ancestors.append((i, child_indent))
        start, end, indent = i, _block_end(lines, i, child_indent), child_indent
    raise WriterError("ruta vacía: nada que borrar")


# --- the gates ------------------------------------------------------------------------

def _get(data: Any, path: tuple[str, ...]) -> Any:
    node = data
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return _MISSING
        node = node[key]
    return node


def _verify(data: Any, ops: list[Op]) -> None:
    """Read-back: each path's LAST op must show in the parsed result exactly as intended."""
    last: dict[tuple[str, ...], Op] = {}
    for op in ops:
        last[op.path] = op
    for path, op in last.items():
        got = _get(data, path)
        if op.kind == "delete":
            ok = got is _MISSING
        else:
            ok = got == yaml.safe_load(render_value(op.value))
        if not ok:
            raise WriterError(f"el cambio en «{'.'.join(path)}» no quedó donde debía: nada se escribió")


def _friendly(exc: ValidationError) -> str:
    parts: list[str] = []
    for err in exc.errors():
        where = ".".join(str(p) for p in err.get("loc", ()))
        msg = str(err.get("msg", "valor inválido")).removeprefix("Value error, ")
        parts.append(f"{where}: {msg}" if where else msg)
    return " | ".join(parts)


def _validate(data: Any) -> None:
    if not isinstance(data, dict):
        raise WriterError("config.yaml quedaría sin forma de mapa: nada se escribió")
    try:
        Config.model_validate(data)
    except ValidationError as exc:
        raise WriterError(_friendly(exc)) from None


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


# --- the public road ------------------------------------------------------------------

def apply_ops(ops: list[Op], *, config_path: Path = CONFIG_PATH) -> WriteResult:
    """Apply ops to config.yaml in ONE atomic write. The CALLER holds LOCK (write() does it for you).
    Raises WriterError (nothing written) on a bad path, a failed read-back or a model bounce."""
    previous = config_path.read_text(encoding="utf-8")
    lines = previous.split("\n")
    changes: list[dict[str, Any]] = []
    for op in ops:
        if op.kind == "set":
            action, old = _set(lines, op.path, op.value, op.flow)
            new: Any = yaml.safe_load(render_value(op.value))
        else:
            action, old = _delete(lines, op.path)
            new = None
        changes.append({"path": ".".join(op.path), "action": action, "old": old, "new": new})
    text = "\n".join(lines)
    if text == previous:
        return WriteResult(changes=changes, previous_text=previous, written=False)
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        raise WriterError("el archivo quedaría ilegible: nada se escribió") from None
    _verify(data, ops)
    _validate(data)
    _atomic_write(config_path, text)
    return WriteResult(changes=changes, previous_text=previous, written=True)


def restore(previous_text: str, *, config_path: Path = CONFIG_PATH) -> None:
    """Put a previous text back, atomically (the caller holds LOCK): the undo of an all-or-nothing write."""
    _atomic_write(config_path, previous_text)


async def write(ops: list[Op], *, config_path: Path = CONFIG_PATH) -> WriteResult:
    """The everyday road: LOCK, then apply_ops off the event loop."""
    async with LOCK:
        return await asyncio.to_thread(apply_ops, ops, config_path=config_path)


def write_textos_sync(overrides: dict[str, str], *, path: Path = TEXTOS_PATH) -> None:
    """The whole machine-owned textos.yaml (every template validated first); read back or undone. Caller holds LOCK."""
    text = textos_yaml(overrides)
    previous = path.read_text(encoding="utf-8") if path.exists() else None
    _atomic_write(path, text)
    if load_textos(path) != dict(overrides):
        if previous is not None:
            _atomic_write(path, previous)
        raise WriterError("textos.yaml no se leyó igual después de escribirlo: se dejó como estaba")


async def write_textos(overrides: dict[str, str], *, path: Path = TEXTOS_PATH) -> None:
    async with LOCK:
        await asyncio.to_thread(write_textos_sync, overrides, path=path)
