"""LPQ_VISION brain/validator/textos.py: the bot's voice, shared by the bot and the api (SPEC 1.5, 1.6).

Every message the bot sends (and the two the api sends itself: the pre-reboot notice and enviar-prueba)
lives here ONCE: its factory text, the fichas (named holes) it accepts with an example value each, and
the family that decides where it goes. Around the catalog: the VALIDATOR (a template that would fail at
3am never saves), the RENDERER (the only road from a text to a message), the textos.yaml reader and its
whole-file writer, and the DESTINATIONS map (the routing law that replaced the bot's destination()).

Pure code: no database, no network, no Telegram. textos.yaml is MACHINE-OWNED, the one config file
rewritten whole; absent-when-off: a key present overrides one message, a key absent uses the factory
text, and "restaurar original" deletes the key.

Families: site = only that site's chat; machine = the admin chat when configured, else every chat;
all = every chat (greeting, reboot notice); reply = answers the chat that asked; direct = one chosen chat.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Literal

import yaml

from brain.validator.models import CONFIG_DIR

log = logging.getLogger("lpq.validator.textos")

# --- named constants (SPEC section 4) -------------------------------------------------
TEXTOS_PATH = CONFIG_DIR / "textos.yaml"
TEXTO_MAX_CHARS = 1000                        # under Telegram's 1024-character photo-caption limit
REBOOT_NOTICE_DELAY = timedelta(seconds=60)   # the api sends the notice, waits this long, then calls the butler
MISSING_VALUE = "?"                           # a ficha the caller could not fill: the message still goes out
FICHA_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz_")

Familia = Literal["site", "machine", "all", "reply", "direct"]

TEXTOS_HEADER = (
    "# LPQ_VISION - textos.yaml: MACHINE-OWNED. The api rewrites this whole file from the mostrador\n"
    "# (Textos del bot); never edit it by hand. Absent-when-off: a key present overrides that bot message,\n"
    "# a key absent uses its factory text (brain/validator/textos.py); \"restaurar original\" deletes the key.\n"
    "# Operator state: this file never travels in a deploy.\n"
)


@dataclass(frozen=True)
class Texto:
    """One bot message: where it goes, when it fires (shown in the mostrador), its factory text and fichas."""

    key: str
    familia: Familia
    cuando: str
    texto: str
    fichas: dict[str, str] = field(default_factory=dict)   # ficha name -> example value (the menu the page shows)


def _t(key: str, familia: Familia, cuando: str, texto: str, **fichas: str) -> Texto:
    return Texto(key=key, familia=familia, cuando=cuando, texto=texto, fichas=dict(fichas))


_SC = {"sitio": "demo", "camara": "barra-1"}   # the example site/camera every site message shows

CATALOG: tuple[Texto, ...] = (
    # --- all chats ---
    _t("saludo", "all", "Cada vez que el bot arranca: reinicio, apagón o despliegue.",
       "🟢 Hola, estoy en línea. Vigilo la cola, las fallas, el lente, las cámaras y la ventanilla."),
    _t("aviso_reinicio", "all", "Antes de un reinicio pedido en el cuarto de máquinas (lo manda la ventanilla).",
       "🔄 El cerebro se reiniciará en {minutos} min por mantenimiento. Vuelvo en unos minutos.", minutos="1"),
    # --- machine families ---
    _t("cola_atorada", "machine", "Cuando los platos pendientes pasan el umbral de la cola.",
       "📈 Cola atorada: {pendientes} platos esperando (umbral {umbral}). El analista no da abasto.",
       pendientes="31", umbral="25"),
    _t("cola_normal", "machine", "Cuando la cola atorada vuelve bajo el umbral.",
       "✅ La cola volvió a la normalidad: {pendientes} pendientes.", pendientes="4"),
    _t("analisis_fallido", "machine", "Cuando un plato agota sus intentos de análisis.",
       "❌ El análisis del plato #{plato} falló definitivamente tras {intentos} intentos (job {job}): {error}",
       plato="166", intentos="3", job="412", error="Timeout: la llamada tardó demasiado"),
    _t("analista_callado", "machine", "Cuando hay platos esperando y el analista no termina ninguno a tiempo.",
       "😶 El analista lleva más de {minutos} min sin terminar nada y hay {pendientes} platos esperando. ¿Está vivo el worker?",
       minutos="10", pendientes="7"),
    _t("cola_en_pausa", "machine", "En lugar del aviso anterior, cuando la cola está en pausa desde el mostrador.",
       "⏸️ La cola está en pausa: {pendientes} platos esperan hasta que alguien la reanude en el mostrador.",
       pendientes="7"),
    _t("analista_volvio", "machine", "Cuando el analista vuelve a terminar platos.",
       "✅ El analista volvió a terminar platos."),
    _t("ventanilla_caida", "machine", "Cuando la ventanilla deja de responder.",
       "🔴 La ventanilla no responde ({motivo}). Nadie puede subir fotos hasta que vuelva.", motivo="health 503"),
    _t("ventanilla_sigue_caida", "machine", "Recordatorio mientras la ventanilla sigue sin responder.",
       "🔴 La ventanilla sigue sin responder desde hace {minutos} min ({motivo}).", minutos="15", motivo="health 503"),
    _t("ventanilla_volvio", "machine", "Cuando la ventanilla vuelve a responder.",
       "🟢 La ventanilla volvió después de {minutos} min.", minutos="3"),
    _t("forense_prendido", "machine", "Cada 48 h mientras el registro forense sigue prendido.",
       "📝 El registro forense lleva {horas} h PRENDIDO, ¿a propósito? Llena disco (tope 200 MB). Apágalo en el mostrador cuando termines.",
       horas="48"),
    _t("forense_apagado", "machine", "Cuando el registro forense se apaga solo al vencer su plazo.",
       "📝 El registro forense se apagó solo al vencer su plazo."),
    _t("analistas_ajustados", "machine", "Cuando la ventanilla ajusta sola el número de analistas tras un arranque.",
       "⚙️ Ajusté los analistas a {deseados} (había {corriendo}) después del arranque.", deseados="2", corriendo="1"),
    _t("cuenta_por_borrarse", "machine", "Cinco días antes de borrar para siempre una cuenta eliminada.",
       "⚠️ La cuenta {usuario} (#{numero}) se borra para siempre en {dias} días ({fecha}). Si fue un error, "
       "recupérala en el mostrador: Sitios y cuentas → Eliminadas.",
       usuario="gerente.roma", numero="7", dias="5", fecha="12 nov 2026"),
    # --- site families ---
    _t("lente_sucio", "site", "Cuando las últimas fotos de una cámara salen borrosas.",
       "🧽 Lente sucio en {sitio}/{camara}: las últimas {n} fotos salieron borrosas (nitidez {valores}, piso {piso}). Limpien el vidrio de la cámara.",
       **_SC, n="3", valores="41, 38, 44", piso="60"),
    _t("camara_tapada", "site", "Cuando una cámara ve negro dentro del horario de servicio.",
       "🕶️ Algo tapa la cámara {sitio}/{camara}: lleva {segundos} s viendo negro en horario de servicio. Revisen que nada esté encima del lente.",
       **_SC, segundos="130"),
    _t("sitio_callado", "site", "Cuando una cámara deja de mandar imagen dentro del horario de servicio.",
       "📵 El sitio {sitio} está callado: la cámara {camara} no manda imagen desde hace {minutos} min en horario de servicio. ¿Se apagó el teléfono o se cerró la página de captura?",
       **_SC, minutos="6"),
    _t("sitio_volvio", "site", "Cuando esa cámara vuelve a mandar imagen.",
       "🟢 El sitio {sitio} volvió a mandar imagen ({camara}).", **_SC),
    _t("alineacion_inicio", "site", "Cuando empieza el modo alineación de una cámara.",
       "🟡 Modo alineación en {sitio}/{camara}: la captura de esa cámara está en pausa.", **_SC),
    _t("alineacion_fin", "site", "Cuando termina el modo alineación y la captura despierta.",
       "🟢 Alineación terminada en {sitio}/{camara}: la captura despertó.", **_SC),
    _t("relevo", "site", "Cuando una cámara cambia de fuente (relevo) y vuelve a capturar.",
       "🟢 {sitio}/{camara} capturando de nuevo (fuente: {fuente}).", **_SC, fuente="teléfono de emergencia"),
    _t("extension_fin", "site", "Cuando termina la extensión de horario de la noche.",
       "🟢 Terminó la extensión de hoy en {sitio}: vuelve el horario normal ({apertura} a {cierre}).",
       sitio="demo", apertura="07:00", cierre="22:00"),
    # --- replies to commands ---
    _t("extension_ok", "reply", "Respuesta a /extender cuando se acepta.",
       "🟡 Horario extendido en {sitio}: hoy cierra a las {hora}. Mañana vuelve el horario normal.",
       sitio="demo", hora="23:30"),
    _t("extension_rechazada", "reply", "Respuesta a /extender cuando no se puede.",
       "No pude extender el horario de {sitio}: {motivo}",
       sitio="demo", motivo="esa hora ya llega a la apertura de mañana"),
    _t("extension_pide_sitio", "reply", "Respuesta a /extender en un grupo con varios sitios sin decir cuál.",
       "Este grupo atiende varios sitios ({sitios}). Escribe el sitio antes de la hora, por ejemplo: /extender {ejemplo} 23:30",
       sitios="demo, roma", ejemplo="roma"),
    _t("extension_uso", "reply", "Respuesta a /extender sin hora o con una hora ilegible.",
       "Uso: /extender 23:30 o /extender +2h. Solo cambia el cierre de hoy; mañana vuelve el horario normal."),
    _t("grupo_sin_sitio", "reply", "Respuesta a un comando en un grupo sin sitio asignado.",
       "Este grupo no está asignado a ningún sitio. Pide al admin que lo asigne en el mostrador (Destino Telegram)."),
    _t("id_chat", "reply", "Respuesta a /id.",
       "Este chat es el {chat_id}. Pégalo en el mostrador, en Destino Telegram del sitio.", chat_id="-1001234567890"),
    _t("cola_estado", "reply", "Respuesta a /cola cuando ya hay platos terminados.",
       "📦 Cola de {sitio}: {pendientes} pendientes · {en_proceso} en proceso. Último plato terminado: #{plato} ({platillo}) hace {hace}.",
       sitio="demo", pendientes="2", en_proceso="1", plato="166", platillo="omelette_champinones", hace="2 min"),
    _t("cola_sin_platos", "reply", "Respuesta a /cola cuando todavía no hay platos terminados.",
       "📦 Cola de {sitio}: {pendientes} pendientes · {en_proceso} en proceso. Todavía no se ha terminado ningún plato.",
       sitio="demo", pendientes="0", en_proceso="0"),
    _t("cola_error", "reply", "Respuesta a /cola cuando no se puede leer la base.",
       "No pude leer la cola ({motivo}).", motivo="OperationalError"),
    _t("foto_pie", "reply", "Pie de la foto que manda /foto.",
       "📷 {sitio}/{camara} · nitidez {nitidez} · copia de hace {edad} s", **_SC, nitidez="84.2", edad="3.1"),
    _t("foto_sin_copia", "reply", "Respuesta a /foto cuando nadie está capturando.",
       "Sin copia fresca de {sitio}/{camara}: nadie está capturando ahora.", **_SC),
    _t("foto_error", "reply", "Respuesta a /foto cuando la ventanilla no contesta.",
       "La ventanilla no respondió ({motivo}).", motivo="TimeoutError"),
    _t("sitio_sin_camaras", "reply", "Respuesta a /foto en un sitio sin cámaras.",
       "El sitio {sitio} no tiene cámaras configuradas.", sitio="demo"),
    # --- one chosen chat ---
    _t("prueba_destino", "direct", "Botón «enviar prueba» de Destino Telegram.",
       "🔔 Prueba de LPQ_VISION: este chat recibirá {que}.", que="las alertas del sitio demo"),
)

FACTORY: dict[str, Texto] = {t.key: t for t in CATALOG}


class TextoError(ValueError):
    """A template the validator refuses; the message (Spanish) names the exact point for the mostrador."""


def _frag(text: str, i: int) -> str:
    return text[max(0, i - 10): i + 12].replace("\n", " ")


def _scan(text: str, allowed: Iterable[str]) -> list[tuple[bool, str]]:
    """Split a template into (is_ficha, content) parts, refusing anything the renderer could choke on.
    Two opening (or closing) braces in a row are one literal brace, like Python's format."""
    allowed = set(allowed)
    parts: list[tuple[bool, str]] = []
    buf: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "{":
            if text.startswith("{{", i):
                buf.append("{")
                i += 2
                continue
            close = text.find("}", i + 1)
            if close == -1:
                raise TextoError(
                    f"Hay una llave de apertura sin cerrar en la posición {i + 1} («{_frag(text, i)}»). "
                    "Cierra la ficha o escribe dos llaves de apertura seguidas para mostrar una llave."
                )
            name = text[i + 1:close]
            if not name:
                raise TextoError(f"Hay una ficha vacía en la posición {i + 1}: escribe su nombre entre las llaves.")
            if not set(name) <= FICHA_CHARS:
                raise TextoError(
                    f"La ficha que empieza en la posición {i + 1} («{_frag(text, i)}») tiene caracteres no válidos: "
                    "usa solo su nombre en minúsculas."
                )
            if name not in allowed:
                available = ", ".join(sorted(allowed)) or "ninguna"
                raise TextoError(
                    f"La ficha «{name}» (posición {i + 1}) no existe para este mensaje. Fichas disponibles: {available}."
                )
            if buf:
                parts.append((False, "".join(buf)))
                buf = []
            parts.append((True, name))
            i = close + 1
            continue
        if ch == "}":
            if text.startswith("}}", i):
                buf.append("}")
                i += 2
                continue
            raise TextoError(
                f"Hay una llave de cierre suelta en la posición {i + 1} («{_frag(text, i)}»). "
                "Bórrala o escribe dos llaves de cierre seguidas para mostrar una llave."
            )
        buf.append(ch)
        i += 1
    if buf:
        parts.append((False, "".join(buf)))
    return parts


# Anti-stupid guard at import: a duplicated key or a broken factory text fails loudly at startup.
assert len(FACTORY) == len(CATALOG), "textos: duplicated key in CATALOG"
for _texto in CATALOG:
    _scan(_texto.texto, _texto.fichas)


def validate_template(key: str, text: object) -> None:
    """Raise TextoError (Spanish, naming the point) unless `text` is a safe template for message `key`."""
    texto = FACTORY.get(key)
    if texto is None:
        raise TextoError(f"No existe el mensaje «{key}».")
    if not isinstance(text, str) or not text.strip():
        raise TextoError("El texto no puede quedar vacío.")
    if len(text) > TEXTO_MAX_CHARS:
        raise TextoError(f"El texto tiene {len(text)} caracteres y el máximo es {TEXTO_MAX_CHARS}.")
    _scan(text, texto.fichas)


def render(key: str, values: Mapping[str, object] | None = None, overrides: Mapping[str, str] | None = None) -> str:
    """The message for `key`: the override when present and valid, else the factory text, fichas filled.
    A missing value renders as MISSING_VALUE: an alert is never lost to a template."""
    texto = FACTORY[key]   # an unknown key is a programming error: loud, at the call site
    template = texto.texto
    custom = (overrides or {}).get(key)
    if custom is not None:
        try:
            validate_template(key, custom)
            template = custom
        except TextoError as exc:
            log.warning("textos: override of %s unusable (%s); factory text used", key, exc)
    vals = values or {}
    out: list[str] = []
    for is_ficha, content in _scan(template, texto.fichas):
        if is_ficha:
            value = vals.get(content)
            out.append(MISSING_VALUE if value is None else str(value))
        else:
            out.append(content)
    return "".join(out)


def sample_values(key: str) -> dict[str, str]:
    """The example value of every ficha of `key` (what enviar-prueba fills in)."""
    return dict(FACTORY[key].fichas)


def catalog_view(overrides: Mapping[str, str]) -> list[dict[str, object]]:
    """What the mostrador lists: every message with its factory text, the current text and its fichas."""
    return [
        {
            "key": t.key,
            "familia": t.familia,
            "cuando": t.cuando,
            "original": t.texto,
            "actual": overrides.get(t.key, t.texto),
            "propio": t.key in overrides,
            "fichas": dict(t.fichas),
        }
        for t in CATALOG
    ]


def load_textos(path: Path = TEXTOS_PATH) -> dict[str, str]:
    """The valid overrides in textos.yaml. Never raises: a missing or broken file, or a bad entry, is logged
    and skipped, and that message falls back to its factory text (the bot reads this every round)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        log.error("textos.yaml unreadable, factory texts used: %s", exc)
        return {}
    if data is None:
        return {}
    if not isinstance(data, dict):
        log.error("textos.yaml top level must be a mapping; factory texts used")
        return {}
    out: dict[str, str] = {}
    for key, text in data.items():
        name = str(key)
        try:
            validate_template(name, text)
        except TextoError as exc:
            log.warning("textos.yaml entry %s ignored: %s", name, exc)
            continue
        out[name] = text
    return out


def textos_yaml(overrides: Mapping[str, str]) -> str:
    """The WHOLE textos.yaml content for these overrides (each validated first). The api's writer writes it
    atomically; empty overrides = the header alone (absent-when-off: no empty mapping left behind)."""
    for key, text in overrides.items():
        validate_template(key, text)
    if not overrides:
        return TEXTOS_HEADER
    body = yaml.safe_dump(
        {key: overrides[key] for key in sorted(overrides)},
        allow_unicode=True,
        default_style='"',
        sort_keys=False,
        width=4096,
    )
    return TEXTOS_HEADER + body


def _dedup(chats: Iterable[str | None]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for chat in chats:
        if chat and chat not in seen:
            seen.add(chat)
            out.append(chat)
    return out


def destinations(
    familia: Familia,
    *,
    site: str | None = None,
    site_chats: Mapping[str, str],
    admin_chat: str | None = None,
    env_chat: str | None = None,
    muted: frozenset[str] = frozenset(),
) -> list[str]:
    """The chats a message of this family goes to (SPEC 1.5 routing law). Chat ids are strings, never logged.

    site:    ONLY that site's chat; nothing while the site is in mantenimiento (muted).
    machine: ONLY admin_chat when it exists; otherwise every configured chat, deduplicated.
    all:     every configured chat, admin_chat included (greeting and reboot notice reach everyone).
    The temporal env chat is the LAST resort: used only when nothing else is configured for that message,
    and it dies by itself the day its .env key is deleted. An empty list = undeliverable (the caller logs it).
    """
    fallback = [env_chat] if env_chat else []
    if familia == "site":
        if site is None or site in muted:
            return []
        chat = site_chats.get(site) or env_chat
        return [chat] if chat else []
    everyone = _dedup([*site_chats.values(), admin_chat])
    if familia == "machine":
        if admin_chat:
            return [admin_chat]
        return everyone or fallback
    if familia == "all":
        return everyone or fallback
    raise ValueError(f"familia {familia!r} answers the chat that asked; it is never routed")
