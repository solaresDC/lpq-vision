"""LPQ_VISION boundary shapes: the one house for every shape that crosses a boundary.

Config files -> typed objects (load_config, load_menu, load_prompt, load_presentation_prompt),
the HTTP bodies (UploadRequest, LoginRequest, VerifyRequest, BulkReviewRequest,
AlignStartRequest, CalibrationRequest) and the model's answers (LLMResponse,
PresentationResponse). This module validates and loads; it never touches the database,
the network or the photo bytes.
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

# --- Fase-2 vocabularies (SPEC 1.9, Arch section 3) -----------------------------------
CameraSource = Literal["phone", "pi"]
CameraRole = Literal["return", "outgoing", "both"]
KitchenEdge = Literal["top", "right", "bottom", "left"]
ReviewBulkAction = Literal["verify_as_is", "not_plate", "reopen"]

# --- presentation lane constants (SPEC section 4 / 1.7) --------------------------------
PRESENTATION_PASS_SCORE = 80
# The one-way lock: an outgoing plate that looks eaten/used gets this flag, score 0, pass false,
# and falls to review instead of a grade. The reverse direction does not exist.
PRESENTATION_FLAG_NOT_FRESH = "no_parece_recien_emplatado"


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


def _bucketed_percentages(value: dict[str, int]) -> dict[str, int]:
    """Shared by the model's leftovers and the human's leftovers_verified: 0-100 ints, bucketed."""
    cleaned: dict[str, int] = {}
    for key, pct in value.items():
        if isinstance(pct, bool) or not 0 <= pct <= 100:
            raise ValueError(f"leftovers[{key!r}] = {pct!r}: must be an integer from 0 to 100")
        cleaned[key.strip()] = bucket(pct)
    return cleaned


# --------------------------------------------------------------------------- the prompts

def prompt_version_of(path: Path) -> str:
    """Derive the version string from the prompt file name: prompt_sonnet_v1.txt -> 'v1'."""
    match = re.search(r"_(v\d+)$", path.stem)
    if not match:
        raise ValueError(
            f"{path.name}: prompt files must be named <name>_v<N>.txt so the version is derived"
        )
    return match.group(1)


# The active prompt file. Its `_vN` suffix IS the prompt version string (SPEC section 4),
# derived once here beside the loader. Choosing it becomes an admin knob in FASE 3.
ACTIVE_PROMPT_FILE = PROMPTS_DIR / "prompt_sonnet_v1.txt"
PROMPT_VERSION = prompt_version_of(ACTIVE_PROMPT_FILE)

# The presentation lane's own prompt (T-E5): same naming law, same derivation, its own version.
PRESENTATION_PROMPT_FILE = PROMPTS_DIR / "prompt_presentation_v1.txt"
PRESENTATION_PROMPT_VERSION = prompt_version_of(PRESENTATION_PROMPT_FILE)


def load_prompt(path: Path = ACTIVE_PROMPT_FILE) -> str:
    """Read the system prompt. Called per job by the adapter (config is read per job)."""
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"{path} is empty")
    return text


def load_presentation_prompt(path: Path = PRESENTATION_PROMPT_FILE) -> str:
    """Read the presentation lane's system prompt. Same rules as load_prompt."""
    return load_prompt(path)


# --------------------------------------------------------------------------- config.yaml

class CaptureConfig(BaseModel):
    """The phone guard's knobs (T-C1). Per-deployment reality, so they live in config, not code."""

    model_config = ConfigDict(extra="forbid")

    diff_threshold: int = Field(ge=1, le=100)      # % of pixels changed that counts as an arrival
    settle_frames: int = Field(ge=1)               # consecutive calm frames that confirm the landing
    burst_frames: int = Field(ge=1, le=10)         # frames per burst; the server keeps the sharpest


class AlertsConfig(BaseModel):
    """Thresholds of the bot's six alert families. Nothing temporal here (that is .env)."""

    model_config = ConfigDict(extra="forbid")

    sharpness_min: float = Field(gt=0)
    sharpness_n: int = Field(ge=1)
    sharpness_cooldown_min: int = Field(ge=0)
    backlog_threshold: int = Field(ge=1)
    worker_silence_min: int = Field(ge=1)
    poll_s: int = Field(ge=1)


class VideoAlignConfig(BaseModel):
    """The alignment mode's rope: a default and a hard cap, never 'forever'."""

    model_config = ConfigDict(extra="forbid")

    default_s: int = Field(ge=1)
    max_s: int = Field(ge=1)

    @model_validator(mode="after")
    def _default_within_cap(self) -> "VideoAlignConfig":
        if self.default_s > self.max_s:
            raise ValueError(
                f"video_align.default_s ({self.default_s}) exceeds max_s ({self.max_s}): "
                "the default rope cannot be longer than the cap"
            )
        return self


class Funciones(BaseModel):
    """The site's function switches (Arch section 3). Real booleans, bare in YAML."""

    model_config = ConfigDict(extra="forbid")

    merma: bool
    presentacion: bool


class CameraConfig(BaseModel):
    """One named camera of a site: where its frames come from, which stream(s) it labels,
    and, ONLY once calibrated, which edge of its frame is the kitchen side."""

    model_config = ConfigDict(extra="forbid")

    source: CameraSource
    role: CameraRole
    kitchen_edge: KitchenEdge | None = None       # absent-when-off: written by the alignment mode

    @model_validator(mode="after")
    def _edge_only_for_both(self) -> "CameraConfig":
        # A single-role camera never looks at direction, so a stored edge would be dead config.
        if self.kitchen_edge is not None and self.role != "both":
            raise ValueError(
                f"kitchen_edge is set on a camera with role {self.role!r}: it only applies to role 'both'"
            )
        return self


class SiteConfig(BaseModel):
    """One entry of the `sites:` block: how that site's photos arrive, its functions, its cameras."""

    model_config = ConfigDict(extra="forbid")

    ingestion: Ingestion
    funciones: Funciones
    cameras: dict[str, CameraConfig] = Field(default_factory=dict)


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
    capture: CaptureConfig
    alerts: AlertsConfig
    video_align: VideoAlignConfig
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
    """The form fields of POST /api/upload and /api/upload_burst (the photo files are validated separately)."""

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


# --------------------------------------------------------------------------- HTTP bodies (Fase 2)

class LoginRequest(BaseModel):
    """POST /api/login. The password is compared against a salted hash and never stored or logged."""

    model_config = ConfigDict(extra="forbid")

    user: str = Field(min_length=1)
    password: str = Field(min_length=1)


class VerifyRequest(BaseModel):
    """POST /api/review/{plate_id}/verify: the one-by-one correction.

    leftovers_verified omitted = the model was right: the row stores NULL (SPEC 1.2), so the
    training export reads COALESCE(leftovers_verified, leftovers).
    """

    model_config = ConfigDict(extra="forbid")

    dish_verified: str = Field(min_length=1)
    leftovers_verified: dict[str, int] | None = None

    @field_validator("dish_verified", mode="before")
    @classmethod
    def _strip_dish(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("leftovers_verified")
    @classmethod
    def _bucketed(cls, value: dict[str, int] | None) -> dict[str, int] | None:
        return None if value is None else _bucketed_percentages(value)


class BulkReviewRequest(BaseModel):
    """POST /api/review/bulk: one action over a multi-selection. Correcting is one by one (Arch section 9)."""

    model_config = ConfigDict(extra="forbid")

    ids: list[int] = Field(min_length=1)
    action: ReviewBulkAction


class AlignStartRequest(BaseModel):
    """POST /api/camera/align/start: the rope in minutes; the route converts and caps it at video_align.max_s."""

    model_config = ConfigDict(extra="forbid")

    site: str = Field(min_length=1)
    camera: str = Field(min_length=1)
    minutes: int | None = Field(default=None, ge=1)   # absent = video_align.default_s


class AlignStopRequest(BaseModel):
    """POST /api/camera/align/stop."""

    model_config = ConfigDict(extra="forbid")

    site: str = Field(min_length=1)
    camera: str = Field(min_length=1)


class CalibrationRequest(BaseModel):
    """POST /api/camera/calibration: the ONE config write before FASE 3 (kitchen_edge)."""

    model_config = ConfigDict(extra="forbid")

    site: str = Field(min_length=1)
    camera: str = Field(min_length=1)
    kitchen_edge: KitchenEdge


# --------------------------------------------------------------------------- LLM answers

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
        return _bucketed_percentages(value)


class PresentationResponse(BaseModel):
    """The model's answer to prompt_presentation_v1 (SPEC 1.7):
    {dish, score 0-100, pass, issues[], flags[]}.

    The model answers `pass`, but the VALIDATOR recomputes it from the score against
    PRESENTATION_PASS_SCORE and overwrites it: one source of truth. The one-way lock is
    enforced here too: the not-fresh flag forces score 0 and pass false, whatever the model
    said beside it. `pass` is a Python keyword, hence the alias.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    dish: str = Field(min_length=1)
    score: int = Field(ge=0, le=100)
    passed: bool = Field(alias="pass")
    issues: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)

    @field_validator("dish", mode="before")
    @classmethod
    def _strip_dish(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("issues", "flags", mode="before")
    @classmethod
    def _clean_strings(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        return value

    @model_validator(mode="after")
    def _recompute_pass_and_lock(self) -> "PresentationResponse":
        if PRESENTATION_FLAG_NOT_FRESH in self.flags:
            self.score = 0
            self.passed = False
            return self
        self.passed = self.score >= PRESENTATION_PASS_SCORE
        return self

    def presentation_json(self) -> dict[str, Any]:
        """The row's plates.presentation value: absent-when-off (flags only when present)."""
        out: dict[str, Any] = {"score": self.score, "pass": self.passed, "issues": list(self.issues)}
        if self.flags:
            out["flags"] = list(self.flags)
        return out


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
