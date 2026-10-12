"""LPQ_VISION bot/main.py: la voz, @LPQ_vision_bot on python-telegram-bot 22.8 (SPEC 1.5 and 1.8; Sections 3.11, 3.13.1).

Commands, answered in the group that asked: /id (this chat's number, to paste in the mostrador), /cola (that
site's queue and its last finished plate), /foto (what that site's camera sees now), /extender (moves tonight's
close later) and the one-day hours (Section 3.13.1): /hoy and /manana show that day's hours; /abrir, /cerrar,
/cerrar ya, /hoy HH:MM HH:MM, /hoy cerrado and their mañana forms change them, only for today or tomorrow and
only for that day. /mañana with the ñ works too: Telegram ends a command at the ñ, so a text filter catches it.
Every change, /extender's included, is ONE row of horario_extensiones (the whole new state of that service day;
the latest row of a day wins) plus its bitácora row, in one transaction: the bot's only writes. A group serves
the sites whose sites.telegram_chat_id it is; the admin chat and the temporal .env chat serve every active site.
A group with several sites names the site first (/foto roma, /extender roma 23:30, /abrir roma mañana 10:00)
and the bot asks instead of guessing.

One round every alerts.poll_s. Config and textos.yaml are read every round (every knob enters hot); then the
database and the api; each rule is a pure function of the round's inputs and the bot's RAM, returning the
messages to send. The vigilantes judge each site in its own timezone and horario (the midnight law; that day's
one-day changes included): 📵 a camera with no frames, counted from the later of its last frame (the api's boot
standing in when it has none) and the opening; 🕶️ a covered camera (black_for_s, which the api measures to the
LAST frame); the dirty lens; the alignment notices. Nothing judges a site in mantenimiento. Machine families:
the backlog, the failures (a cursor on finished_at: a retried job's second failure is announced too), the
silent worker ("cola en pausa" while the queue is paused), the ventanilla and the forensic river. A read-only
cursor over the bitácora announces the relevo, the forensic river's auto-off, the workers' reconvergence and
an account about to be erased: the action, never the name of who acted.

Where a message goes is brain.validator.textos.destinations (the routing law); every text comes from
textos.yaml or its factory default. The api calls carry the "lpq-bot" User-Agent and, for a site's camera, that
site's gafete (X-Device-Key). Chat ids and gafetes are never logged. At shutdown the round task is cancelled.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from brain.adapter import raw_log
from brain.db import queries as q
from brain.validator import knobs, textos
from brain.validator.models import Config, HorarioConfig, load_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("lpq.bot")

# --- named constants (SPEC section 4) -------------------------------------------------
API_BASE = "http://fastapi:8000"             # the compose SERVICE name; internal network only
HTTP_TIMEOUT_S = 5
USER_AGENT = "lpq-bot"                       # the api's machine-room status recognizes the bot by it
API_REMINDER = timedelta(minutes=15)         # how often "la ventanilla sigue sin responder" repeats
RAW_LOG_REMINDER_HOURS = 48
LAST_ERROR_MAX_CHARS = 300
HORARIO_LOOKBACK = timedelta(days=3)         # the service days read: from the day before yesterday (it bounds
                                             # yesterday's opening) on, in any site's timezone
POLL_S_DEFAULT = 10                          # the round cadence until a config has loaded once
ENV_TOKEN = "TELEGRAM_BOT_TOKEN"
ENV_CHAT_ID = "TELEGRAM_CHAT_ID"             # temporal: the last-resort chat until the ceremony deletes it
MESES = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")
DIAS_SEMANA = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
DIA_PALABRAS = {"hoy": 0, "mañana": 1, "manana": 1}   # the only days a one-day change may name
FUENTES = {"phone": "teléfono", "pi": "Pi"}
QUIEN_MAX = 60                               # the Telegram name kept in an hour change's bitácora row
BITACORA_HORARIO = "horario.cambio"          # the bitácora action of every hour change (/extender's included)

_HORA_RE = re.compile(r"^(\d{1,2}):(\d{2})\Z")
_MAS_RE = re.compile(r"^\+(?:(\d{1,2})h(?:(\d{1,2})m?)?|(\d{1,4})m)\Z")
# /mañana with the ñ (composed, or n plus the combining tilde): Telegram ends a command entity at the ñ, so the
# bot matches the text itself. Group 1 is the bot it is written to, when it says.
_MANANA_ENIE_RE = re.compile(r"^/ma(?:ñ|n\u0303)ana(?:@(\w+))?(?:\s|\Z)", re.IGNORECASE)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _env_chat() -> str | None:
    return (os.environ.get(ENV_CHAT_ID) or "").strip() or None


def _hhmm(moment: datetime, zone: ZoneInfo) -> str:
    return moment.astimezone(zone).strftime("%H:%M")


# --- the horario, judged in the site's own timezone (pure) ----------------------------------

@dataclass(frozen=True)
class Window:
    """One service day's REGULAR window: its opening, its close (the NEXT day when close <= open) and the next
    day's regular opening."""

    open: datetime
    close: datetime
    next_open: datetime


@dataclass(frozen=True)
class Dia:
    """One service day as it stands: its regular window and its hours after that day's one-day changes (the
    latest change of the day wins). open None = cerrado todo el día."""

    fecha: date
    base: Window
    open: datetime | None
    close: datetime | None

    @property
    def cerrado(self) -> bool:
        return self.open is None

    @property
    def cambiado(self) -> bool:
        return self.open != self.base.open or self.close != self.base.close

    def dentro(self, now: datetime) -> bool:
        return self.open is not None and self.close is not None and self.open <= now < self.close


def _utc(moment: datetime | None) -> datetime | None:
    """Every instant of the horario is kept in UTC, whatever timezone the database session hands back: comparisons
    and "+2h" then count real time, daylight saving included."""
    return None if moment is None else moment.astimezone(timezone.utc)


def _at(day: date, hhmm: str, zone: ZoneInfo, fold: int = 0) -> datetime:
    """That day's HH:MM on the site's wall clock, as a UTC instant (fold=1: the second pass of the hour that repeats
    when daylight saving ends)."""
    hours, minutes = (int(part) for part in hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hours, minutes, tzinfo=zone, fold=fold).astimezone(timezone.utc)


def _window(day: date, horario: HorarioConfig, zone: ZoneInfo) -> Window:
    close_day = day if horario.close > horario.open else day + timedelta(days=1)   # the midnight law
    return Window(_at(day, horario.open, zone), _at(close_day, horario.close, zone),
                  _at(day + timedelta(days=1), horario.open, zone))


def _primera_despues(moment: datetime, hhmm: str, zone: ZoneInfo) -> datetime:
    """HH:MM's first occurrence after a moment, in the site's timezone (a close after midnight is the next day)."""
    day = moment.astimezone(zone).date()
    pasos = (_at(day, hhmm, zone), _at(day, hhmm, zone, fold=1), _at(day + timedelta(days=1), hhmm, zone))
    return next(at for at in pasos if at > moment)


def _cierre_para(abre: datetime, hhmm: str, zone: ZoneInfo, now: datetime) -> datetime:
    """A close written as HH:MM: its first occurrence after the opening; in the hour that repeats when daylight
    saving ends, its second pass once the first one went by."""
    cierre = _primera_despues(abre, hhmm, zone)
    if cierre <= now:
        otra = _at(cierre.astimezone(zone).date(), hhmm, zone, fold=1)
        if otra > now:
            return otra
    return cierre


def dia_de(fecha: date, horario: HorarioConfig, zone: ZoneInfo, cambios: Iterable[Mapping[str, Any]]) -> Dia:
    """That service day's hours: its regular window, unless a one-day change of that day exists (the latest wins)."""
    base = _window(fecha, horario, zone)
    mios = [c for c in cambios if c["dia"] == fecha]
    if not mios:
        return Dia(fecha, base, base.open, base.close)
    last = max(mios, key=lambda c: (_utc(c["created_at"]), c["id"]))
    if last["cerrado"]:
        return Dia(fecha, base, None, None)
    return Dia(fecha, base, _utc(last["abre_at"]) or base.open, _utc(last["until"]))


def hoy_de(now: datetime, horario: HorarioConfig, zone: ZoneInfo,
           cambios: Iterable[Mapping[str, Any]]) -> tuple[Dia, bool]:
    """The service day "hoy" means, and whether now is inside it. A night still open past midnight is still
    yesterday's service day (the midnight law); otherwise hoy is the calendar day in the site's timezone."""
    cambios = list(cambios)
    today = now.astimezone(zone).date()
    ayer = dia_de(today - timedelta(days=1), horario, zone, cambios)
    if ayer.dentro(now):
        return ayer, True
    hoy = dia_de(today, horario, zone, cambios)
    return hoy, hoy.dentro(now)


def siguiente_apertura(fecha: date, horario: HorarioConfig, zone: ZoneInfo,
                       cambios: Iterable[Mapping[str, Any]]) -> datetime:
    """The next day's opening, its own one-day change included (a day closed all day bounds with its regular one)."""
    nxt = dia_de(fecha + timedelta(days=1), horario, zone, cambios)
    return nxt.open or nxt.base.open


def cierre_anterior(fecha: date, horario: HorarioConfig, zone: ZoneInfo,
                    cambios: Iterable[Mapping[str, Any]]) -> datetime:
    """The previous day's close, its own one-day change included (a day closed all day bounds with its regular one)."""
    prev = dia_de(fecha - timedelta(days=1), horario, zone, cambios)
    return prev.close or prev.base.close


def _hora(text: str) -> str | None:
    """HH:MM on a 24-hour clock, normalized (9:05 -> 09:05); None when it is anything else."""
    found = _HORA_RE.match(text.strip())
    if not found:
        return None
    hours, minutes = int(found.group(1)), int(found.group(2))
    return f"{hours:02d}:{minutes:02d}" if hours < 24 and minutes < 60 else None


def parse_extension(arg: str) -> tuple[str, Any] | None:
    """("hora", "HH:MM") for /extender 23:30, ("mas", timedelta) for +2h, +1h30 or +90m; None when unreadable."""
    text = arg.strip().lower()
    hora = _hora(text)
    if hora:
        return ("hora", hora)
    found = _MAS_RE.match(text)
    if found:
        if found.group(3) is not None:
            minutes = int(found.group(3))
        else:
            extra = int(found.group(2) or 0)
            if extra >= 60:
                return None
            minutes = int(found.group(1)) * 60 + extra
        return ("mas", timedelta(minutes=minutes)) if 0 < minutes < 24 * 60 else None
    return None


def _noche(now: datetime, horario: HorarioConfig, zone: ZoneInfo, cambios: list[Mapping[str, Any]]) -> Dia:
    """The night /extender lengthens: the service day open now; else hoy once its opening came (even when it closed
    early); before hoy opens, the nearer of last night (just closed) and hoy (opens later; a day closed all day
    counts with its regular opening, and then there is nothing to lengthen). Never tomorrow: the answer says
    "hoy"."""
    hoy, dentro = hoy_de(now, horario, zone, cambios)
    abre = hoy.open or hoy.base.open
    if dentro or abre <= now:
        return hoy
    ayer = dia_de(hoy.fecha - timedelta(days=1), horario, zone, cambios)
    if ayer.cerrado:
        return hoy
    return hoy if abre - now < now - ayer.close else ayer


def resolve_extension(now: datetime, horario: HorarioConfig, zone: ZoneInfo, cambios: Iterable[Mapping[str, Any]],
                      pedido: tuple[str, Any]) -> tuple[Dia | None, datetime | None, str]:
    """(day, until, "") for an accepted /extender, or (None, None, motivo). "Tonight" is _noche's day. HH:MM is
    that time's first occurrence after the day's opening and must fall after tonight's close; +2h counts from
    tonight's close (that day's changes included); nothing may reach the next opening."""
    cambios = list(cambios)
    dia = _noche(now, horario, zone, cambios)
    if dia.cerrado:
        return None, None, "hoy está cerrado todo el día y /extender solo alarga una noche abierta."
    kind, value = pedido
    if kind == "hora":
        until = _cierre_para(dia.open, value, zone, now)
        if until <= dia.close:
            return None, None, f"hoy ya cierra a las {_hhmm(dia.close, zone)} y /extender solo alarga la noche."
    else:
        until = dia.close + value
    tope = siguiente_apertura(dia.fecha, horario, zone, cambios)
    if until >= tope:
        return None, None, f"llegaría a la apertura de mañana ({_hhmm(tope, zone)})."
    if until <= now:
        return None, None, "esa hora ya pasó."
    return dia, until, ""


def parse_orden(comando: str, args: list[str]) -> tuple[Any, ...] | None:
    """What a one-day hours command asks for, or None when it is written another way (the answer is the usage).
    ("ver", d) · ("cerrado", d) · ("horario", d, abre, cierra) · ("abrir", d, hora) · ("cerrar", d, hora) · ("ya",)
    · ("lejos", palabra) for a day that is neither hoy nor mañana. d: 0 = hoy, 1 = mañana."""
    words = [a.strip().lower() for a in args if a.strip()]
    if comando in ("hoy", "manana"):
        d = DIA_PALABRAS[comando]
        if not words:
            return ("ver", d)
        if words == ["cerrado"]:
            return ("cerrado", d)
        if len(words) == 2 and _hora(words[0]) and _hora(words[1]):
            return ("horario", d, _hora(words[0]), _hora(words[1]))
        return None
    if comando == "cerrar" and words == ["ya"]:
        return ("ya",)
    if comando in ("abrir", "cerrar"):
        if len(words) == 1 and _hora(words[0]):
            return (comando, 0, _hora(words[0]))
        if len(words) == 2 and _hora(words[1]):
            if words[0] in DIA_PALABRAS:
                return (comando, DIA_PALABRAS[words[0]], _hora(words[1]))
            if not _hora(words[0]):
                return ("lejos", words[0])
    return None


def _nombre(fecha: date) -> str:
    return f"{DIAS_SEMANA[fecha.weekday()]} {fecha.day}"


def _para_manana(orden: tuple[Any, ...]) -> str:
    """The same change written for mañana."""
    if orden[0] in ("abrir", "cerrar"):
        return f"/{orden[0]} mañana {orden[2]}"
    if orden[0] == "horario":
        return f"/manana {orden[2]} {orden[3]}"
    return "/manana cerrado"


def _con_pista(motivo: str, pista: str) -> str:
    return motivo if not pista else motivo + (" " if motivo.endswith(".") else ". ") + pista


def resolve_cambio(now: datetime, horario: HorarioConfig, zone: ZoneInfo, cambios: Iterable[Mapping[str, Any]],
                   orden: tuple[Any, ...]) -> tuple[Dia | None, Dia | None, str]:
    """(before, after, "") for an accepted one-day change, or (None, None, motivo). It targets hoy (the service
    day hoy_de names) or mañana, and lives only that day. A close is the time's first occurrence after that
    day's opening (after midnight = the next morning). A day may not open before the previous day closes nor
    close at or after the next day's opening. Refused too: a close already past, moving the opening of a day
    open right now to an hour already past, closing all day a day already over, and a change that changes
    nothing. Past midnight inside last night's service, "hoy" is still that night: every refusal then also
    shows the mañana form. A day closed all day reopens from its regular hours."""
    cambios = list(cambios)
    hoy, dentro = hoy_de(now, horario, zone, cambios)
    if orden[0] == "ya":
        if not dentro:
            return None, None, "no está abierto ahora."
        return hoy, replace(hoy, close=now), ""
    fecha = hoy.fecha + timedelta(days=orden[1])
    dia = dia_de(fecha, horario, zone, cambios)
    cuando = _cuando(fecha, hoy.fecha)
    pista = ""
    if dentro and orden[1] == 0 and hoy.fecha < now.astimezone(zone).date():
        pista = (f"Pasada la medianoche, «hoy» todavía es la noche del {_nombre(hoy.fecha)}, abierta hasta las "
                 f"{_cierre_txt(hoy, zone)}. Para el {_nombre(hoy.fecha + timedelta(days=1))}: {_para_manana(orden)}")

    def no(motivo: str) -> tuple[None, None, str]:
        return None, None, _con_pista(motivo, pista)

    if orden[0] == "cerrado":
        if dia.cerrado:
            return no(f"{cuando} ya está cerrado todo el día.")
        if pista:
            return no(f"{cuando} está abierto ahora. Para cerrar ahora: /cerrar ya")
        if dia.close <= now:
            return no(f"{cuando} ya cerró, a las {_cierre_txt(dia, zone)}. Para mañana: /manana cerrado")
        return dia, replace(dia, open=None, close=None), ""
    abre = dia.base.open if dia.cerrado else dia.open
    cierra = dia.base.close if dia.cerrado else dia.close
    if orden[0] in ("abrir", "horario"):
        abre = _at(fecha, orden[2], zone)
        if dia.dentro(now) and abre != dia.open and abre <= now:
            solo = f" Para cambiar solo el cierre: /cerrar {orden[3]}" if orden[0] == "horario" else ""
            return no(f"{cuando} ya abrió, a las {_hhmm(dia.open, zone)}, y esa apertura ya pasó.{solo}")
    if orden[0] == "abrir":
        if abre >= cierra:
            fin = dia.base.close if dia.base.close > abre else abre + timedelta(hours=2)
            comando = "hoy" if orden[1] == 0 else "manana"
            return no(f"ese día cierra a las {_hhmm(cierra, zone)}, antes de abrir. Para cambiar las dos horas a la "
                      f"vez: /{comando} {orden[2]} {_hhmm(fin, zone)}")
        nuevo = replace(dia, open=abre, close=cierra)
    elif orden[0] == "cerrar":
        nuevo = replace(dia, open=abre, close=_cierre_para(abre, orden[2], zone, now))
    else:
        if orden[2] == orden[3]:
            return no("la apertura y el cierre no pueden ser la misma hora.")
        nuevo = replace(dia, open=abre, close=_cierre_para(abre, orden[3], zone, now))
    if nuevo.open != dia.open:
        previo = cierre_anterior(fecha, horario, zone, cambios)
        if nuevo.open < previo:
            return no(f"abriría antes de que cierre el día anterior (cierra a las {_hhmm(previo, zone)}).")
    tope = siguiente_apertura(fecha, horario, zone, cambios)
    if nuevo.close >= tope:
        return no(f"el cierre llegaría a la apertura del día siguiente ({_hhmm(tope, zone)}).")
    if nuevo.close <= now:
        if dentro and fecha == hoy.fecha:
            return no("esa hora ya pasó. Para cerrar ahora: /cerrar ya")
        return no(f"ese horario ya terminó (cerraría a las {_hhmm(nuevo.close, zone)}). Para mañana escribe "
                  f"«mañana», por ejemplo: /abrir mañana {_hhmm(nuevo.open, zone)}")
    if nuevo.open == dia.open and nuevo.close == dia.close:
        return no(f"{cuando} ya queda así: {horas_txt(dia, zone, nota=False)}.")
    return dia, nuevo, ""


def _cuando(fecha: date, hoy: date) -> str:
    """'hoy (domingo 11)' or 'mañana (lunes 12)': every answer names the day."""
    return f"{'hoy' if fecha == hoy else 'mañana'} ({DIAS_SEMANA[fecha.weekday()]} {fecha.day})"


def _cierre_txt(dia: Dia, zone: ZoneInfo) -> str:
    """A close after midnight says so."""
    texto = _hhmm(dia.close, zone)
    return texto + " de la madrugada" if dia.close.astimezone(zone).date() > dia.fecha else texto


def horas_txt(dia: Dia, zone: ZoneInfo, nota: bool = True) -> str:
    """'abre 07:00, cierra 22:00' or 'cerrado todo el día', plus the regular hours when a change applies."""
    texto = "cerrado todo el día" if dia.cerrado else f"abre {_hhmm(dia.open, zone)}, cierra {_cierre_txt(dia, zone)}"
    if nota and dia.cambiado:
        texto += f" (cambio de un día; el normal es {_hhmm(dia.base.open, zone)} a {_hhmm(dia.base.close, zone)})"
    return texto


def cambio_txt(orden: tuple[Any, ...], nuevo: Dia, zone: ZoneInfo) -> str:
    if orden[0] == "ya":
        return f"se cerró ahora, a las {_hhmm(nuevo.close, zone)}"
    if orden[0] == "cerrado":
        return "queda cerrado todo el día"
    if orden[0] == "abrir":
        return f"se cambió la apertura a las {_hhmm(nuevo.open, zone)}"
    if orden[0] == "cerrar":
        return f"se cambió el cierre a las {_cierre_txt(nuevo, zone)}"
    return f"se cambió el horario: abre {_hhmm(nuevo.open, zone)}, cierra {_cierre_txt(nuevo, zone)}"


def vuelta_txt(nuevo: Dia, hoy: Dia, horario: HorarioConfig, zone: ZoneInfo, cambios: Iterable[Mapping[str, Any]]) -> str:
    """What comes after: tomorrow's regular hours, unless tomorrow has its own change."""
    if nuevo.fecha != hoy.fecha:
        return "Solo ese día."
    manana = dia_de(hoy.fecha + timedelta(days=1), horario, zone, cambios)
    if manana.cambiado:
        return f"Mañana tiene su propio cambio: {horas_txt(manana, zone, nota=False)}."
    return "Mañana vuelve el horario normal."


# --- the round's inputs ------------------------------------------------------------------

@dataclass
class CamState:
    align: bool = False
    age_s: float | None = None          # absent: the api has no frame of that camera (its RAM is blank)
    black_for_s: float | None = None    # absent until the api measures black (Section 3.13)


@dataclass
class ApiState:
    ok: bool
    reason: str = ""
    up_s: float | None = None           # the api's own uptime, when it reports it (Section 3.13)
    cameras: dict[tuple[str, str], CamState] = field(default_factory=dict)
    read: set[str] = field(default_factory=set)      # sites whose camera state was read this round


@dataclass
class DbState:
    pending: int = 0
    working: int = 0
    last_done_at: datetime | None = None
    failed: list[dict[str, Any]] = field(default_factory=list)    # failures after the cursor, oldest first
    failed_newest: tuple[datetime | None, int] = (None, 0)       # the cursor's origin (baseline reads)
    log_rows: list[dict[str, Any]] = field(default_factory=list)  # bitácora rows after the cursor
    log_max_id: int = 0
    sites: list[dict[str, Any]] = field(default_factory=list)     # every sites row
    keys: dict[str, str] = field(default_factory=dict)            # site -> gafete (never logged)
    cambios: dict[str, list[dict[str, Any]]] = field(default_factory=dict)   # site -> its recent hour changes
    sharpness: dict[tuple[str, str], list[float]] = field(default_factory=dict)   # newest first


@dataclass(frozen=True)
class Msg:
    key: str
    values: dict[str, Any] = field(default_factory=dict)
    site: str | None = None


@dataclass(frozen=True)
class CamJudge:
    """What the vigilantes need about one camera in this round."""

    site: str
    camera: str
    inside: bool
    opened: datetime                   # the opening of the service day being judged
    last_frame_at: datetime | None
    boot_at: datetime                  # the api's boot: the stand-in for a camera with no frame
    window: timedelta                  # alerts.silent_site_min (this site's value)
    fresh: bool                        # a frame within the window: frames are flowing
    black_for_s: float | None
    black_timer_s: int
    cooldown: timedelta                # alerts.sharpness_cooldown_min (this site's value)


# --- the bot's RAM (disposable by construction; a restart restarts every clock) --------------

@dataclass
class BotState:
    baseline_ready: bool = False        # set by the first SUCCESSFUL db read: the cursors' origin
    failed_cursor: tuple[datetime | None, int] = (None, 0)
    log_cursor: int = 0
    backlog_active: bool = False
    worker_quiet: bool = False
    api_down_since: datetime | None = None
    api_last_reminder: datetime | None = None
    api_up_since: datetime | None = None
    lens_last: dict[tuple[str, str], datetime] = field(default_factory=dict)
    black_last: dict[tuple[str, str], datetime] = field(default_factory=dict)
    silent: set[tuple[str, str]] = field(default_factory=set)
    align_active: dict[tuple[str, str], bool] = field(default_factory=dict)
    ended: set[tuple[str, str]] = field(default_factory=set)    # later closes whose end was handled
    raw_log_first_seen: datetime | None = None
    raw_log_last_reminder: datetime | None = None


# --- the rules: pure functions returning the messages ---------------------------------------

def rule_api(state: BotState, api: ApiState, now: datetime) -> list[Msg]:
    out: list[Msg] = []
    if not api.ok:
        if state.api_down_since is None:
            state.api_down_since = state.api_last_reminder = now
            out.append(Msg("ventanilla_caida", {"motivo": api.reason}))
        elif now - (state.api_last_reminder or now) >= API_REMINDER:
            state.api_last_reminder = now
            minutos = int((now - state.api_down_since).total_seconds() // 60)
            out.append(Msg("ventanilla_sigue_caida", {"minutos": minutos, "motivo": api.reason}))
        return out
    if state.api_down_since is not None:
        minutos = int((now - state.api_down_since).total_seconds() // 60)
        state.api_down_since = state.api_last_reminder = None
        state.api_up_since = now
        out.append(Msg("ventanilla_volvio", {"minutos": minutos}))
    elif state.api_up_since is None:
        state.api_up_since = now
    return out


def rule_backlog(state: BotState, db: DbState, cfg: Config) -> list[Msg]:
    umbral = cfg.alerts.backlog_threshold
    if db.pending > umbral and not state.backlog_active:
        state.backlog_active = True
        return [Msg("cola_atorada", {"pendientes": db.pending, "umbral": umbral})]
    if db.pending <= umbral and state.backlog_active:
        state.backlog_active = False
        return [Msg("cola_normal", {"pendientes": db.pending})]
    return []


def rule_failures(state: BotState, db: DbState) -> list[Msg]:
    out: list[Msg] = []
    for job in db.failed:
        error = (job["last_error"] or "sin detalle")[:LAST_ERROR_MAX_CHARS]
        out.append(Msg("analisis_fallido", {"plato": job["plate_id"], "intentos": job["attempts"], "job": job["id"], "error": error}))
        state.failed_cursor = (job["finished_at"], int(job["id"]))
    return out


def rule_silent_worker(state: BotState, db: DbState, cfg: Config, now: datetime) -> list[Msg]:
    """😶 once while plates wait and nothing finishes; "cola en pausa" instead while the queue is paused."""
    window = cfg.alerts.worker_silence_min
    quiet = db.last_done_at is None or now - db.last_done_at > timedelta(minutes=window)
    if db.pending > 0 and quiet and not state.worker_quiet:
        state.worker_quiet = True
        if cfg.queue_paused:
            return [Msg("cola_en_pausa", {"pendientes": db.pending})]
        return [Msg("analista_callado", {"minutos": window, "pendientes": db.pending})]
    if state.worker_quiet and not quiet:
        state.worker_quiet = False
        return [Msg("analista_volvio")]
    return []


def rule_raw_log(state: BotState, cfg: Config, now: datetime) -> list[Msg]:
    """The "since when" clock is the bot's own first sighting, in RAM: late after a restart, never lost."""
    if not cfg.raw_logging:
        state.raw_log_first_seen = state.raw_log_last_reminder = None
        return []
    if state.raw_log_first_seen is None:
        state.raw_log_first_seen = now
        return []
    since = state.raw_log_last_reminder or state.raw_log_first_seen
    if now - since >= timedelta(hours=RAW_LOG_REMINDER_HOURS):
        state.raw_log_last_reminder = now
        return [Msg("forense_prendido", {"horas": int((now - state.raw_log_first_seen).total_seconds() // 3600)})]
    return []


def rule_lens(state: BotState, db: DbState, cfg: Config, now: datetime, judged_sites: Iterable[str]) -> list[Msg]:
    out: list[Msg] = []
    sites = set(judged_sites)
    for (site, camera), scores in db.sharpness.items():
        if site not in sites:
            continue
        n = knobs.effective(cfg, "alerts.sharpness_n", site)
        piso = knobs.effective(cfg, "alerts.sharpness_min", site)
        cooldown = timedelta(minutes=knobs.effective(cfg, "alerts.sharpness_cooldown_min", site))
        if len(scores) < n or any(s >= piso for s in scores[:n]):
            continue
        last = state.lens_last.get((site, camera))
        if last is not None and now - last < cooldown:
            continue
        state.lens_last[(site, camera)] = now
        out.append(Msg("lente_sucio", {"sitio": site, "camara": camera, "n": n,
                                       "valores": ", ".join(f"{s:.0f}" for s in scores[:n]), "piso": f"{piso:.0f}"},
                       site=site))
    return out


def rule_align(state: BotState, api: ApiState) -> list[Msg]:
    """🟡 when a camera's capture pauses for the alignment mode, 🟢 when it wakes. A first sighting only records."""
    out: list[Msg] = []
    for (site, camera), cam in api.cameras.items():
        key = (site, camera)
        was = state.align_active.get(key)
        if was is not None and cam.align != was:
            values = {"sitio": site, "camara": camera}
            out.append(Msg("alineacion_inicio" if cam.align else "alineacion_fin", values, site=site))
        state.align_active[key] = cam.align
    return out


def rule_silent_site(state: BotState, judged: Iterable[CamJudge], now: datetime) -> list[Msg]:
    """📵 once when a camera sends nothing for its window inside the horario; 🟢 when frames flow again. The clock
    starts at the later of its last frame (or the api's boot) and the opening; outside the horario it is reset."""
    out: list[Msg] = []
    for cam in judged:
        key = (cam.site, cam.camera)
        if not cam.inside:
            state.silent.discard(key)
            continue
        if cam.fresh:
            if key in state.silent:
                state.silent.discard(key)
                out.append(Msg("sitio_volvio", {"sitio": cam.site, "camara": cam.camera}, site=cam.site))
            continue
        quiet = now - max(cam.last_frame_at or cam.boot_at, cam.opened)
        if key not in state.silent and quiet > cam.window:
            state.silent.add(key)
            out.append(Msg("sitio_callado", {"sitio": cam.site, "camara": cam.camera,
                                             "minutos": int(quiet.total_seconds() // 60)}, site=cam.site))
    return out


def rule_black(state: BotState, judged: Iterable[CamJudge], now: datetime) -> list[Msg]:
    """🕶️ when a camera with flowing frames has seen black longer than black_timer_s inside the horario; outside it
    the night is black and never screams. A dead phone is the silent site's job, not this one's."""
    out: list[Msg] = []
    for cam in judged:
        if not cam.inside or not cam.fresh or cam.black_for_s is None or cam.black_for_s <= cam.black_timer_s:
            continue
        key = (cam.site, cam.camera)
        last = state.black_last.get(key)
        if last is not None and now - last < cam.cooldown:
            continue
        state.black_last[key] = now
        out.append(Msg("camara_tapada", {"sitio": cam.site, "camara": cam.camera, "segundos": int(cam.black_for_s)},
                       site=cam.site))
    return out


def rule_extension_end(state: BotState, nights: Mapping[str, tuple[HorarioConfig, ZoneInfo, list[dict[str, Any]]]],
                       now: datetime, announce: bool = True) -> list[Msg]:
    """🟢 once when a night that closes LATER than its regular close (/extender, or a later close by a one-day
    change) passes it. announce=False records without speaking (the boot baseline)."""
    out: list[Msg] = []
    for site, (horario, zone, cambios) in nights.items():
        today = now.astimezone(zone).date()
        for fecha in (today - timedelta(days=1), today):
            dia = dia_de(fecha, horario, zone, cambios)
            if dia.cerrado or dia.close <= dia.base.close or now < dia.close:
                continue
            key = (site, dia.close.isoformat())
            if key in state.ended:
                continue
            state.ended.add(key)
            if announce:
                out.append(Msg("extension_fin", {"sitio": site, "apertura": horario.open, "cierre": horario.close}, site=site))
    state.ended = {k for k in state.ended if datetime.fromisoformat(k[1]) > now - HORARIO_LOOKBACK}
    return out


def fecha_es(moment: datetime, zone: ZoneInfo) -> str:
    local = moment.astimezone(zone)
    return f"{local.day} {MESES[local.month - 1]} {local.year}"


def rule_bitacora(rows: Iterable[Mapping[str, Any]], cfg: Config, now: datetime) -> list[Msg]:
    """The bitácora rows the bot announces: the action and its detail, never who acted."""
    out: list[Msg] = []
    for row in rows:
        action, detail = row["action"], row.get("detail") or {}
        if action == "relevo":
            source = detail.get("source")
            fuente = FUENTES.get(source, source)
            if source == "phone" and detail.get("road") == "captura":
                fuente = "teléfono de emergencia"
            out.append(Msg("relevo", {"sitio": detail.get("site"), "camara": detail.get("camera"), "fuente": fuente},
                           site=detail.get("site")))
        elif action == "raw_logging.auto_off":
            out.append(Msg("forense_apagado"))
        elif action == "workers.reconverge":
            out.append(Msg("analistas_ajustados", {"deseados": detail.get("deseados"), "corriendo": detail.get("corriendo")}))
        elif action == "user.erase_soon":
            values: dict[str, Any] = {"usuario": detail.get("usuario"), "numero": detail.get("uid")}
            try:
                borra = datetime.fromisoformat(str(detail.get("borra")))
                values["dias"] = max(0, math.ceil((borra - now).total_seconds() / 86400))
                values["fecha"] = fecha_es(borra, ZoneInfo(cfg.timezone))
            except (TypeError, ValueError):
                pass
            out.append(Msg("cuenta_por_borrarse", values))
    return out


# --- gathering the inputs (threads: psycopg and urllib are sync) -------------------------------

def _http_get(path: str, key: str | None = None) -> tuple[int, bytes, dict[str, str]]:
    headers = {"User-Agent": USER_AGENT}
    if key:
        headers["X-Device-Key"] = key
    request = urllib.request.Request(API_BASE + path, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as resp:
            return resp.status, resp.read(), {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), {}


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def read_api(sites: Mapping[str, str | None]) -> ApiState:
    """/api/health, then each site's /api/camera/state with its gafete. A site whose state fails is not judged."""
    try:
        status, _, _ = _http_get("/api/health")
    except Exception as exc:
        return ApiState(ok=False, reason=f"sin conexión: {type(exc).__name__}")
    if status != 200:
        return ApiState(ok=False, reason=f"health {status}")
    api = ApiState(ok=True)
    for site, key in sites.items():
        try:
            st_status, body, _ = _http_get("/api/camera/state?site=" + urllib.parse.quote(site), key)
            if st_status != 200:
                log.warning("camera state of %s answered %s", site, st_status)
                continue
            data = json.loads(body)
        except Exception as exc:
            log.warning("camera state of %s failed: %s", site, type(exc).__name__)
            continue
        if api.up_s is None:
            api.up_s = _number(data.get("up_s"))
        for camera, entry in (data.get("cameras") or {}).items():
            api.cameras[(site, camera)] = CamState(
                align=bool((entry.get("align") or {}).get("active")),
                age_s=_number(entry.get("last_frame_age_s")),
                black_for_s=_number(entry.get("black_for_s")),
            )
        api.read.add(site)
    return api


def read_db(cfg: Config, state: BotState) -> DbState:
    """Everything the round needs from Postgres, read-only. Before the baseline only the cursors' origins are read."""
    db = DbState()
    with q.connect() as conn:
        db.sites = conn.execute(q.SELECT_SITES_ALL).fetchall()
        db.keys = {r["site"]: r["device_key"] for r in conn.execute(q.SELECT_SITE_KEYS).fetchall()}
        db.pending = int(conn.execute(q.COUNT_PENDING_JOBS).fetchone()["n"])
        db.working = int(conn.execute(q.COUNT_WORKING_JOBS).fetchone()["n"])
        db.last_done_at = conn.execute(q.BOT_LAST_DONE_AT).fetchone()["at"]
        if state.baseline_ready:
            after_at, after_id = state.failed_cursor
            db.failed = conn.execute(q.BOT_FAILED_AFTER, {"after_at": after_at, "after_id": after_id}).fetchall()
            db.log_rows = conn.execute(q.ADMIN_LOG_AFTER, {"after_id": state.log_cursor}).fetchall()
        else:
            newest = conn.execute(q.BOT_FAILED_CURSOR).fetchone()
            db.failed_newest = (newest["finished_at"], int(newest["id"])) if newest else (None, 0)
            db.log_max_id = int(conn.execute(q.ADMIN_LOG_MAX_ID).fetchone()["id"])
        for row in conn.execute(q.SELECT_RECENT_HORARIO, {"window": HORARIO_LOOKBACK}).fetchall():
            db.cambios.setdefault(row["site"], []).append(row)
        for row in db.sites:
            site = row["site"]
            if not row["active"] or site not in cfg.sites:
                continue
            n = knobs.effective(cfg, "alerts.sharpness_n", site)
            for camera in cfg.sites[site].cameras:
                rows = conn.execute(q.BOT_RECENT_SHARPNESS, {"site": site, "camera": camera, "n": n}).fetchall()
                db.sharpness[(site, camera)] = [float(r["sharpness"]) for r in rows]
    return db


def _site_rows() -> list[dict[str, Any]]:
    with q.connect() as conn:
        return conn.execute(q.SELECT_SITES_ALL).fetchall()


def site_chats(rows: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """The destinations map's input: every ACTIVE site that has a chat."""
    return {r["site"]: str(r["telegram_chat_id"]) for r in rows if r["active"] and r["telegram_chat_id"]}


# --- the commands' logic (sync, run in a thread; pure enough to test without Telegram) -----------

def group_sites(chat_id: str, rows: Iterable[Mapping[str, Any]], cfg: Config) -> list[str]:
    """The sites a group serves: those whose chat it is. The admin chat and the temporal .env chat serve them all."""
    active = [r for r in rows if r["active"]]
    if chat_id in (cfg.admin_chat_id, _env_chat()):
        return sorted(r["site"] for r in active)
    return sorted(r["site"] for r in active if str(r["telegram_chat_id"] or "") == chat_id)


def pick_site(sites: list[str], args: list[str]) -> tuple[str | None, list[str]]:
    """The site named first, or the group's only site; None when a group with several sites named none."""
    if args and args[0].lower() in sites:
        return args[0].lower(), args[1:]
    if len(sites) == 1:
        return sites[0], args
    return None, args


def _hace(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 120:
        return f"{seconds} s"
    if seconds < 7200:
        return f"{seconds // 60} min"
    if seconds < 172800:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} días"


def reply_id(chat_id: str) -> str:
    return textos.render("id_chat", {"chat_id": chat_id}, textos.load_textos())


def reply_cola(chat_id: str, args: list[str]) -> str:
    overrides = textos.load_textos()
    try:
        cfg = load_config()
        rows = _site_rows()
        sites = group_sites(chat_id, rows, cfg)
        if not sites:
            return textos.render("grupo_sin_sitio", {}, overrides)
        site, _ = pick_site(sites, args)
        if site is None:
            return textos.render("comando_pide_sitio", {"sitios": ", ".join(sites), "comando": "cola", "ejemplo": sites[0]}, overrides)
        with q.connect() as conn:
            counts = conn.execute(q.BOT_SITE_QUEUE, {"site": site}).fetchone()
            last = conn.execute(q.BOT_LAST_DONE_SITE, {"site": site}).fetchone()
            names = {r["dish_id"]: r["nombre"] for r in conn.execute(q.SELECT_DISH_NAMES).fetchall()}
    except Exception as exc:
        return textos.render("cola_error", {"motivo": type(exc).__name__}, overrides)
    values: dict[str, Any] = {"sitio": site, "pendientes": int(counts["pending"]), "en_proceso": int(counts["working"])}
    if last is None:
        return textos.render("cola_sin_platos", values, overrides)
    dish = last["dish"]
    values.update(plato=last["plate_id"], platillo=names.get(dish, dish) if dish else "sin platillo",
                  hace=_hace(float(last["age_s"])))
    return textos.render("cola_estado", values, overrides)


def reply_foto(chat_id: str, args: list[str]) -> tuple[bytes | None, str]:
    """(photo, caption) when the camera has a fresh copy, else (None, the answer)."""
    overrides = textos.load_textos()
    try:
        cfg = load_config()
        rows = _site_rows()
    except Exception as exc:
        return None, textos.render("foto_error", {"motivo": type(exc).__name__}, overrides)
    sites = group_sites(chat_id, rows, cfg)
    if not sites:
        return None, textos.render("grupo_sin_sitio", {}, overrides)
    site, rest = pick_site(sites, args)
    if site is None:
        return None, textos.render("comando_pide_sitio", {"sitios": ", ".join(sites), "comando": "foto", "ejemplo": sites[0]}, overrides)
    cameras = list(cfg.sites[site].cameras) if site in cfg.sites else []
    if not cameras:
        return None, textos.render("sitio_sin_camaras", {"sitio": site}, overrides)
    camera = rest[0].lower() if rest and rest[0].lower() in cameras else cameras[0]
    try:
        with q.connect() as conn:
            row = conn.execute(q.SELECT_DEVICE_KEY, {"site": site}).fetchone()
        key = row["device_key"] if row else None
        path = "/api/camera/frame?" + urllib.parse.urlencode({"site": site, "camera": camera})
        status, body, headers = _http_get(path, key)
    except Exception as exc:
        return None, textos.render("foto_error", {"motivo": type(exc).__name__}, overrides)
    if status == 404:
        return None, textos.render("foto_sin_copia", {"sitio": site, "camara": camera}, overrides)
    if status != 200:
        return None, textos.render("foto_error", {"motivo": f"respuesta {status}"}, overrides)
    caption = textos.render("foto_pie", {"sitio": site, "camara": camera, "nitidez": headers.get("x-sharpness", "?"),
                                         "edad": headers.get("x-frame-age-s", "?")}, overrides)
    return body, caption


def _cambios_de(conn: Any, site: str) -> list[dict[str, Any]]:
    return [r for r in conn.execute(q.SELECT_RECENT_HORARIO, {"window": HORARIO_LOOKBACK}).fetchall() if r["site"] == site]


def _escribe_cambio(conn: Any, site: str, nuevo: Dia, zone: ZoneInfo, chat_id: str, quien: str, comando: str) -> None:
    """The bot's only writes, inside the caller's transaction: the day's whole new state, and its bitácora row
    (who wrote it in Telegram and the command, never the chat's id)."""
    conn.execute(q.INSERT_HORARIO_CAMBIO, {
        "site": site, "dia": nuevo.fecha, "abre_at": nuevo.open,
        "until": nuevo.base.close if nuevo.cerrado else nuevo.close, "cerrado": nuevo.cerrado, "chat_id": chat_id,
    })
    detail = {
        "sitio": site, "dia": nuevo.fecha.isoformat(), "comando": comando, "cerrado": nuevo.cerrado,
        "abre": None if nuevo.cerrado else _hhmm(nuevo.open, zone),
        "cierra": None if nuevo.cerrado else _hhmm(nuevo.close, zone),
    }
    conn.execute(q.INSERT_ADMIN_LOG, {"usuario": "telegram: " + quien, "action": BITACORA_HORARIO, "detail": Jsonb(detail)})


def reply_extender(chat_id: str, args: list[str], quien: str = "?", now: datetime | None = None) -> str:
    """/extender <HH:MM|+2h>, or /extender <sitio> <HH:MM|+2h> in a group with several sites. Its change is that
    night's new close, written like every hour change (the day's row plus its bitácora row)."""
    overrides = textos.load_textos()
    now = now or _now()
    try:
        cfg = load_config()
        rows = _site_rows()
    except Exception as exc:
        return textos.render("extension_rechazada", {"sitio": "?", "motivo": f"no pude leer la configuración ({type(exc).__name__})."}, overrides)
    sites = group_sites(chat_id, rows, cfg)
    if not sites:
        return textos.render("grupo_sin_sitio", {}, overrides)
    site, rest = pick_site(sites, args)
    if site is None:
        return textos.render("extension_pide_sitio", {"sitios": ", ".join(sites), "ejemplo": sites[0]}, overrides)
    pedido = parse_extension(rest[0]) if rest else None
    if pedido is None:
        return textos.render("extension_uso", {}, overrides)
    horario = knobs.horario_of(cfg, site)
    zone = ZoneInfo(knobs.timezone_of(cfg, site))
    try:
        with q.connect() as conn, conn.transaction():
            dia, until, motivo = resolve_extension(now, horario, zone, _cambios_de(conn, site), pedido)
            if dia is None or until is None:
                return textos.render("extension_rechazada", {"sitio": site, "motivo": motivo}, overrides)
            _escribe_cambio(conn, site, replace(dia, close=until), zone, chat_id, quien, "/extender " + " ".join(args))
    except Exception as exc:
        return textos.render("extension_rechazada", {"sitio": site, "motivo": f"no pude guardarla ({type(exc).__name__})."}, overrides)
    log.info("extension written for %s until %s", site, until.isoformat())
    return textos.render("extension_ok", {"sitio": site, "hora": _hhmm(until, zone)}, overrides)


def reply_horario(chat_id: str, comando: str, args: list[str], quien: str = "?", now: datetime | None = None) -> str:
    """/hoy, /manana, /abrir and /cerrar (Section 3.13.1). Showing the hours writes nothing; a change writes the
    day's row and its bitácora row in one transaction, and its answer always names the day."""
    overrides = textos.load_textos()
    now = now or _now()
    try:
        cfg = load_config()
        rows = _site_rows()
    except Exception as exc:
        return textos.render("horario_error", {"sitio": "?", "motivo": type(exc).__name__}, overrides)
    sites = group_sites(chat_id, rows, cfg)
    if not sites:
        return textos.render("grupo_sin_sitio", {}, overrides)
    site, rest = pick_site(sites, args)
    if site is None:
        return textos.render("comando_pide_sitio", {"sitios": ", ".join(sites), "comando": comando, "ejemplo": sites[0]}, overrides)
    orden = parse_orden(comando, rest)
    if orden is None:
        return textos.render("horario_uso", {}, overrides)
    if orden[0] == "lejos":
        motivo = "solo se cambia hoy o mañana, y solo por ese día. El horario de los demás días se cambia en el mostrador."
        return textos.render("horario_rechazado", {"sitio": site, "motivo": motivo}, overrides)
    horario = knobs.horario_of(cfg, site)
    zone = ZoneInfo(knobs.timezone_of(cfg, site))
    try:
        with q.connect() as conn, conn.transaction():
            cambios = _cambios_de(conn, site)
            hoy, _ = hoy_de(now, horario, zone, cambios)
            if orden[0] == "ver":
                dia = dia_de(hoy.fecha + timedelta(days=orden[1]), horario, zone, cambios)
                values = {"sitio": site, "dia": _cuando(dia.fecha, hoy.fecha), "horas": horas_txt(dia, zone)}
                return textos.render("horario_dia", values, overrides)
            _, nuevo, motivo = resolve_cambio(now, horario, zone, cambios, orden)
            if nuevo is None:
                return textos.render("horario_rechazado", {"sitio": site, "motivo": motivo}, overrides)
            _escribe_cambio(conn, site, nuevo, zone, chat_id, quien, " ".join(["/" + comando, *args]))
    except Exception as exc:
        if orden[0] == "ver":
            return textos.render("horario_error", {"sitio": site, "motivo": type(exc).__name__}, overrides)
        return textos.render("horario_rechazado", {"sitio": site, "motivo": f"no pude guardarlo ({type(exc).__name__})."}, overrides)
    log.info("hours change written for %s on %s", site, nuevo.fecha.isoformat())
    values = {"sitio": site, "dia": _cuando(nuevo.fecha, hoy.fecha), "cambio": cambio_txt(orden, nuevo, zone),
              "vuelta": vuelta_txt(nuevo, hoy, horario, zone, cambios)}
    return textos.render("horario_cambiado", values, overrides)


# --- the bot ----------------------------------------------------------------------------------

class Bot:
    def __init__(self) -> None:
        self.state = BotState()
        self.cfg: Config | None = None            # the last config that loaded (a broken edit never stops the bot)
        self.chats: dict[str, str] = {}           # the last destinations map read from the database
        self.task: asyncio.Task[None] | None = None

    def config(self) -> Config:
        try:
            self.cfg = load_config()
        except Exception as exc:
            if self.cfg is None:
                raise
            log.error("config.yaml unreadable (%s): the last good config is used", type(exc).__name__)
        return self.cfg

    def targets(self, msg: Msg, cfg: Config) -> list[str]:
        muted = frozenset(site for site, site_cfg in cfg.sites.items() if site_cfg.mantenimiento)
        familia = textos.FACTORY[msg.key].familia
        return textos.destinations(familia, site=msg.site, site_chats=self.chats, admin_chat=cfg.admin_chat_id,
                                   env_chat=_env_chat(), muted=muted)

    async def send(self, app: Application, msg: Msg, cfg: Config, overrides: Mapping[str, str]) -> None:
        chats = self.targets(msg, cfg)
        if not chats:
            log.warning("sin destino para %s (sitio %s)", msg.key, msg.site or "-")
            return
        text = textos.render(msg.key, msg.values, overrides)
        for chat in chats:
            try:
                await app.bot.send_message(chat_id=chat, text=text)
            except Exception as exc:
                log.error("send of %s failed (%s)", msg.key, type(exc).__name__)
        log.info("sent %s to %d chat(s)", msg.key, len(chats))

    def judge(self, cfg: Config, db: DbState, api: ApiState, now: datetime, sites: list[str]) -> list[CamJudge]:
        boot_at = now - timedelta(seconds=api.up_s) if api.up_s is not None else (self.state.api_up_since or now)
        judged: list[CamJudge] = []
        for site in sites:
            if site not in api.read:
                continue
            horario = knobs.horario_of(cfg, site)
            zone = ZoneInfo(knobs.timezone_of(cfg, site))
            dia, is_inside = hoy_de(now, horario, zone, db.cambios.get(site, []))
            opened = dia.open or dia.base.open
            window = timedelta(minutes=knobs.effective(cfg, "alerts.silent_site_min", site))
            timer = knobs.effective(cfg, "alerts.black_timer_s", site)
            cooldown = timedelta(minutes=knobs.effective(cfg, "alerts.sharpness_cooldown_min", site))
            for camera in cfg.sites[site].cameras:
                cam = api.cameras.get((site, camera), CamState())
                last = now - timedelta(seconds=cam.age_s) if cam.age_s is not None else None
                judged.append(CamJudge(site=site, camera=camera, inside=is_inside, opened=opened, last_frame_at=last,
                                       boot_at=boot_at, window=window, fresh=last is not None and now - last <= window,
                                       black_for_s=cam.black_for_s, black_timer_s=timer, cooldown=cooldown))
        return judged

    async def round(self, app: Application, now: datetime | None = None) -> list[Msg]:
        cfg = self.config()
        overrides = textos.load_textos()
        now = now or _now()
        state = self.state
        try:
            db: DbState | None = await asyncio.to_thread(read_db, cfg, state)
        except Exception as exc:
            log.warning("db round failed: %s", type(exc).__name__)
            db = None
        messages: list[Msg] = []
        if db is not None:
            self.chats = site_chats(db.sites)
            active = [r["site"] for r in db.sites if r["active"] and r["site"] in cfg.sites]
            muted = {site for site in active if cfg.sites[site].mantenimiento}
            judged_sites = [site for site in active if site not in muted]
            for site in muted:
                state.silent = {k for k in state.silent if k[0] != site}
            nights = {site: (knobs.horario_of(cfg, site), ZoneInfo(knobs.timezone_of(cfg, site)), db.cambios.get(site, []))
                      for site in active}
            api = await asyncio.to_thread(read_api, {site: db.keys.get(site) for site in active})
        else:
            judged_sites, nights = [], {}
            api = await asyncio.to_thread(read_api, {})
        messages += rule_api(state, api, now)
        if api.ok:
            messages += rule_align(state, api)
        if db is not None:
            if not state.baseline_ready:
                # The cursors start at the first SUCCESSFUL read: old failures, old bitácora rows and later closes
                # that already ended are never announced, even when the first rounds fail.
                state.failed_cursor, state.log_cursor = db.failed_newest, db.log_max_id
                state.baseline_ready = True
                rule_extension_end(state, nights, now, announce=False)
            else:
                messages += rule_failures(state, db)
                messages += rule_bitacora(db.log_rows, cfg, now)
                if db.log_rows:
                    state.log_cursor = max(int(r["id"]) for r in db.log_rows)
                messages += rule_extension_end(state, nights, now)
            messages += rule_backlog(state, db, cfg)
            messages += rule_silent_worker(state, db, cfg, now)
            messages += rule_lens(state, db, cfg, now, judged_sites)
            if api.ok:
                judged = self.judge(cfg, db, api, now, judged_sites)
                messages += rule_silent_site(state, judged, now)
                messages += rule_black(state, judged, now)
        messages += rule_raw_log(state, cfg, now)
        for msg in messages:
            await self.send(app, msg, cfg, overrides)
        return messages

    def poll_s(self) -> int:
        return self.cfg.alerts.poll_s if self.cfg is not None else POLL_S_DEFAULT

    async def greet(self, app: Application) -> None:
        try:
            cfg = self.config()
            try:
                self.chats = site_chats(await asyncio.to_thread(_site_rows))
            except Exception as exc:
                log.warning("greeting without the database (%s): the temporal chat is the only destination", type(exc).__name__)
            await self.send(app, Msg("saludo"), cfg, textos.load_textos())
        except Exception as exc:
            log.error("greeting failed: %s", type(exc).__name__)

    async def loop(self, app: Application) -> None:
        await self.greet(app)
        while True:
            try:
                await self.round(app)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("round crashed: %s", type(exc).__name__)
            await asyncio.sleep(self.poll_s())

    async def stop(self) -> None:
        """The shutdown cancel: the round task dies with the bot instead of outliving it."""
        task, self.task = self.task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        log.info("bot stopped: round task cancelled")

    # --- commands ---
    async def cmd_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(reply_id(str(update.effective_chat.id)))

    async def cmd_cola(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = await asyncio.to_thread(reply_cola, str(update.effective_chat.id), list(context.args or []))
        await update.effective_message.reply_text(text)

    async def cmd_foto(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        photo, text = await asyncio.to_thread(reply_foto, str(update.effective_chat.id), list(context.args or []))
        if photo is None:
            await update.effective_message.reply_text(text)
        else:
            await update.effective_message.reply_photo(photo=photo, caption=text)

    async def cmd_extender(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = await asyncio.to_thread(reply_extender, str(update.effective_chat.id), list(context.args or []),
                                       quien_de(update))
        await update.effective_message.reply_text(text)

    async def horario(self, update: Update, comando: str, args: list[str]) -> None:
        text = await asyncio.to_thread(reply_horario, str(update.effective_chat.id), comando, args, quien_de(update))
        await update.effective_message.reply_text(text)

    async def cmd_hoy(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.horario(update, "hoy", list(context.args or []))

    async def cmd_manana(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.horario(update, "manana", list(context.args or []))

    async def cmd_abrir(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.horario(update, "abrir", list(context.args or []))

    async def cmd_cerrar(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.horario(update, "cerrar", list(context.args or []))

    async def msg_manana_enie(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/mañana with the ñ (Telegram ends the command at the ñ): it works exactly like /manana. Written to
        another bot (/mañana@OtroBot), it is not for this one, as with every command."""
        text = update.effective_message.text or ""
        found = _MANANA_ENIE_RE.match(text)
        if found is None or (found.group(1) and found.group(1).lower() != (context.bot.username or "").lower()):
            return
        await self.horario(update, "manana", text.split()[1:])


def quien_de(update: Update) -> str:
    """The Telegram name of who wrote a command, for the bitácora row of an hour change."""
    user = update.effective_user
    return ((user.full_name if user else "") or "").strip()[:QUIEN_MAX] or "?"


def main() -> None:
    token = os.environ.get(ENV_TOKEN, "").strip()
    if not token:
        raise RuntimeError(f"{ENV_TOKEN} is not set: is .env present and passed through env_file?")
    raw_log.add_service_log_file("bot")          # the machine room's viewer tails /data/logs/bot.log
    bot = Bot()
    cfg = bot.config()
    log.info("bot starting: poll_s=%d sites=%s", cfg.alerts.poll_s, list(cfg.sites))

    async def post_init(app: Application) -> None:
        bot.task = asyncio.create_task(bot.loop(app), name="lpq-bot-rounds")

    async def post_stop(app: Application) -> None:
        await bot.stop()

    app = Application.builder().token(token).post_init(post_init).post_stop(post_stop).build()
    app.add_handler(CommandHandler("id", bot.cmd_id))
    app.add_handler(CommandHandler("cola", bot.cmd_cola))
    app.add_handler(CommandHandler("foto", bot.cmd_foto))
    app.add_handler(CommandHandler("extender", bot.cmd_extender))
    app.add_handler(CommandHandler("hoy", bot.cmd_hoy))
    app.add_handler(CommandHandler("manana", bot.cmd_manana))
    app.add_handler(CommandHandler("abrir", bot.cmd_abrir))
    app.add_handler(CommandHandler("cerrar", bot.cmd_cerrar))
    app.add_handler(MessageHandler(filters.Regex(_MANANA_ENIE_RE), bot.msg_manana_enie))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
