"""LPQ_VISION boundary shapes: the one house for every shape that crosses a boundary.

Config files -> typed objects (load_config, load_menu), the HTTP upload form
(UploadRequest) and the model's answer (LLMResponse). This module validates and
loads; it never touches the database, the network or the photo bytes.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

# The compose bind mount ./config:/app/config is the ONE place config lives inside a
# container, laptop and VPS alike. Named once here; every other module imports these.
CONFIG_DIR = Path("/app/config")
CONFIG_PATH = CONFIG_DIR / "config.yaml"
MENU_PATH = CONFIG_DIR / "menu.yaml"
PROMPTS_DIR = CONFIG_DIR / "prompts"

# Leftovers are reported in buckets of this size (SPEC section 4: named constant).
BUCKET_SIZE = 10

# dish_ids are the keys of the model's JSON answer, so they stay plain snake_case.
DISH_ID_RE = re.compile(r"^[a-z0-9_]+$")

Ingestion = Literal["phone_web", "pi_csi", "usb"]
Confidence = Literal["alta", "media", "baja"]
RecordType = Literal["return", "outgoing"]
PhotoCondition = Literal["normal", "lampara"]


# --------------------------------------------------------------------------- helpers

def _reject_yaml_magic(field: str, value: Any) -> Any:
    """Catch the classic YAML trap: `cache: on` parses as True, `cascade: off` as False.

    The system's rule is that such string values are QUOTED. A bool or None here means
    the quotes were dropped; failing with a message that names the fix beats silently
    running with a switch in the wrong position.
    """
    if isinstance(value, bool) or value is None:
        raise ValueError(
            f"'{field}' arrived as {value!r}: it must be a QUOTED string. Bare on/off/yes/no "
            f"parse as YAML booleans and none/null as null. Write {field}: \"off\" or "
            f"{field}: \"on\" with the quotes."
        )
    return value


def bucket(value: int) -> int:
    """Round a 0-100 percentage to the nearest BUCKET_SIZE, capped at 100.

    63 -> 60, 65 -> 70, 98 -> 100. Same answer in the contract's grammar: the model's
    honest estimate is kept, only expressed in the buckets the dataset speaks.
    """
    rounded = (value + BUCKET_SIZE // 2) // BUCKET_SIZE * BUCKET_SIZE
    return min(100, rounded)


# --------------------------------------------------------------------------- config.yaml

class SiteConfig(BaseModel):
    """One entry of the `sites:` block: how that site's photos arrive."""

    model_config = ConfigDict(extra="forbid")

    ingestion: Ingestion


class Config(BaseModel):
    """config/config.yaml: exact keys, absent-when-off, no other keys (extra='forbid')."""

    model_config = ConfigDict(extra="forbid")

    active_model: str = Field(min_length=1)
    cache: Literal["on", "off"]
    # The sealed three-position switch. Fase 1 has no cascade code, so only the OFF
    # position is accepted: any other value fails loudly instead of pretending to cascade.
    cascade: Literal["off"]
    raw_logging: bool
    brain_host: str = Field(min_length=1)
    poll_seconds: int = Field(ge=1)
    sites: dict[str, SiteConfig] = Field(min_length=1)

    @field_validator("cache", "cascade", mode="before")
    @classmethod
    def _quoted_strings(cls, value: Any, info: Any) -> Any:
        return _reject_yaml_magic(info.field_name, value)


# --------------------------------------------------------------------------- menu.yaml

class Componente(BaseModel):
    """One component of a dish: name (the JSON key the model will use) and portion."""

    model_config = ConfigDict(extra="forbid")

    nombre: str = Field(min_length=1)
    porcion_g: int = Field(gt=0)


class Dish(BaseModel):
    """Universal layer entry. Optional keys map 1:1 to menu_dishes columns; absent = off."""

    model_config = ConfigDict(extra="forbid")

    nombre: str = Field(min_length=1)
    componentes: list[Componente] = Field(min_length=1)
    plate_type: str | None = None
    contable: dict[str, Any] | None = None
    activo: bool = True

    @field_validator("componentes")
    @classmethod
    def _unique_component_names(cls, value: list[Componente]) -> list[Componente]:
        names = [c.nombre for c in value]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate component names: {dupes}")
        return value

    @property
    def component_names(self) -> list[str]:
        return [c.nombre for c in self.componentes]


class FotoRef(BaseModel):
    """One reference photo of a dish at a site, with its light condition."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    condition: PhotoCondition = "normal"


class SiteMenu(BaseModel):
    """Per-site layer: reference photos per dish_id. Empty = inherits brand."""

    model_config = ConfigDict(extra="forbid")

    fotos_ref: dict[str, list[FotoRef]] = Field(default_factory=dict)


class Menu(BaseModel):
    """config/menu.yaml: the universal `menu:` layer plus the per-site `sites:` layer."""

    model_config = ConfigDict(extra="forbid")

    menu: dict[str, Dish] = Field(min_length=1)
    sites: dict[str, SiteMenu] = Field(default_factory=dict)

    @field_validator("menu")
    @classmethod
    def _dish_ids_are_snake_case(cls, value: dict[str, Dish]) -> dict[str, Dish]:
        bad = [k for k in value if not DISH_ID_RE.match(k)]
        if bad:
            raise ValueError(
                f"dish_id keys must be lowercase snake_case (a-z, 0-9, _): {bad}"
            )
        return value

    @model_validator(mode="after")
    def _photos_point_to_known_dishes(self) -> "Menu":
        for site, site_menu in self.sites.items():
            unknown = [d for d in site_menu.fotos_ref if d not in self.menu]
            if unknown:
                raise ValueError(
                    f"sites.{site}.fotos_ref references dish_ids not in menu: {unknown}"
                )
        return self

    def active_dishes(self) -> dict[str, Dish]:
        return {k: d for k, d in self.menu.items() if d.activo}


# --------------------------------------------------------------------------- HTTP upload

class UploadRequest(BaseModel):
    """The form fields of POST /api/upload (the photo file is validated separately)."""

    model_config = ConfigDict(extra="forbid")

    site: str = Field(min_length=1)
    camera: str | None = None
    record_type: RecordType = "return"

    @field_validator("site", "camera", mode="before")
    @classmethod
    def _strip_and_blank_to_none(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            if value == "":
                return None
        return value


# --------------------------------------------------------------------------- LLM answer

class LLMResponse(BaseModel):
    """The model's answer to prompt_sonnet_v1: {dish, leftovers{component: 0-100}, confidence}.

    extra='ignore': a stray key is noise, not a parse failure; the three contract keys
    are what matter. Semantic checks (dish in menu, components known) belong to repair.py,
    which flags them as data; this model only guarantees the SHAPE.
    """

    model_config = ConfigDict(extra="ignore")

    dish: str = Field(min_length=1)
    leftovers: dict[str, int]
    confidence: Confidence

    @field_validator("dish", mode="before")
    @classmethod
    def _strip_dish(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("confidence", mode="before")
    @classmethod
    def _normalize_confidence(cls, value: Any) -> Any:
        # " Alta " is the same answer as "alta"; anything else still fails loudly.
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("leftovers")
    @classmethod
    def _percentages_in_buckets(cls, value: dict[str, int]) -> dict[str, int]:
        cleaned: dict[str, int] = {}
        for key, pct in value.items():
            if isinstance(pct, bool) or not 0 <= pct <= 100:
                raise ValueError(f"leftovers[{key!r}] = {pct!r}: must be an integer from 0 to 100")
            cleaned[key.strip()] = bucket(pct)
        return cleaned


# --------------------------------------------------------------------------- loaders

def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise FileNotFoundError(
            f"{path} not found: is ./config mounted at {CONFIG_DIR} in this container?"
        ) from None
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be a mapping, got {type(data).__name__}")
    return data


def load_config(path: Path = CONFIG_PATH) -> Config:
    """Read config.yaml into a Config. Called per job by the worker, at startup by api/init."""
    try:
        return Config.model_validate(_read_yaml_mapping(path))
    except ValidationError as exc:
        raise ValueError(f"{path} is invalid:\n{exc}") from None


def load_menu(path: Path = MENU_PATH) -> Menu:
    """Read menu.yaml into a Menu. Used by db.init to seed; the worker reads the DATABASE."""
    try:
        return Menu.model_validate(_read_yaml_mapping(path))
    except ValidationError as exc:
        raise ValueError(f"{path} is invalid:\n{exc}") from None
