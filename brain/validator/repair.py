"""The validator's repair path: parse -> strip -> one bounded re-ask -> flag.

A bad answer is data; a crash is a bug. Whatever the model says, this module returns a
row-ready answer: the parsed JSON when it is clean, the repaired JSON when fences or prose
had to be stripped, the re-asked JSON when a second call rescued it, and a flagged
'desconocido' row when nothing worked. Semantic mismatches (a dish not in the menu, a
component not in that dish) stay in the row AS DATA with their flag: the review corrects,
the flag points. The re-ask is injected as a callable so this module never imports the
adapter; the worker wires them.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import ValidationError

from brain.validator.models import LLMResponse
from brain.worker.menu_builder import MenuReference

UNKNOWN_DISH = "desconocido"          # the contract's own valid answer, never flagged

FLAG_UNPARSEABLE = "unparseable"
FLAG_DISH_NOT_IN_MENU = "dish_not_in_menu"
FLAG_UNKNOWN_COMPONENT = "unknown_component"

_FENCE_RE = re.compile(r"```[a-zA-Z]*")


@dataclass
class ValidatedAnswer:
    dish: str
    leftovers: dict[str, int]
    confidence: str
    repairs: int = 0
    flags: list[str] = field(default_factory=list)

    def validator_json(self) -> dict[str, Any]:
        """Absent-when-off: only what happened. {} means 'validated, nothing to report'."""
        out: dict[str, Any] = {}
        if self.repairs:
            out["repairs"] = self.repairs
        if self.flags:
            out["flags"] = list(self.flags)
        return out


def _try_parse(text: str) -> LLMResponse | None:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return LLMResponse.model_validate(data)
    except ValidationError:
        return None


def _strip(text: str) -> str | None:
    """Remove code fences and any prose around the outermost {...}; None if no object is there."""
    cleaned = _FENCE_RE.sub("", text)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return cleaned[start : end + 1].strip()


def parse(text: str) -> tuple[LLMResponse | None, bool]:
    """(answer, repaired): repaired is True only when a strip was needed AND succeeded."""
    direct = _try_parse(text.strip())
    if direct is not None:
        return direct, False
    stripped = _strip(text)
    if stripped is not None and stripped != text.strip():
        fixed = _try_parse(stripped)
        if fixed is not None:
            return fixed, True
    return None, False


def _semantic_flags(answer: LLMResponse, reference: MenuReference) -> list[str]:
    if answer.dish == UNKNOWN_DISH:
        return []
    known = reference.dishes.get(answer.dish)
    if known is None:
        return [FLAG_DISH_NOT_IN_MENU]
    if any(component not in known for component in answer.leftovers):
        return [FLAG_UNKNOWN_COMPONENT]
    return []


def validate(
    raw_text: str,
    reask: Callable[[str], str],
    reference: MenuReference,
) -> ValidatedAnswer:
    """Turn the model's raw text into a row-ready answer, using at most ONE re-ask.

    reask(bad_text) performs the bounded second call and returns its raw text.
    """
    repairs = 0
    answer, repaired = parse(raw_text)
    if repaired:
        repairs += 1

    if answer is None:
        repairs += 1
        answer, repaired = parse(reask(raw_text))
        if repaired:
            repairs += 1

    if answer is None:
        return ValidatedAnswer(
            dish=UNKNOWN_DISH,
            leftovers={},
            confidence="baja",
            repairs=repairs,
            flags=[FLAG_UNPARSEABLE],
        )

    return ValidatedAnswer(
        dish=answer.dish,
        leftovers=dict(answer.leftovers),
        confidence=answer.confidence,
        repairs=repairs,
        flags=_semantic_flags(answer, reference),
    )
