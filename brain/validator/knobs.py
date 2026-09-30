"""LPQ_VISION brain/validator/knobs.py: the knob registry, the whitelist the admin writes through (SPEC 1.4).

One table drives everything. The mostrador and the machine room render their fields from it, each with
the THREE HELPS (what it is, a copyable example, the factory recommendation); the save endpoints validate
against it; and the knob cascade resolves through it:

    global default  ->  sites.<site>.overrides.<knob>  ->  session layer (the align rope, the one-night extension)

A knob that is not in REGISTRY cannot be edited from any page: the whitelist IS the security model.
Changing a global never touches an override; "volver al global" deletes one override line; "borrar
overrides (N)" deletes one knob's overrides in every site. Deliberately outside the registry (frozen by
design): poll_seconds, video_align.max_s, brain_host, and cascade (the MODEL cascade, sealed "off").

Pure code: it reads a loaded Config, never the file, the database or the network. Its messages are shown
to people on the two floors, so they are Spanish and end with the knob's recommendation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from brain.validator.models import (
    CHAT_ID_RE,
    WORKER_SCALE_MAX,
    Config,
    HorarioConfig,
    check_timezone,
    keyed_contenders,
    prompt_files,
)

Kind = Literal[
    "int", "float", "choice", "switch", "pause", "horario", "timezone",
    "chat_id", "prompt_merma", "prompt_presentacion", "model", "prices",
]
Floor = Literal["mostrador", "maquinas"]


@dataclass(frozen=True)
class Knob:
    """One editable value: where its GLOBAL home lives, how it is validated, and its three helps."""

    name: str                      # the registry name = the flat override key under sites.<site>.overrides
    path: tuple[str, ...]          # the global value's path in config.yaml
    kind: Kind
    seccion: str                   # the page section that renders it
    que_es: str                    # help 1: one line, what it is
    ejemplo: str                   # help 2: a copyable example (also the field's gray placeholder)
    recomendacion: str             # help 3: the factory recommendation, always visible
    per_site: bool = False         # True = the cascade applies (sites may override it)
    floor: Floor = "mostrador"
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    optional: bool = False         # absent-when-off: the global may simply not exist (None)
    special: bool = False          # written by its own endpoint (side effects or a map), never the generic set


class KnobError(ValueError):
    """A value the registry refuses; the message names the knob, the rule and the recommendation."""


def _k(name: str, kind: Kind, seccion: str, que_es: str, ejemplo: str, recomendacion: str,
       *, path: str | None = None, **extra: Any) -> Knob:
    return Knob(name=name, path=tuple((path or name).split(".")), kind=kind, seccion=seccion,
                que_es=que_es, ejemplo=ejemplo, recomendacion=recomendacion, **extra)


REGISTRY: tuple[Knob, ...] = (
    # --- mostrador: analysis ---
    _k("cache", "choice", "analisis",
       "Reutiliza el menú ya enviado a Anthropic en llamadas seguidas: cuesta menos y no cambia el resultado.",
       "on", "on", choices=("on", "off")),
    _k("active_prompt", "prompt_merma", "analisis",
       "La versión de instrucciones del analista de merma. Solo aparecen los archivos que existen.",
       "prompt_sonnet_v1.txt", "La de fábrica (prompt_sonnet_v1.txt) hasta que exista una versión nueva probada.",
       optional=True),
    _k("active_presentation_prompt", "prompt_presentacion", "analisis",
       "La versión de instrucciones del juez de emplatado (los platos que salen).",
       "prompt_presentation_v1.txt", "La de fábrica (prompt_presentation_v1.txt).", optional=True),
    _k("confianza_minima", "choice", "analisis",
       "El panel y la revisión marcan los platos con confianza por debajo de este nivel.",
       "media", "media: marca los dudosos sin llenar la revisión.", choices=("alta", "media", "baja"), per_site=True),
    # --- mostrador: capture ---
    _k("capture.frame_poll_s", "int", "captura",
       "Cada cuántos segundos la página de captura manda una copia a la mirilla.",
       "5", "5 s: la mirilla se siente viva sin gastar red.", minimum=1, maximum=60),
    _k("capture.diff_threshold", "int", "captura",
       "Qué porcentaje de la imagen debe cambiar para que la captura crea que llegó un plato.",
       "12", "12: súbelo si capta sombras o manos; bájalo si se le escapan platos.",
       minimum=1, maximum=100, per_site=True),
    _k("capture.settle_frames", "int", "captura",
       "Cuántos cuadros quietos seguidos confirman que el plato ya se asentó.",
       "4", "4 (menos de medio segundo).", minimum=1, maximum=30, per_site=True),
    _k("capture.burst_frames", "int", "captura",
       "Cuántas fotos toma la ráfaga; el servidor se queda con la más nítida.",
       "3", "3: suficiente para esquivar una foto movida.", minimum=1, maximum=10, per_site=True),
    _k("video_align.default_s", "int", "captura",
       "Cuánto dura la pausa del modo alineación si nadie la cambia (la cuerda).",
       "60", "60 s; nunca más que el tope de 3600 s.", minimum=10, maximum=3600, per_site=True),
    # --- mostrador: alerts ---
    _k("alerts.poll_s", "int", "alertas",
       "Cada cuántos segundos el bot revisa todo: cola, fallas y cámaras.",
       "10", "10 s.", minimum=5, maximum=120),
    _k("alerts.backlog_threshold", "int", "alertas",
       "Cuántos platos esperando hacen sonar la alerta de cola atorada.",
       "25", "25.", minimum=1, maximum=1000),
    _k("alerts.worker_silence_min", "int", "alertas",
       "Minutos sin terminar ningún plato, con platos esperando, antes de avisar que el analista está callado.",
       "10", "10 min.", minimum=1, maximum=240),
    _k("alerts.sharpness_min", "float", "alertas",
       "La nitidez mínima de una foto; por debajo cuenta como borrosa.",
       "60", "60 hasta calibrarla en el ensayo del día anterior a la demo.", minimum=1, maximum=5000, per_site=True),
    _k("alerts.sharpness_n", "int", "alertas",
       "Cuántas fotos borrosas seguidas disparan la alerta de lente sucio.",
       "3", "3.", minimum=1, maximum=20, per_site=True),
    _k("alerts.sharpness_cooldown_min", "int", "alertas",
       "Minutos de silencio después de una alerta de lente sucio o de cámara tapada.",
       "60", "60 min.", minimum=0, maximum=1440, per_site=True),
    _k("alerts.black_luma_max", "float", "alertas",
       "Brillo promedio (de 0 a 255) por debajo del cual una imagen cuenta como negra.",
       "12", "12: negro de verdad, no un local con poca luz.", minimum=1, maximum=80, per_site=True),
    _k("alerts.black_timer_s", "int", "alertas",
       "Segundos viendo negro, dentro del horario, antes de avisar que algo tapa la cámara.",
       "120", "120 s.", minimum=10, maximum=3600, per_site=True),
    _k("alerts.silent_site_min", "int", "alertas",
       "Minutos sin recibir imagen, dentro del horario, antes de avisar que el sitio está callado.",
       "5", "5 min: tolera un parpadeo del WiFi, no un teléfono apagado.", minimum=1, maximum=240, per_site=True),
    # --- mostrador: horario, papelera, forensic river ---
    _k("horario", "horario", "horario",
       "Horario de servicio: fuera de él la noche es negra y el bot no grita. Si el cierre es antes de la apertura, cierra al día siguiente.",
       '{"open": "07:00", "close": "22:00"}', "07:00 a 22:00; para una noche especial usen /extender en el grupo.",
       path="horario_default", per_site=True),
    _k("timezone", "timezone", "horario",
       "La zona horaria con la que se juzga el horario del sitio.",
       "America/Mexico_City", "America/Mexico_City (sin horario de verano).", per_site=True),
    _k("papelera_dias", "int", "papelera",
       "Días que un plato descartado espera en la papelera antes de borrarse para siempre.",
       "10", "10 días.", minimum=1, maximum=365),
    _k("raw_logging", "switch", "forense",
       "Guarda cada pregunta y respuesta completas del modelo (sin fotos) para investigar un problema.",
       "false", "Apagado. Préndelo solo mientras investigas: se apaga solo en 24 h.", special=True),
    # --- mostrador: destino, cola, gasto (their own endpoints) ---
    _k("admin_chat_id", "chat_id", "destino",
       "Chat propio para las alertas de máquina (fallas, cola, ventanilla). Vacío = van a todos los chats.",
       "-1001234567890", "Vacío hasta tener varios cafés; entonces un grupo solo del admin.",
       optional=True, special=True),
    _k("queue_paused", "pause", "cola",
       "Freno de la cola: los platos siguen llegando y esperan; nadie los analiza hasta reanudar.",
       "true", "Sin pausa; úsalo solo durante un mantenimiento.", optional=True, special=True),
    _k("prices", "prices", "gasto",
       "Precio por millón de tokens de cada modelo, para convertir el gasto en dinero.",
       "claude-sonnet-5: in_mtok, out_mtok, cache_write_mtok, cache_read_mtok",
       "Vacío hasta copiar el precio de la página oficial del proveedor; sin precio, el tablero muestra solo tokens.",
       special=True),
    # --- machine room ---
    _k("active_model", "model", "modelo",
       "El modelo que analiza los platos. Solo aparecen modelos cuya llave existe en el servidor.",
       "claude-sonnet-5", "claude-sonnet-5 hasta que el bake-off diga otra cosa.", floor="maquinas"),
    _k("machine.max_tokens", "int", "modelo",
       "Largo máximo de la respuesta del modelo.",
       "1024", "1024: el JSON de un plato cabe de sobra.", minimum=256, maximum=8192, floor="maquinas"),
    _k("machine.llm_timeout_s", "int", "constantes",
       "Segundos máximos de una llamada al modelo antes de contarla como fallida.",
       "120", "120 s; siempre menor que la ventana de rescate.", minimum=10, maximum=600, floor="maquinas"),
    _k("machine.orphan_timeout_min", "int", "constantes",
       "Minutos tras los cuales un plato en proceso sin terminar se rescata para otro analista.",
       "10", "10 min: el doble del peor intento normal.", minimum=2, maximum=120, floor="maquinas"),
    _k("machine.max_attempts", "int", "constantes",
       "Intentos de análisis por plato antes de darlo por fallido.",
       "3", "3.", minimum=1, maximum=10, floor="maquinas"),
    _k("machine.workers", "int", "analistas",
       "Cuántos analistas trabajan en paralelo para toda la cadena; el número se recuerda tras cada reinicio.",
       "2", "1 mientras la cola no se atore.", minimum=1, maximum=WORKER_SCALE_MAX, floor="maquinas"),
    _k("machine.session_hours", "int", "sesiones",
       "Horas que dura una sesión abierta antes de pedir la contraseña otra vez.",
       "720", "720 h (30 días).", minimum=1, maximum=8760, floor="maquinas"),
)

KNOBS: dict[str, Knob] = {k.name: k for k in REGISTRY}
PER_SITE: frozenset[str] = frozenset(k.name for k in REGISTRY if k.per_site)

# Anti-stupid guards at import: an incoherent registry fails loudly at startup, never on a page.
assert len(KNOBS) == len(REGISTRY), "knobs: duplicated name in REGISTRY"
for _knob in REGISTRY:
    assert _knob.que_es and _knob.ejemplo and _knob.recomendacion, f"knobs: {_knob.name} lacks one of the three helps"
    assert not (_knob.per_site and (_knob.special or _knob.floor != "mostrador")), (
        f"knobs: {_knob.name} is per_site, so it must be a plain mostrador knob"
    )
    assert _knob.kind not in ("int", "float") or (
        _knob.minimum is not None and _knob.maximum is not None and _knob.minimum <= _knob.maximum
    ), f"knobs: {_knob.name} needs coherent bounds"


# --------------------------------------------------------------------------- validation

def _num(x: float | int | None) -> str:
    if x is None:
        return "?"
    return str(int(x)) if float(x).is_integer() else str(x)


def _fail(knob: Knob, message: str) -> KnobError:
    return KnobError(f"{knob.name}: {message} Recomendación: {knob.recomendacion}")


def _as_number(raw: Any, *, integer: bool) -> float | int | None:
    """A number from JSON or a form field ("12", "12.5", "12,5"); None when it is not one."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, str):
        try:
            raw = float(raw.strip().replace(",", "."))
        except ValueError:
            return None
    if not isinstance(raw, (int, float)) or not math.isfinite(raw):
        return None
    if integer:
        if isinstance(raw, float) and not raw.is_integer():
            return None
        return int(raw)
    return float(raw)


def coerce(name: str, raw: Any) -> Any:
    """The normalized value to write for knob `name`, or KnobError with the registry's message."""
    knob = KNOBS.get(name)
    if knob is None:
        raise KnobError(f"«{name}» no es una perilla del registro: no se puede escribir.")
    kind = knob.kind
    if kind in ("int", "float"):
        value = _as_number(raw, integer=kind == "int")
        entero = " entero" if kind == "int" else ""
        if value is None:
            raise _fail(knob, f"escribe un número{entero} entre {_num(knob.minimum)} y {_num(knob.maximum)}.")
        if value < knob.minimum or value > knob.maximum:
            raise _fail(knob, f"debe estar entre {_num(knob.minimum)} y {_num(knob.maximum)}; llegó {_num(value)}.")
        return value
    if kind == "choice":
        if not isinstance(raw, str) or raw.strip() not in knob.choices:
            raise _fail(knob, f"elige una de estas opciones: {', '.join(knob.choices)}.")
        return raw.strip()
    if kind == "switch":
        if not isinstance(raw, bool):
            raise _fail(knob, "debe ser prendido o apagado (true o false).")
        return raw
    if kind == "pause":
        if raw is not True:
            raise _fail(knob, "solo existe prendida (true); para reanudar se borra la llave.")
        return True
    if kind == "horario":
        try:
            return HorarioConfig.model_validate(raw).model_dump()
        except ValidationError:
            raise _fail(knob, "escribe apertura y cierre como HH:MM (por ejemplo 07:00 y 22:00); no pueden ser iguales.") from None
    if kind == "timezone":
        try:
            return check_timezone(str(raw).strip())
        except ValueError:
            raise _fail(knob, "zona horaria desconocida; usa un nombre como America/Mexico_City.") from None
    if kind == "chat_id":
        text = "" if raw is None or isinstance(raw, bool) else str(raw).strip()
        if not CHAT_ID_RE.match(text):
            raise _fail(knob, "es el número del chat de Telegram, como -1001234567890 (escribe /id en el grupo).")
        return text
    if kind in ("prompt_merma", "prompt_presentacion"):
        names = [p.name for p in prompt_files("merma" if kind == "prompt_merma" else "presentacion")]
        if not isinstance(raw, str) or raw.strip() not in names:
            raise _fail(knob, f"elige un archivo que exista: {', '.join(names) or 'ninguno'}.")
        return raw.strip()
    if kind == "model":
        if not isinstance(raw, str) or not raw.strip():
            raise _fail(knob, "elige un modelo de la lista.")
        return raw.strip()
    raise KnobError(f"{knob.name} se edita en su propia sección, no como perilla suelta.")


def validate_overrides(cfg: Config) -> None:
    """Config's load-time gate for the cascade's middle layer: every override must be a per-site knob
    with a value the registry accepts (called from Config's validator; raises ValueError)."""
    for site, site_cfg in cfg.sites.items():
        for name, raw in site_cfg.overrides.items():
            knob = KNOBS.get(name)
            where = f"sites.{site}.overrides.{name}"
            if knob is None:
                raise ValueError(f"{where}: no such knob in the registry")
            if not knob.per_site:
                raise ValueError(f"{where}: {name} is a global knob; it has no per-site override")
            try:
                value = coerce(name, raw)
            except KnobError as exc:
                raise ValueError(f"{where}: {exc}") from None
            if name == "video_align.default_s" and value > cfg.video_align.max_s:
                raise ValueError(f"{where}: {value} s is longer than the rope's cap video_align.max_s ({cfg.video_align.max_s} s)")


# --------------------------------------------------------------------------- the cascade

def _plain(node: Any) -> Any:
    if isinstance(node, BaseModel):
        return node.model_dump()
    if isinstance(node, dict):
        return {k: _plain(v) for k, v in node.items()}
    return node


def global_value(cfg: Config, name: str) -> Any:
    """The knob's value at its global home (None when an optional knob is absent)."""
    node: Any = cfg
    for part in KNOBS[name].path:
        node = getattr(node, part)
    return _plain(node)


def effective(cfg: Config, name: str, site: str | None = None) -> Any:
    """The cascade: the site's override when it has one, else the global value."""
    knob = KNOBS[name]
    if site is not None and knob.per_site:
        site_cfg = cfg.sites.get(site)
        if site_cfg is not None and name in site_cfg.overrides:
            return coerce(name, site_cfg.overrides[name])
    return global_value(cfg, name)


def source(cfg: Config, name: str, site: str | None) -> str:
    """How the page paints the value: "propio de <site>", "heredado del global", or a plain global."""
    if not KNOBS[name].per_site or site is None:
        return "global"
    site_cfg = cfg.sites.get(site)
    return "propio" if site_cfg is not None and name in site_cfg.overrides else "heredado"


def overrides_of(cfg: Config, name: str) -> dict[str, Any]:
    """Every site's override of one knob: exactly the list "borrar overrides (N)" shows before it deletes."""
    return {
        site: coerce(name, site_cfg.overrides[name])
        for site, site_cfg in cfg.sites.items()
        if name in site_cfg.overrides
    }


def config_path(name: str, site: str | None = None) -> tuple[str, ...]:
    """Where the writer edits: the global home, or sites.<site>.overrides.<name> for a site's override."""
    knob = KNOBS.get(name)
    if knob is None:
        raise KnobError(f"«{name}» no es una perilla del registro: no se puede escribir.")
    if site is None:
        return knob.path
    if not knob.per_site:
        raise KnobError(f"{name} vale lo mismo para todos los sitios: no tiene valor propio por sitio.")
    return ("sites", site, "overrides", name)


def horario_of(cfg: Config, site: str) -> HorarioConfig:
    """The site's effective service hours (the bot's vigilantes judge with this)."""
    return HorarioConfig.model_validate(effective(cfg, "horario", site))


def timezone_of(cfg: Config, site: str) -> str:
    """The site's effective timezone name (every horario judgment converts through it)."""
    return effective(cfg, "timezone", site)


def describe(cfg: Config, *, site: str | None = None, floor: Floor | None = None) -> list[dict[str, Any]]:
    """What the two floors render: every knob with its helps, bounds, global value and, for a site,
    its effective value painted propio or heredado. Selector choices list only what exists: prompt
    files on disk, models whose provider key is present."""
    keyed = [c.model for c in keyed_contenders()]
    out: list[dict[str, Any]] = []
    for knob in REGISTRY:
        if floor is not None and knob.floor != floor:
            continue
        choices = list(knob.choices)
        if knob.kind == "prompt_merma":
            choices = [p.name for p in prompt_files("merma")]
        elif knob.kind == "prompt_presentacion":
            choices = [p.name for p in prompt_files("presentacion")]
        elif knob.kind == "model":
            choices = keyed
        item: dict[str, Any] = {
            "name": knob.name,
            "kind": knob.kind,
            "seccion": knob.seccion,
            "floor": knob.floor,
            "per_site": knob.per_site,
            "special": knob.special,
            "optional": knob.optional,
            "min": knob.minimum,
            "max": knob.maximum,
            "choices": choices,
            "que_es": knob.que_es,
            "ejemplo": knob.ejemplo,
            "recomendacion": knob.recomendacion,
            "global": global_value(cfg, knob.name),
            "overrides": len(overrides_of(cfg, knob.name)) if knob.per_site else 0,
        }
        if knob.per_site and site is not None:
            item["effective"] = effective(cfg, knob.name, site)
            item["source"] = source(cfg, knob.name, site)
        out.append(item)
    return out
