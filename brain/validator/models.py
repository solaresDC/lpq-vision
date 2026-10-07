"""LPQ_VISION boundary shapes: the one house for every shape that crosses a boundary.

Config files -> typed objects (load_config, load_menu, load_prompt, load_presentation_prompt),
the HTTP bodies (UploadRequest, LoginRequest, VerifyRequest, BulkReviewRequest,
AlignStartRequest, CalibrationRequest, and from Fase 3 every body of the two admin floors) and
the model's answers (LLMResponse, PresentationResponse). This module validates and loads; it
never touches the database, the network or the photo bytes.

Fase 3 adds: the config shapes of the admin era (timezone, horario, the machine block, prices,
the site layer with mantenimiento and overrides), each with a factory default so an older live
config.yaml loads as it is; the two prompt lanes; and the ONE shared contender table (the
bake-off and the machine room's keyless selector both read it here). Site overrides are checked
against the knob registry (brain/validator/knobs.py) at load.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
ReviewBulkAction = Literal["verify_as_is", "not_plate", "reopen", "discard"]   # 'discard' = the F3 papelera

# --- Fase-3 vocabularies (SPEC 1.3, 1.6) ------------------------------------------------
Role = Literal["admin", "manager", "machine"]
AccountRole = Literal["admin", "manager"]              # what the users CRUD may create; 'machine' is the cuarto row
Lane = Literal["merma", "presentacion"]
RestartService = Literal["fastapi", "worker", "bot"]   # postgres never has a button

# --- Fase-3 constants (SPEC section 4); the absent-key defaults live HERE, one source ------
FRAME_POLL_S_DEFAULT = 5          # push cadence when capture.frame_poll_s is absent (api.routes.FRAME_POLL_S)
RAW_LOG_AUTO_OFF_HOURS = 24       # default life of the forensic river when switched on from the mostrador
WORKER_SCALE_MAX = 4              # the butler's `scale worker <1..4>` ceiling
DEFAULT_TIMEZONE = "America/Mexico_City"
MACHINE_USER = "cuarto"           # the reserved users row whose hash IS the machine floor
RESERVED_SITE_SLUGS = ("reference", "brand")   # the photos root's reference/ folder and the brand album
PASSWORD_MIN_CHARS = 10           # new passwords only; an existing one is checked, never length-judged
USER_RECOVERY_DAYS = 30           # a deleted account stays recoverable this long, then the worker erases it (owner ruling 3.9.2)
USER_ERASE_WARN_DAYS = 5          # the admin chat hears about the erase this many days before it happens

SITE_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{1,23}\Z")
USER_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,31}\Z")
CAMERA_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}\Z")
CHAT_ID_RE = re.compile(r"^-?\d{1,20}\Z")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d\Z")

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


def check_timezone(name: str) -> str:
    """An IANA timezone name the containers' OS tzdata knows (gate D proved it ships in the base image)."""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"unknown timezone {name!r}: use an IANA name like {DEFAULT_TIMEZONE}") from None
    return name


# --------------------------------------------------------------------------- the prompts

def prompt_version_of(path: Path) -> str:
    """Derive the version string from the prompt file name: prompt_sonnet_v1.txt -> 'v1'."""
    match = re.search(r"_(v\d+)$", path.stem)
    if not match:
        raise ValueError(
            f"{path.name}: prompt files must be named <name>_v<N>.txt so the version is derived"
        )
    return match.group(1)


# The lanes' DEFAULT prompt files. Their `_vN` suffix IS the prompt version string (SPEC section 4).
# From Fase 3, config's active_prompt / active_presentation_prompt may select another file of the
# SAME lane (the mostrador's selectors); absent = these defaults.
ACTIVE_PROMPT_FILE = PROMPTS_DIR / "prompt_sonnet_v1.txt"
PROMPT_VERSION = prompt_version_of(ACTIVE_PROMPT_FILE)

# The presentation lane's own prompt (T-E5): same naming law, same derivation, its own version.
PRESENTATION_PROMPT_FILE = PROMPTS_DIR / "prompt_presentation_v1.txt"
PRESENTATION_PROMPT_VERSION = prompt_version_of(PRESENTATION_PROMPT_FILE)

# A lane is its file prefix; N has no leading zero, so two files of one lane can never share a
# version string (the SPEC's duplicated-vN refusal holds by construction).
PROMPT_LANE_PREFIX: dict[str, str] = {"merma": "prompt_sonnet_", "presentacion": "prompt_presentation_"}
LANE_DEFAULT_FILE: dict[str, Path] = {"merma": ACTIVE_PROMPT_FILE, "presentacion": PRESENTATION_PROMPT_FILE}


def _lane_re(lane: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(PROMPT_LANE_PREFIX[lane])}v[1-9]\d*\.txt\Z")


def lane_accepts(lane: str, filename: str) -> bool:
    """True when `filename` is a prompt file of that lane (merma can never point at a presentation prompt)."""
    return bool(_lane_re(lane).match(filename))


def prompt_files(lane: str, directory: Path = PROMPTS_DIR) -> list[Path]:
    """The prompt files of one lane that EXIST, oldest version first (exactly what a selector lists)."""
    pattern = _lane_re(lane)
    found = [p for p in directory.glob("*.txt") if pattern.match(p.name)]
    return sorted(found, key=lambda p: int(prompt_version_of(p)[1:]))


def prompt_file_for(cfg: Config, lane: str) -> Path:
    """The SELECTED prompt of a lane: the config's choice when present, else the lane's default file."""
    chosen = cfg.active_prompt if lane == "merma" else cfg.active_presentation_prompt
    return PROMPTS_DIR / chosen if chosen else LANE_DEFAULT_FILE[lane]


def prompt_version_for(cfg: Config, lane: str) -> str:
    """The version stamped at insert (plan of record) and written by the worker (truth) for that lane."""
    return prompt_version_of(prompt_file_for(cfg, lane))


def _lane_prompt(lane: str, value: str | None) -> str | None:
    if value is not None and not lane_accepts(lane, value):
        raise ValueError(
            f"{value!r} is not a {lane} prompt file (expected {PROMPT_LANE_PREFIX[lane]}v<N>.txt)"
        )
    return value


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
    frame_poll_s: int = Field(default=FRAME_POLL_S_DEFAULT, ge=1, le=60)   # F3: seconds between mirilla copies


class AlertsConfig(BaseModel):
    """Thresholds of the bot's alert families and vigilantes. Nothing temporal here."""

    model_config = ConfigDict(extra="forbid")

    sharpness_min: float = Field(gt=0)
    sharpness_n: int = Field(ge=1)
    sharpness_cooldown_min: int = Field(ge=0)
    backlog_threshold: int = Field(ge=1)
    worker_silence_min: int = Field(ge=1)
    poll_s: int = Field(ge=1)
    black_luma_max: float = Field(default=12.0, gt=0, le=255)      # F3: mean brightness under which a frame is black
    black_timer_s: int = Field(default=120, ge=1, le=86400)        # F3: seconds of black inside the horario
    silent_site_min: int = Field(default=5, ge=1, le=1440)         # F3: minutes without frames inside the horario


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


class HorarioConfig(BaseModel):
    """Service hours, judged in the site's timezone. close <= open means the window ends the NEXT day
    (a café closing at 00:30 just works: the midnight law, SPEC 1.8)."""

    model_config = ConfigDict(extra="forbid")

    open: str
    close: str

    @field_validator("open", "close", mode="before")
    @classmethod
    def _hhmm(cls, value: Any, info: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, str):
            raise ValueError(
                f"{info.field_name} arrived as {value!r}: write the time QUOTED, like \"22:00\" "
                "(a bare 22:00 is the YAML number 1320)"
            )
        value = value.strip()
        if not TIME_RE.match(value):
            raise ValueError(f"{info.field_name} {value!r} must be HH:MM on a 24-hour clock, like \"07:00\"")
        return value

    @model_validator(mode="after")
    def _not_equal(self) -> "HorarioConfig":
        if self.open == self.close:
            raise ValueError(f"horario: open and close are both {self.open}: a zero or 24-hour window is ambiguous")
        return self


class MachineConfig(BaseModel):
    """The machine room's block (SPEC 1.4, edited only on the second floor). Absent = these defaults:
    the one home of the old queue/adapter constants' values (loop.py and llm.py keep the names)."""

    model_config = ConfigDict(extra="forbid")

    session_hours: int = Field(default=720, ge=1, le=8760)
    workers: int = Field(default=1, ge=1, le=WORKER_SCALE_MAX)   # the REMEMBERED desire: one number for the chain
    orphan_timeout_min: int = Field(default=10, ge=2, le=120)
    max_attempts: int = Field(default=3, ge=1, le=10)
    llm_timeout_s: int = Field(default=120, ge=10, le=600)
    max_tokens: int = Field(default=1024, ge=256, le=8192)

    @model_validator(mode="after")
    def _coherent(self) -> "MachineConfig":
        # The Fase-2 anti-stupid assert, now at LOAD: a call slower than the rescue window would be
        # double-claimed by a live worker. This message is shown verbatim on the machine room's screen.
        if self.llm_timeout_s >= self.orphan_timeout_min * 60:
            raise ValueError(
                f"llm_timeout_s ({self.llm_timeout_s} s) debe ser menor que orphan_timeout_min "
                f"({self.orphan_timeout_min} min = {self.orphan_timeout_min * 60} s): una llamada más lenta que "
                "la ventana de rescate sería tomada dos veces por otro analista. Baja llm_timeout_s o sube "
                "orphan_timeout_min."
            )
        return self


MACHINE_DEFAULTS = MachineConfig()


class PriceConfig(BaseModel):
    """One model's price in USD per million tokens. Prices ship EMPTY: a human enters them after
    verifying at the source (rule 1). An absent model shows "sin precio", never a guess."""

    model_config = ConfigDict(extra="forbid")

    in_mtok: float = Field(ge=0)
    out_mtok: float = Field(ge=0)
    cache_write_mtok: float = Field(ge=0)
    cache_read_mtok: float = Field(ge=0)


class SiteConfig(BaseModel):
    """One entry of the `sites:` block: how that site's photos arrive, its functions, its cameras, and
    the Fase-3 site layer: mantenimiento (absent-when-off) and overrides (the knob cascade's middle
    layer, one flat line per registry name, validated against brain.validator.knobs by Config)."""

    model_config = ConfigDict(extra="forbid")

    ingestion: Ingestion
    funciones: Funciones
    cameras: dict[str, CameraConfig] = Field(default_factory=dict)
    mantenimiento: bool | None = None
    overrides: dict[str, Any] = Field(default_factory=dict)

    @field_validator("mantenimiento")
    @classmethod
    def _mantenimiento_only_true(cls, value: bool | None) -> bool | None:
        if value is False:
            raise ValueError("mantenimiento: false is dead config (absent-when-off): delete the key instead")
        return value

    @field_validator("overrides", mode="before")
    @classmethod
    def _overrides_none_is_empty(cls, value: Any) -> Any:
        return {} if value is None else value


class Config(BaseModel):
    """config/config.yaml: exact keys, absent-when-off, no other keys (extra='forbid').
    Every Fase-3 key carries a factory default, so an older live file loads as it is."""

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
    # --- Fase 3 (SPEC 1.4) ---
    timezone: str = DEFAULT_TIMEZONE
    horario_default: HorarioConfig = Field(default_factory=lambda: HorarioConfig(open="07:00", close="22:00"))
    confianza_minima: Confidence = "media"
    papelera_dias: int = Field(default=10, ge=1, le=365)
    prices: dict[str, PriceConfig] = Field(default_factory=dict)
    raw_logging_off_at: datetime | None = None          # absent-when-off: only while raw_logging is on
    admin_chat_id: str | None = None                    # absent-when-off: the machine families' own chat
    queue_paused: bool | None = None                    # absent-when-off: only `true` ever exists
    active_prompt: str | None = None                    # absent = the merma lane's default file
    active_presentation_prompt: str | None = None       # absent = the presentation lane's default file
    machine: MachineConfig = Field(default_factory=MachineConfig)

    @field_validator("cache", "cascade", mode="before")
    @classmethod
    def _quoted_strings(cls, value: Any, info: Any) -> Any:
        return _reject_yaml_magic(info.field_name, value)

    @field_validator("timezone")
    @classmethod
    def _zone(cls, value: str) -> str:
        return check_timezone(value)

    @field_validator("raw_logging_off_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("raw_logging_off_at needs an offset, like \"2026-10-01T09:00:00+00:00\"")
        return value

    @field_validator("admin_chat_id", mode="before")
    @classmethod
    def _chat_id(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError("admin_chat_id must be a QUOTED Telegram chat id")
        value = str(value).strip()
        if not CHAT_ID_RE.match(value):
            raise ValueError(f"admin_chat_id {value!r} must be a Telegram chat id like \"-1001234567890\"")
        return value

    @field_validator("queue_paused")
    @classmethod
    def _paused_only_true(cls, value: bool | None) -> bool | None:
        if value is False:
            raise ValueError("queue_paused: false is dead config (absent-when-off): delete the key instead")
        return value

    @field_validator("active_prompt")
    @classmethod
    def _merma_prompt(cls, value: str | None) -> str | None:
        return _lane_prompt("merma", value)

    @field_validator("active_presentation_prompt")
    @classmethod
    def _presentation_prompt(cls, value: str | None) -> str | None:
        return _lane_prompt("presentacion", value)

    @model_validator(mode="after")
    def _fase3_coherence(self) -> "Config":
        if self.raw_logging_off_at is not None and not self.raw_logging:
            raise ValueError("raw_logging_off_at is set while raw_logging is false: dead config (absent-when-off)")
        # The knob cascade's middle layer: every override must be a per-site registry knob with a value
        # the registry accepts. Imported here, never at the top: knobs.py imports this module.
        from brain.validator import knobs

        knobs.validate_overrides(self)
        return self


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
    """POST /api/camera/calibration: the kitchen_edge write (through the one writer from Fase 3)."""

    model_config = ConfigDict(extra="forbid")

    site: str = Field(min_length=1)
    camera: str = Field(min_length=1)
    kitchen_edge: KitchenEdge


# --------------------------------------------------------------------------- HTTP bodies (Fase 3)
# Messages here are shown to the person on the mostrador or the machine room: Spanish, plain.

def _account_name(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        if not USER_RE.match(value):
            raise ValueError("usuario: minúsculas, números, punto, guion o guion bajo; empieza con letra (2 a 32)")
        if value == MACHINE_USER:
            raise ValueError(f"«{MACHINE_USER}» está reservado para el cuarto de máquinas")
    return value


def _site_slug(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        if not SITE_SLUG_RE.match(value):
            raise ValueError("sitio: minúsculas, números, guion o guion bajo; empieza con letra (2 a 24)")
        if value in RESERVED_SITE_SLUGS:
            raise ValueError(f"«{value}» es un nombre reservado del sistema: elige otro")
    return value


def _role_site(role: str, site: str | None) -> None:
    if role == "manager" and not site:
        raise ValueError("un manager necesita su sitio")
    if role == "admin" and site:
        raise ValueError("un admin ve todos los sitios: no lleva sitio")


def _component_list(value: list[Componente]) -> list[Componente]:
    names = [c.nombre for c in value]
    bad = [n for n in names if not DISH_ID_RE.match(n)]
    if bad:
        raise ValueError(f"componentes en minúsculas_con_guion_bajo (son las llaves del JSON del modelo): {bad}")
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ValueError(f"componentes repetidos: {dupes}")
    return value


class _Body(BaseModel):
    """Every Fase-3 request body: exact keys, nothing else."""

    model_config = ConfigDict(extra="forbid")


class SwitchRequest(_Body):
    on: bool


class IdsRequest(_Body):
    ids: list[int] = Field(min_length=1, max_length=500)


class PasswordRequest(_Body):
    """A NEW password (set, reset, the cuarto's birth or change): typed by a human, hashed server-side."""

    password: str = Field(min_length=PASSWORD_MIN_CHARS, max_length=200)


class UnlockRequest(_Body):
    """POST /api/admin/machine/unlock: an EXISTING password is checked, never length-judged."""

    password: str = Field(min_length=1, max_length=200)


class ConfirmRequest(_Body):
    """Step 2 of every dangerous action: the pending_id step 1 returned with its diff."""

    pending_id: str = Field(min_length=8, max_length=64)


class ConfirmNameRequest(_Body):
    """Deleting an account: the exact name the person typed (the page checks it, and so does the server)."""

    name: str = Field(min_length=1, max_length=64)


class OwnPasswordRequest(_Body):
    """Mi cuenta: the CURRENT password (checked, never length-judged) and the new one (typed twice on the page)."""

    current: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=PASSWORD_MIN_CHARS, max_length=200)


class EnrollRequest(_Body):
    """POST /api/device/enroll: /captura presents the site's gafete once; the server sets the device cookie."""

    site: str = Field(min_length=1)
    key: str = Field(min_length=16, max_length=200)


class KnobWriteRequest(_Body):
    knob: str = Field(min_length=1)
    value: Any
    site: str | None = None       # absent = the global value; present = that site's override


class KnobResetRequest(_Body):
    """"Volver al global": deletes that site's override line."""

    knob: str = Field(min_length=1)
    site: str = Field(min_length=1)


class KnobNameRequest(_Body):
    knob: str = Field(min_length=1)


class RawLoggingRequest(_Body):
    on: bool
    hours: int | None = Field(default=None, ge=1, le=720)   # absent = RAW_LOG_AUTO_OFF_HOURS


class ChatIdRequest(_Body):
    chat_id: str | None = None    # empty or absent = remove the chat (absent-when-off)

    @field_validator("chat_id", mode="before")
    @classmethod
    def _chat(cls, value: Any) -> Any:
        if value is None or isinstance(value, bool):
            return None if value is None else value
        value = str(value).strip()
        if value == "":
            return None
        if not CHAT_ID_RE.match(value):
            raise ValueError("el chat es un número de Telegram, como -1001234567890 (escribe /id en el grupo)")
        return value


class TelegramTestRequest(_Body):
    site: str | None = None       # absent = the admin chat


class TextoSaveRequest(_Body):
    key: str = Field(min_length=1)
    text: str = Field(min_length=1)


class TextoKeyRequest(_Body):
    key: str = Field(min_length=1)


class TextoTestRequest(_Body):
    key: str = Field(min_length=1)
    text: str | None = None       # present = preview an unsaved edit; absent = the saved text
    site: str | None = None       # absent = the admin chat


class UserCreateRequest(_Body):
    usuario: str
    password: str = Field(min_length=PASSWORD_MIN_CHARS, max_length=200)
    role: AccountRole
    site: str | None = None

    @field_validator("usuario", mode="before")
    @classmethod
    def _name(cls, value: Any) -> Any:
        return _account_name(value)

    @model_validator(mode="after")
    def _scope(self) -> "UserCreateRequest":
        _role_site(self.role, self.site)
        return self


class UserRoleRequest(_Body):
    role: AccountRole
    site: str | None = None

    @model_validator(mode="after")
    def _scope(self) -> "UserRoleRequest":
        _role_site(self.role, self.site)
        return self


class CameraUpsertRequest(_Body):
    name: str
    source: CameraSource
    role: CameraRole

    @field_validator("name", mode="before")
    @classmethod
    def _camera_name(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            if not CAMERA_RE.match(value):
                raise ValueError("cámara: minúsculas, números, guion o guion bajo (1 a 24), como barra-1")
        return value


class SiteCreateRequest(_Body):
    """Crear restaurante, stage 0 (SPEC 1.5): site, cameras, functions, menu clone and its manager,
    born together in one all-or-nothing write (the gafete is born inside it too)."""

    site: str
    ingestion: Ingestion = "phone_web"
    funciones: Funciones
    cameras: list[CameraUpsertRequest] = Field(min_length=1, max_length=8)
    clone_menu_from: str | None = None
    manager_usuario: str
    manager_password: str = Field(min_length=PASSWORD_MIN_CHARS, max_length=200)

    @field_validator("site", mode="before")
    @classmethod
    def _slug(cls, value: Any) -> Any:
        return _site_slug(value)

    @field_validator("manager_usuario", mode="before")
    @classmethod
    def _manager(cls, value: Any) -> Any:
        return _account_name(value)

    @field_validator("cameras")
    @classmethod
    def _unique_cameras(cls, value: list[CameraUpsertRequest]) -> list[CameraUpsertRequest]:
        names = [c.name for c in value]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"cámaras repetidas: {dupes}")
        return value


class SiteEditRequest(_Body):
    ingestion: Ingestion | None = None
    active: bool | None = None

    @model_validator(mode="after")
    def _something(self) -> "SiteEditRequest":
        if self.ingestion is None and self.active is None:
            raise ValueError("nada que cambiar")
        return self


class SourceRequest(_Body):
    """The relevo: a camera's source flips (mostrador switch or /captura's emergency road)."""

    source: CameraSource


class DishCreateRequest(_Body):
    """CREAR PLATILLO: born at the current MAX menu_version; its photos arrive through the uploader."""

    dish_id: str
    nombre: str = Field(min_length=1, max_length=80)
    componentes: list[Componente] = Field(min_length=1, max_length=20)
    plate_type: str | None = None

    @field_validator("dish_id", mode="before")
    @classmethod
    def _dish_id(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            if not DISH_ID_RE.match(value):
                raise ValueError("dish_id en minúsculas_con_guion_bajo, como tartine_aguacate")
        return value

    @field_validator("componentes")
    @classmethod
    def _components(cls, value: list[Componente]) -> list[Componente]:
        return _component_list(value)


class DishEditRequest(_Body):
    """The full new definition; a change in the SET of component names bumps to global MAX+1."""

    nombre: str = Field(min_length=1, max_length=80)
    componentes: list[Componente] = Field(min_length=1, max_length=20)
    plate_type: str | None = None

    @field_validator("componentes")
    @classmethod
    def _components(cls, value: list[Componente]) -> list[Componente]:
        return _component_list(value)


class RefPhotoRequest(_Body):
    """A reference photo action: upload's form fields, replace and delete (photo_path names the row)."""

    site: str = Field(min_length=1)
    dish_id: str = Field(min_length=1)
    photo_path: str | None = None
    condition: PhotoCondition = "normal"


class RetryRequest(_Body):
    site: str | None = None       # a manager is forced to its own site server-side


class PriceRequest(_Body):
    model: str = Field(min_length=1, max_length=120)
    in_mtok: float = Field(ge=0)
    out_mtok: float = Field(ge=0)
    cache_write_mtok: float = Field(ge=0)
    cache_read_mtok: float = Field(ge=0)


class ModelNameRequest(_Body):
    model: str = Field(min_length=1, max_length=120)


class WorkersRequest(_Body):
    count: int = Field(ge=1, le=WORKER_SCALE_MAX)


class RestartRequest(_Body):
    service: RestartService


# --------------------------------------------------------------------------- the contenders

@dataclass(frozen=True)
class Contender:
    """One exam string of the bake-off and one selectable active_model: the lane, the LiteLLM
    model string, and the env key that unlocks it (SPEC 1.6: this is its ONE shared home).

    Rule 1: strings and prices are verified at the source the day their key exists. The two
    Anthropic strings are verified; the four others carry the names the SPEC gives them and stay
    unverified until their keys are pasted (without a key they are skipped and never listed).
    """

    lane: str            # 'alto' | 'bajo'
    label: str
    model: str
    env_key: str


CONTENDERS: tuple[Contender, ...] = (
    Contender("alto", "Sonnet 5", "claude-sonnet-5", "ANTHROPIC_API_KEY"),
    Contender("alto", "Terra", "terra", "TERRA_API_KEY"),                              # verify when its key exists
    Contender("alto", "Gemini 3.1 Pro", "gemini/gemini-3.1-pro", "GEMINI_API_KEY"),    # verify when its key exists
    Contender("bajo", "Haiku 4.5", "claude-haiku-4-5-20251001", "ANTHROPIC_API_KEY"),
    Contender("bajo", "Luna", "luna", "LUNA_API_KEY"),                                 # verify when its key exists
    Contender("bajo", "Gemini 3.6 Flash", "gemini/gemini-3.6-flash", "GEMINI_API_KEY"),  # verify when its key exists
)


def keyed_contenders(environ: Mapping[str, str] | None = None) -> list[Contender]:
    """The contenders whose provider key is present and non-empty: a keyless model never appears."""
    env = os.environ if environ is None else environ
    return [c for c in CONTENDERS if (env.get(c.env_key) or "").strip()]


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
