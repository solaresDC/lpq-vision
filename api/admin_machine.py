"""LPQ_VISION api/admin_machine.py: the MACHINE ROOM, the second floor (SPEC 1.6, 1.7; a family of the admin hub;
Section 3.15).

Every endpoint here needs role admin AND a live machine grant on THIS session (the hub's require_machine: the
cuarto password opens the room for MACHINE_GRANT_MIN minutes). Every action is the hub's double confirm with its
visible diff, and leaves ONE bitácora row when it happened; an action refused changed nothing and writes none.

GET  /api/admin/machine                the room: its knobs (the model, its answer length, the queue constants, the
                                       sessions), the keyed models, the analysts wanted and running, the butler, the
                                       reboot notice
POST /api/admin/machine/knob           one second-floor knob, step 1 (the machine block's assert bounces here, verbatim)
POST /api/admin/machine/workers        the analysts' apply button, step 1: machine.workers AND the butler's scale
POST /api/admin/machine/restart        restart ONE service (fastapi, worker or bot; postgres never), step 1
POST /api/admin/machine/reboot         the whole server, step 1: a notice to every chat, then the butler's reboot
POST /api/admin/machine/password       a new cuarto password, step 1 (the room closes in every other session)
GET  /api/admin/status                 the post-reboot checklist: api, database, migrations, queue, analysts, cameras,
                                       bot and butler
GET  /api/admin/logs                   the log files the viewer can open
GET  /api/admin/logs/stream?archivo=   the live viewer, SSE's one home: the last LOG_TAIL_LINES lines, then every new
                                       one, until the grant ends

The butler (SPEC 1.7) is a host service deaf to every network: one plain text line through its unix-socket mailbox
BUTLER_SOCKET, one line back ("ok <id>" or "rechazado: <motivo>"). Its vocabulary is closed (reboot, scale worker
1..4, restart fastapi|worker|bot) and this module builds no other line. A connect that sends nothing is its
liveness probe. The butler is a service of the server's host only (from 3.18; never one on the laptop), so where the
mailbox is absent every action that needs it is refused BEFORE anything changes, and the reboot before anyone is
warned.

The running analysts are counted by the compose network's DNS (one address per living worker; 0 when that name is
unknown while postgres resolves): read-only, no docker socket; a lookup that fails is "?", never a guess. At boot,
after RECONVERGE_SETTLE_S and two equal counts, the api compares the count with machine.workers (the remembered
desire: 0 running is a difference too) and calls the butler ONCE when they differ: its row workers.reconverge is
what the bot announces. A butler answer that cannot say whether it acted is never announced as a refusal.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import socket
import stat
import time
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from math import ceil
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from api import admin as hub
from api import config_writer as cw
from api import routes as lane
from api.admin import FLOOR_MAQUINAS, propose, require_machine
from api.auth import Session, machine_grant_left, make_hash, revoke_machine_others
from brain.adapter.raw_log import LOG_DIR
from brain.db import queries as q
from brain.validator import knobs, textos
from brain.validator.models import (
    CONTENDERS,
    MACHINE_USER,
    WORKER_SCALE_MAX,
    Config,
    MachineConfig,
    MachineKnobRequest,
    PasswordRequest,
    RestartRequest,
    WorkersRequest,
    keyed_contenders,
    load_config,
)

log = logging.getLogger("lpq.api.admin_machine")

router = APIRouter()

# --- named constants (SPEC section 4) -------------------------------------------------
BUTLER_SOCKET = "/run/lpq-butler/butler.sock"   # the butler's mailbox, bind-mounted into fastapi only (from 3.16)
BUTLER_WINDOW_MIN = 10            # the butler takes one identical reboot or restart line per this many minutes
BUTLER_CONNECT_S = 2              # a mailbox that takes no connect in this time is absent
BUTLER_REPLY_S = 120              # the longest a verb may take to answer (a scale starts containers)
BUTLER_REPLY_MAX = 512            # bytes of the butler's one reply line
REBOOT_GRACE_S = 180              # a reboot line the butler took without a word: alive after this = it did not happen
RESTART_MARK_S = 300              # how long the ventanilla's own restart, once taken, keeps reboots away
RECONVERGE_SETTLE_S = 90          # the boot reconvergence waits this long before its first count
RECONVERGE_RETRY_S = 10           # between two counts
RECONVERGE_WINDOW_S = 120         # it keeps counting this long for two equal answers, then gives up
CENSUS_HOST = "worker"            # the compose service whose addresses are the running analysts
CENSUS_PROBE_HOST = "postgres"    # always on the compose network: it resolves and "worker" does not = none runs
CENSUS_TIMEOUT_S = 3
LOG_TAIL_LINES = 200              # the viewer opens with this many lines
LOG_TAIL_BYTES = 256 * 1024       # how far back the opening read looks for them
LOG_READ_MAX = 512 * 1024         # the most one round reads; beyond it the viewer skips ahead and says so
LOG_POLL_S = 1.0                  # how often the viewer looks for new lines
LOG_KEEPALIVE_S = 15              # a quiet stream sends a comment this often (a closed page is noticed)
LOG_GONE_S = 5                    # a file missing this long (longer than a rotation's blink) ends the viewer
LOG_LINE_MAX = 2000               # characters of one line in the viewer
BOT_SEEN_MIN_S = 60               # the bot counts as alive when it called within max(this, 3 of its rounds)

VERB_RE = re.compile(rf"^(reboot|scale worker [1-{WORKER_SCALE_MAX}]|restart (fastapi|worker|bot))\Z")
REPLY_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}\Z")
LOG_NAME_RE = re.compile(r"^(fastapi|bot|worker-[A-Za-z0-9][A-Za-z0-9_.-]{0,62})\.log\Z")

# When a second-floor knob takes effect: the confirm says it beside the change.
DESDE: dict[str, str] = {
    "active_model": "El siguiente plato ya lo analiza el modelo nuevo.",
    "machine.session_hours": "Cada sesión abierta se juzga con el valor nuevo desde su siguiente clic.",
}
DESDE_DEFAULT = "Los analistas lo leen en su siguiente plato."

SERVICIOS: dict[str, str] = {
    "fastapi": "la ventanilla (fastapi)",
    "worker": "los analistas (worker)",
    "bot": "el bot (bot)",
}
REINICIO_DE: dict[str, str] = {
    "fastapi": (
        "⚠️ Es la ventanilla MISMA: esta página, el mostrador y las cámaras pierden la conexión unos segundos, y un "
        "plato que llegue a una cámara en esos segundos no se guarda. Las sesiones viven en su memoria: al volver, "
        "todos entran otra vez y el cuarto se abre de nuevo."
    ),
    "worker": (
        "Los analistas se detienen y vuelven a arrancar. Los platos que llegan siguen entrando a la cola y esperan; "
        "uno que estaba a medio analizar se rescata solo después y se analiza de nuevo."
    ),
    "bot": (
        "El bot se detiene y vuelve a arrancar: saluda en cada chat al volver. Lo que pase mientras está "
        "apagado no se anuncia después."
    ),
}

# The one reboot in flight: its deadline (monotonic) while the notice's minute runs, its task, and how the last one
# ended in THIS process (the page reads it: a reboot that happened leaves no process to remember it).
_REBOOT: dict[str, Any] = {"deadline": None, "task": None, "last": None, "announcing": False}
_REBOOT_LOCK = asyncio.Lock()
_SCALE_LOCK = asyncio.Lock()      # one scale at a time: Aplicar and the boot reconvergence never cross
# Restart lines out, by service (monotonic time): no reboot is announced meanwhile. The ventanilla's own mark stays
# after the butler took it (this process is about to end) for at most RESTART_MARK_S, so a restart that never came
# cannot block reboots for good.
_RESTARTING: dict[str, float] = {}
# The butler's calls wait up to BUTLER_REPLY_S: their own two threads, never the pool every database call shares.
_BUTLER_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="lpq-butler")


class ButlerNote(Exception):
    """A butler answer that is not an "ok": the HTTP status and the person's sentence."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


# --- the butler's mailbox ---------------------------------------------------------------

def butler_probe() -> bool:
    """The liveness probe: connect and close, no verb sent (the butler answers it by closing)."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(BUTLER_CONNECT_S)
            sock.connect(BUTLER_SOCKET)
        return True
    except OSError:
        return False


def butler_call(line: str) -> tuple[str, str]:
    """ONE verb through the mailbox (sync: call it off the event loop, through _butler). Answers (verdict, info):
    ("ok", id), ("rechazado", motivo), ("ausente", why) when the mailbox took nothing, or ("sin_respuesta", why)
    when the line went out and no readable answer came back: the butler may have acted, or not."""
    if not VERB_RE.match(line):
        raise ValueError(f"not a butler verb: {line!r}")
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError as exc:
        return "ausente", type(exc).__name__
    with sock:
        sock.settimeout(BUTLER_CONNECT_S)
        try:
            sock.connect(BUTLER_SOCKET)
            sock.sendall((line + "\n").encode("ascii"))
        except OSError as exc:
            return "ausente", type(exc).__name__
        sock.settimeout(BUTLER_REPLY_S)
        data = b""
        try:
            while b"\n" not in data and len(data) < BUTLER_REPLY_MAX:
                chunk = sock.recv(BUTLER_REPLY_MAX - len(data))
                if not chunk:
                    break
                data += chunk
        except TimeoutError:
            return "sin_respuesta", f"no contestó en {BUTLER_REPLY_S} s"
        except ConnectionResetError:              # it closed with our line unread: it took nothing
            return "ausente", "cerró sin leer la orden"
        except OSError:
            return "sin_respuesta", "se cortó la conexión"
    reply = data.split(b"\n", 1)[0].decode("utf-8", "replace").strip()
    if reply.startswith("ok "):
        ident = reply[3:].strip()
        if REPLY_ID_RE.match(ident):
            return "ok", ident
        return "sin_respuesta", "contestó algo que no se entiende"
    if reply.startswith("rechazado:"):
        motivo = reply[len("rechazado:"):].strip() or "sin motivo"
        return "rechazado", motivo[:200]
    return "sin_respuesta", "contestó algo que no se entiende" if reply else "cerró sin contestar"


async def _butler(line: str) -> tuple[str, str]:
    return await asyncio.get_running_loop().run_in_executor(_BUTLER_POOL, butler_call, line)


def _butler_note(verdict: str, info: str) -> ButlerNote:
    """The person's sentence for a butler answer that is not an "ok"."""
    if verdict == "rechazado":
        return ButlerNote(409, "El mayordomo no lo aceptó: " + info + ". Nada cambió.")
    if verdict == "ausente":
        return ButlerNote(503, "El mayordomo no responde: nada cambió.")
    return ButlerNote(504, "El mayordomo tomó la orden pero " + info + ": puede que sí la haya hecho. Mira la lista de "
                      "arranque en un minuto.")


async def _row(usuario: str | None, action: str, detail: dict[str, Any], uid: int | None = None) -> None:
    """The bitácora row of an act that ALREADY happened (the butler took it, or every chat was warned): best-effort,
    so a database that fails right then never turns a done act into an error, nor stops what must follow."""
    try:
        await asyncio.to_thread(hub.log_action, usuario, action, detail, uid)
    except Exception as exc:
        log.warning("bitácora row %s not written (%s): the act stands", action, type(exc).__name__)


def _scale_line(count: int) -> str:
    if not 1 <= count <= WORKER_SCALE_MAX:
        raise ValueError(f"workers out of range: {count}")
    return f"scale worker {count}"


# --- the census -----------------------------------------------------------------------

def _addresses(host: str) -> set[str]:
    infos = socket.getaddrinfo(host, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
    return {info[4][0] for info in infos}


def census() -> int | None:
    """How many analysts run now: the addresses the compose network's DNS gives for the worker service (one per
    living replica). 0 when that name is unknown while postgres resolves (the DNS works and no worker lives); None
    ("?") for anything else (a DNS that fails, a timeout): never a guess."""
    try:
        return len(_addresses(CENSUS_HOST)) or None
    except socket.gaierror as exc:
        if exc.errno != socket.EAI_NONAME:
            return None
    except OSError:
        return None
    try:
        return 0 if _addresses(CENSUS_PROBE_HOST) else None
    except OSError:
        return None


async def census_now() -> int | None:
    try:
        return await asyncio.wait_for(asyncio.to_thread(census), CENSUS_TIMEOUT_S)
    except TimeoutError:
        return None


# --- small helpers ----------------------------------------------------------------------

def _ago(seconds: float) -> str:
    s = max(0, round(seconds))
    if s < 60:
        return f"hace {s} s"
    if s < 3600:
        return f"hace {round(s / 60)} min"
    if s < 86400:
        return f"hace {round(s / 3600)} h"
    return f"hace {round(s / 86400)} d"


def _local(moment: datetime, cfg: Config) -> str:
    return moment.astimezone(ZoneInfo(cfg.timezone)).strftime("%d/%m %H:%M")


def _first_message(exc: ValidationError) -> str:
    err = exc.errors()[0]
    return str(err.get("msg", "valor inválido")).removeprefix("Value error, ")


def _bounce(cfg: Config, knob: knobs.Knob, value: Any) -> None:
    """The machine block's load-time assert, run BEFORE the confirm so the person reads it at once, in the same
    words the writer would bounce with ("machine: ...")."""
    if knob.path[0] != "machine":
        return
    data = cfg.machine.model_dump()
    data[knob.path[1]] = value
    try:
        MachineConfig.model_validate(data)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail="machine: " + _first_message(exc)) from None


def _keyed_models() -> list[str]:
    return [c.model for c in keyed_contenders()]


def _notice_minutes() -> int:
    return max(1, ceil(textos.REBOOT_NOTICE_DELAY.total_seconds() / 60))


def _every_chat() -> list[str]:
    """Sync (database and config): every configured chat, the 'all' family. Chat ids are never logged."""
    return textos.destinations("all", **hub.destinations_input())


def _every_chat_or_none() -> list[str] | None:
    """The same, or None when the database does not answer: the room still opens (a restart needs no database)."""
    try:
        return _every_chat()
    except psycopg.Error as exc:
        log.warning("the chats could not be read (%s)", type(exc).__name__)
        return None


def _text(key: str, values: dict[str, Any]) -> str:
    return textos.render(key, values, overrides=textos.load_textos())


async def _send_every(chats: list[str], text: str) -> list[str]:
    """Send `text` to each chat; returns the chats it reached. A failed send never stops the rest."""
    reached: list[str] = []
    for chat in chats:
        try:
            ok, _detail = await asyncio.to_thread(hub.send_telegram, chat, text)
        except Exception as exc:
            log.warning("a reboot message was not sent (%s)", type(exc).__name__)
            ok = False
        if ok:
            reached.append(chat)
    return reached


def _reboot_left() -> int | None:
    """None: no reboot in flight. Else the seconds until the butler gets the line (0: it has it, or the api waits to
    learn whether it happened)."""
    deadline = _REBOOT["deadline"]
    if deadline is None:
        return None
    return max(0, ceil(deadline - time.monotonic()))


def _reboot_busy() -> str | None:
    if _REBOOT["announcing"]:
        return "Se está anunciando un reinicio del servidor: espera un momento."
    left = _reboot_left()
    if left is None:
        return None
    if left > 0:
        return f"Ya hay un reinicio del servidor en camino: llega en {left} s."
    return "Ya hay un reinicio del servidor en camino: el mayordomo ya lo tiene. Espera a que termine."


def _reboot_last() -> dict[str, Any] | None:
    last = _REBOOT["last"]
    if last is None:
        return None
    return {"estado": last["estado"], "motivo": last["motivo"], "hace_s": max(0, round(time.time() - last["ts"])),
            "avisados": last["avisados"], "de": last["de"]}


def _last_reboot_at() -> datetime | None:
    with q.connect() as conn:
        return conn.execute(q.LAST_REBOOT_AT).fetchone()["ts"]


# --- the room ---------------------------------------------------------------------------

@router.get("/api/admin/machine")
async def room(session: Session = Depends(require_machine)) -> dict[str, Any]:
    cfg = await asyncio.to_thread(load_config)
    described, responde, corriendo, chats, aviso = await asyncio.gather(
        asyncio.to_thread(knobs.describe, cfg, floor=FLOOR_MAQUINAS),
        asyncio.to_thread(butler_probe),
        census_now(),
        asyncio.to_thread(_every_chat_or_none),
        asyncio.to_thread(_text, "aviso_reinicio", {"minutos": _notice_minutes()}),
    )
    keyed = keyed_contenders()
    return {
        "grant_s": machine_grant_left(session),
        "knobs": described,
        "modelos": [{"model": c.model, "label": c.label, "lane": c.lane} for c in keyed],
        "modelo_en_uso": {"model": cfg.active_model, "con_llave": cfg.active_model in {c.model for c in keyed}},
        "analistas": {"deseados": cfg.machine.workers, "corriendo": corriendo, "max": WORKER_SCALE_MAX},
        "mayordomo": {"responde": responde},
        "servicios": [{"servicio": k, "nombre": v, "aviso": REINICIO_DE[k]} for k, v in SERVICIOS.items()],
        "reinicio": {
            "aviso_s": int(textos.REBOOT_NOTICE_DELAY.total_seconds()),
            "texto": aviso,
            "chats": None if chats is None else len(chats),
            "pendiente_s": _reboot_left(),
            "ultimo": _reboot_last(),
        },
    }


# --- one knob ---------------------------------------------------------------------------

@router.post("/api/admin/machine/knob")
async def set_machine_knob(body: MachineKnobRequest, session: Session = Depends(require_machine)) -> dict[str, Any]:
    """Step 1 of a second-floor knob: validated by the registry, the keyed models and the machine block's assert
    BEFORE the confirm; step 2 writes it with its bitácora row (knob.set) as one act."""
    knob = knobs.KNOBS.get(body.knob)
    if knob is None or knob.floor != FLOOR_MAQUINAS:
        raise HTTPException(status_code=400, detail=f"«{body.knob}» no es una perilla del cuarto de máquinas.")
    if knob.name == "machine.workers":
        raise HTTPException(status_code=400, detail="Los analistas se cambian con su botón Aplicar: el número y el "
                            "mayordomo van juntos.")
    try:
        value = knobs.coerce(knob.name, body.value)
    except knobs.KnobError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    cfg = await asyncio.to_thread(load_config)
    if knob.kind == "model":
        keyed = _keyed_models()
        if value not in keyed:
            known = value in {c.model for c in CONTENDERS}
            why = "no tiene llave en este servidor" if known else "no es un modelo de la lista"
            raise HTTPException(
                status_code=400,
                detail=f"active_model: «{value}» {why}: elige uno de la lista ({', '.join(keyed) or 'ninguno'}).",
            )
    _bounce(cfg, knob, value)
    before = knobs.global_value(cfg, knob.name)
    if before == value:
        return {"cambio": False, "knob": knob.name, "valor": value}

    async def run() -> dict[str, Any]:
        def decide(now: Config) -> tuple[list[cw.Op], str, dict[str, Any], dict[str, Any]]:
            antes = knobs.global_value(now, knob.name)
            answer = {"knob": knob.name, "antes": antes, "despues": value}
            if antes == value:
                return [], "", {}, answer
            detail = {"knob": knob.name, "site": None, "antes": antes, "despues": value}
            return [cw.set_op(knob.path, value)], "knob.set", detail, answer

        answer = await hub.config_action(session, decide)
        if answer["cambio"]:
            log.info("machine knob set %s user=%s", knob.name, session.user)
        return answer

    return propose(
        session,
        action="cambiar " + knob.name,
        diff={
            "resumen": f"{knob.name} cambia para toda la cadena. {knob.que_es} {DESDE.get(knob.name, DESDE_DEFAULT)}",
            "cambios": [{"que": knob.name, "antes": before, "despues": value}],
        },
        run=run,
        floor=FLOOR_MAQUINAS,
    )


# --- the analysts -----------------------------------------------------------------------

async def _set_workers(value: int, only_if: int | None = None) -> int:
    """machine.workers through the one writer, holding its lock for the write only (never across a butler call).
    Returns the value before. only_if: write only while the file still holds that value, so an undo never
    overwrites a change someone made meanwhile."""
    def work() -> int:
        before = load_config().machine.workers
        if before != value and (only_if is None or before == only_if):
            cw.apply_ops([cw.set_op(("machine", "workers"), value)])
        return before

    async with cw.LOCK:
        try:
            return await asyncio.to_thread(work)
        except cw.WriterError as exc:
            raise ButlerNote(400, str(exc)) from None


async def _scale(session: Session, count: int) -> dict[str, Any]:
    """The desire (machine.workers) and the butler's scale, together. A refusal or an absent mailbox puts the number
    back; a line the butler took without a readable answer keeps the new desire (the next boot's reconvergence asks
    for it again) and says so."""
    async with _SCALE_LOCK:
        if not await asyncio.to_thread(butler_probe):
            raise ButlerNote(503, "El mayordomo no responde: nada cambió.")
        corriendo = await census_now()
        before = await _set_workers(count)
        verdict, info = await _butler(_scale_line(count))
        if verdict in ("ausente", "rechazado"):
            await _set_workers(before, only_if=count)
            raise _butler_note(verdict, info)
        mayordomo = info if verdict == "ok" else "sin respuesta"
        await _row(session.user, "workers.scale",
                   {"deseados": count, "antes": before, "corriendo": corriendo, "mayordomo": mayordomo}, session.uid)
    if verdict != "ok":
        raise ButlerNote(
            504,
            f"El mayordomo tomó la orden pero {info}. El número deseado quedó guardado ({count}): mira «Corriendo» en "
            "un minuto; si no cambia, el próximo arranque se lo pide otra vez.",
        )
    log.info("workers scaled to %d user=%s", count, session.user)
    return {"deseados": count, "antes": before, "corriendo": corriendo, "mayordomo": info}


def _refuse_without_butler() -> None:
    if not butler_probe():
        raise HTTPException(status_code=503, detail="El mayordomo no responde: nada cambió.")


@router.post("/api/admin/machine/workers")
async def set_workers(body: WorkersRequest, session: Session = Depends(require_machine)) -> dict[str, Any]:
    """Step 1 of the apply button: the diff is the desire against the count running now. Without the butler it is
    refused here already (and again at step 2)."""
    count = body.count
    cfg = await asyncio.to_thread(load_config)
    deseados = cfg.machine.workers
    corriendo = await census_now()
    if count == deseados and corriendo == count:
        return {"cambio": False, "deseados": count, "corriendo": corriendo}
    await asyncio.to_thread(_refuse_without_butler)

    async def run() -> dict[str, Any]:
        try:
            return await _scale(session, count)
        except ButlerNote as note:
            raise HTTPException(status_code=note.status, detail=note.detail) from None

    plural = "analista" if count == 1 else "analistas"
    return propose(
        session,
        action=f"poner {count} {plural}",
        diff={
            "resumen": (
                f"Quedan {count} {plural} para toda la cadena: el número se guarda (se recuerda tras cada reinicio) "
                "y el mayordomo lo aplica ahora. Cada analista toma platos de la misma cola."
            ),
            "cambios": [
                {"que": "analistas deseados", "antes": deseados, "despues": count},
                {"que": "analistas corriendo", "antes": "?" if corriendo is None else corriendo, "despues": count},
            ],
        },
        run=run,
        floor=FLOOR_MAQUINAS,
    )


# --- restart one service ----------------------------------------------------------------

@router.post("/api/admin/machine/restart")
async def restart_service(body: RestartRequest, session: Session = Depends(require_machine)) -> dict[str, Any]:
    """Step 1: the diff names the service and what its restart does; postgres has no button. Refused while a reboot
    of the whole server is on its way (restarting the ventanilla would kill it after every chat was warned)."""
    servicio = body.service
    busy = _reboot_busy()
    if busy:
        raise HTTPException(status_code=409, detail=busy)
    await asyncio.to_thread(_refuse_without_butler)

    async def run() -> dict[str, Any]:
        busy = _reboot_busy()
        if busy:
            raise HTTPException(status_code=409, detail=busy)
        await asyncio.to_thread(_refuse_without_butler)
        _RESTARTING[servicio] = time.monotonic()
        verdict, info = "ausente", "la orden no salió"
        try:
            verdict, info = await _butler("restart " + servicio)
        finally:
            if servicio != "fastapi" or verdict in ("ausente", "rechazado"):
                _RESTARTING.pop(servicio, None)   # a ventanilla that took its own restart keeps the mark until it ends
        if verdict in ("ausente", "rechazado"):
            note = _butler_note(verdict, info)
            raise HTTPException(status_code=note.status, detail=note.detail)
        await _row(session.user, "service.restart",
                   {"servicio": servicio, "mayordomo": info if verdict == "ok" else "sin respuesta"}, session.uid)
        if verdict != "ok":
            note = _butler_note(verdict, info)
            raise HTTPException(status_code=note.status, detail=note.detail)
        log.info("service restart %s taken by the butler user=%s", servicio, session.user)
        return {"servicio": servicio, "mayordomo": info}

    return propose(
        session,
        action="reiniciar " + SERVICIOS[servicio],
        diff={"resumen": REINICIO_DE[servicio], "lista": ["Solo este servicio; los demás siguen trabajando."]},
        run=run,
        floor=FLOOR_MAQUINAS,
    )


# --- the reboot -------------------------------------------------------------------------

def _ended(estado: str, motivo: str = "", avisados: int | None = None, de: int | None = None) -> None:
    _REBOOT["last"] = {"estado": estado, "motivo": motivo, "ts": time.time(), "avisados": avisados, "de": de}


async def _reboot_later(usuario: str, uid: int | None, delay: int, warned: list[str]) -> None:
    """After the notice's minute: the butler's reboot. Taken = a reboot row (the api dies soon after). Refused or
    absent = the chats that were warned hear at once that it did not happen (before any row: that message needs no
    database), then a reboot.fallido row. Taken without a readable answer = the reboot may be under way: a reboot
    row ("sin respuesta"), and only if this process still lives after REBOOT_GRACE_S do the chats hear it did not
    happen."""
    try:
        await asyncio.sleep(delay)
        verdict, info = await _butler("reboot")
        if verdict == "ok":
            _ended("tomado")
            await _row(usuario, "reboot", {"mayordomo": info}, uid)
            log.info("reboot taken by the butler")
            return
        if verdict == "sin_respuesta":
            _ended("sin_respuesta", info)
            await _row(usuario, "reboot", {"mayordomo": "sin respuesta"}, uid)
            log.warning("reboot line taken by the butler without a readable answer: waiting %d s", REBOOT_GRACE_S)
            await asyncio.sleep(REBOOT_GRACE_S)
            motivo = (f"el mayordomo tomó la orden pero {info}, y el servidor no se reinició en "
                      f"{REBOOT_GRACE_S // 60} min")
        else:
            motivo = ("el mayordomo no lo aceptó: " + info) if verdict == "rechazado" else "el mayordomo no responde"
        told = await _send_every(warned, _text("aviso_reinicio_cancelado", {}))
        _ended("fallido", motivo, len(told), len(warned))
        await _row(usuario, "reboot.fallido", {"motivo": motivo, "avisados": len(told)}, uid)
        log.warning("reboot did not happen (%s): %d of %d warned chat(s) told", verdict, len(told), len(warned))
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("the reboot task failed")
    finally:
        _REBOOT["deadline"] = None
        _REBOOT["task"] = None


NO_DB_CHATS = "La base de datos no responde: sin ella no sé a qué chats avisar. No se avisó a nadie y nada cambió."


async def _reboot_refusal(cfg: Config) -> HTTPException | None:
    """Why a reboot cannot be asked now, checked BEFORE anyone is warned (None: it can)."""
    busy = _reboot_busy()
    if busy:
        return HTTPException(status_code=409, detail=busy)
    restarting = sorted(s for s, at in _RESTARTING.items() if time.monotonic() - at < RESTART_MARK_S)
    if restarting:
        return HTTPException(status_code=409, detail=f"Se está reiniciando {', '.join(restarting)}: espera un momento. "
                             "No se avisó a nadie.")
    try:
        last = await asyncio.to_thread(_last_reboot_at)
    except psycopg.Error:
        return HTTPException(status_code=503, detail=NO_DB_CHATS)
    if last is not None and datetime.now(timezone.utc) < last + timedelta(minutes=BUTLER_WINDOW_MIN):
        hora = (last + timedelta(minutes=BUTLER_WINDOW_MIN)).astimezone(ZoneInfo(cfg.timezone)).strftime("%H:%M")
        return HTTPException(status_code=409, detail=(
            f"El mayordomo toma un reinicio cada {BUTLER_WINDOW_MIN} min: el siguiente, después de las {hora}. "
            "No se avisó a nadie."
        ))
    if not await asyncio.to_thread(butler_probe):
        return HTTPException(status_code=503, detail="El mayordomo no responde: no se avisó a nadie y nada cambió.")
    return None


async def _reboot_now(session: Session, delay: int) -> dict[str, Any]:
    async with _REBOOT_LOCK:
        cfg = await asyncio.to_thread(load_config)
        refusal = await _reboot_refusal(cfg)
        if refusal:
            raise refusal
        _REBOOT["announcing"] = True              # from here a restart is refused too (it would kill this reboot)
        try:
            try:
                chats = await asyncio.to_thread(_every_chat)
            except psycopg.Error:
                raise HTTPException(status_code=503, detail=NO_DB_CHATS) from None
            warned = await _send_every(chats, _text("aviso_reinicio", {"minutos": _notice_minutes()}))
            # From the first warned chat on, the reboot goes on: nothing below may stop it or leave it half done.
            _REBOOT["deadline"] = time.monotonic() + delay
            _REBOOT["last"] = None
            _REBOOT["task"] = asyncio.create_task(_reboot_later(session.user, session.uid, delay, warned))
        finally:
            _REBOOT["announcing"] = False
        await _row(session.user, "reboot.aviso", {"chats": len(chats), "enviados": len(warned), "segundos": delay},
                   session.uid)
    log.info("reboot announced to %d of %d chat(s), the butler in %d s user=%s", len(warned), len(chats), delay,
             session.user)
    return {"chats": len(chats), "enviados": len(warned), "reinicio_en_s": delay}


@router.post("/api/admin/machine/reboot")
async def reboot(session: Session = Depends(require_machine)) -> dict[str, Any]:
    """Step 1: the diff shows the notice every chat will read, and when the butler reboots. What would refuse it at
    step 2 (one already on its way, the butler's 10 minutes, no butler, no database) refuses it here already."""
    delay = int(textos.REBOOT_NOTICE_DELAY.total_seconds())
    cfg = await asyncio.to_thread(load_config)
    refusal = await _reboot_refusal(cfg)
    if refusal:
        raise refusal
    try:
        chats = await asyncio.to_thread(_every_chat)
    except psycopg.Error:
        raise HTTPException(status_code=503, detail=NO_DB_CHATS) from None
    text = await asyncio.to_thread(_text, "aviso_reinicio", {"minutos": _notice_minutes()})

    async def run() -> dict[str, Any]:
        return await _reboot_now(session, delay)

    return propose(
        session,
        action="reiniciar el servidor",
        diff={
            "resumen": (
                "El servidor COMPLETO se reinicia: la ventanilla, los analistas, el bot y la base de datos se apagan "
                "y vuelven solos en unos minutos. Mientras tanto nada se guarda: un plato que llegue a una cámara en "
                "esos minutos se pierde. Mejor fuera del horario de servicio."
            ),
            "lista": [
                f"Ahora: este aviso a {len(chats)} chat(s): {text}",
                f"En {delay} s: el mayordomo reinicia el servidor.",
                "Al volver: entra otra vez, abre el cuarto y revisa la lista de arranque.",
            ],
        },
        run=run,
        floor=FLOOR_MAQUINAS,
    )


# --- the cuarto password ----------------------------------------------------------------

def _set_cuarto_hash(session: Session, digest: str) -> bool:
    """The new hash and its bitácora row in ONE transaction; False when there is no cuarto to change."""
    with q.connect() as conn, conn.transaction():
        row = conn.execute(
            q.SET_MACHINE_PASSWORD, {"usuario": MACHINE_USER, "password_hash": digest, "changed_by": session.user},
        ).fetchone()
        if row is None:
            return False
        conn.execute(q.INSERT_ADMIN_LOG_BY, {
            "usuario": session.user, "usuario_uid": session.uid, "action": "machine.password", "detail": Jsonb({}),
        })
    return True


@router.post("/api/admin/machine/password")
async def cuarto_password(body: PasswordRequest, session: Session = Depends(require_machine)) -> dict[str, Any]:
    """Step 1 of a new cuarto password (you are already inside the wall). It is hashed now: the pending confirm
    keeps the hash, never the password."""
    digest = await asyncio.to_thread(make_hash, body.password)

    async def run() -> dict[str, Any]:
        if not await asyncio.to_thread(_set_cuarto_hash, session, digest):
            raise HTTPException(status_code=404, detail="La contraseña del cuarto no existe todavía: créala primero.")
        closed = revoke_machine_others(session)
        log.info("cuarto password changed user=%s other grants closed=%d", session.user, closed)
        return {"cuarto": "cambiada", "otras_sesiones": closed}

    return propose(
        session,
        action="cambiar la contraseña del cuarto",
        diff={
            "resumen": "La contraseña del cuarto de máquinas cambia.",
            "lista": [
                "Desde ahora el cuarto se abre solo con la nueva.",
                "Quien tenga el cuarto abierto en otra sesión lo pierde; tu sesión lo conserva.",
            ],
        },
        run=run,
        floor=FLOOR_MAQUINAS,
    )


# --- the post-reboot checklist ----------------------------------------------------------

def _db_status() -> dict[str, Any]:
    """The queue (the mostrador's own counts: a failed plate in the papelera is not counted), the last finished
    plate, and the active sites."""
    try:
        with q.connect() as conn:
            cola = conn.execute(q.COLA_COUNTS, {"site": None}).fetchone()
            last_done = conn.execute(q.BOT_LAST_DONE_AT).fetchone()["at"]
            active = {r["site"] for r in conn.execute(q.SELECT_SITES_ALL).fetchall() if r["active"]}
    except psycopg.Error as exc:
        log.warning("status: database unreachable (%s)", type(exc).__name__)
        return {"ok": False}
    counts = {k: int(cola[k] or 0) for k in ("esperando", "en_proceso", "fallidos")}
    return {"ok": True, "cola": counts, "last_done": last_done, "active": active}


def _item(clave: str, titulo: str, ok: bool | None, texto: str, detalle: list[str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"clave": clave, "titulo": titulo, "ok": ok, "texto": texto}
    if detalle:
        out["detalle"] = detalle
    return out


def _migrations_text(report: dict[str, Any]) -> str:
    steps = report.get("migrations") or {}
    added = [name for name, verdict in steps.items() if verdict == "added"]
    text = "esquema " + ("creado en este arranque" if report.get("schema") == "created" else "presente")
    text += f"; {len(steps)} pasos de migración"
    text += (": agregados en este arranque: " + ", ".join(added)) if added else ", todos ya presentes"
    if report.get("users_bootstrapped"):
        text += f"; cuentas importadas: {len(report['users_bootstrapped'])}"
    if report.get("device_keys_born"):
        text += f"; gafetes nacidos: {report['device_keys_born']}"
    return text


@router.get("/api/admin/status")
async def status(request: Request, session: Session = Depends(require_machine)) -> dict[str, Any]:
    """The green boot checklist (SPEC 1.6), re-read by the page every few seconds."""
    state = request.app.state
    now = datetime.now(timezone.utc)
    cfg = await asyncio.to_thread(load_config)
    db, corriendo, responde = await asyncio.gather(
        asyncio.to_thread(_db_status), census_now(), asyncio.to_thread(butler_probe),
    )
    items: list[dict[str, Any]] = []

    boot_at = getattr(state, "boot_at", None)
    if boot_at is None:
        items.append(_item("ventanilla", "Ventanilla (api)", None, "sin hora de arranque"))
    else:
        items.append(_item("ventanilla", "Ventanilla (api)", True,
                           f"arrancó {_ago((now - boot_at).total_seconds())} ({_local(boot_at, cfg)})"))

    items.append(_item("base", "Base de datos", db["ok"], "responde" if db["ok"] else "no responde"))

    report = getattr(state, "init_report", None) or {}
    items.append(_item("migraciones", "Migraciones", True if report else None,
                       _migrations_text(report) if report else "sin informe de este arranque"))

    if db["ok"]:
        c = db["cola"]
        cola = f"{c['esperando']} esperando · {c['en_proceso']} en proceso · {c['fallidos']} fallidos"
        atorada = c["esperando"] > cfg.alerts.backlog_threshold      # the bot's own rule (cola_atorada)
        if cfg.queue_paused:
            cola += " · EN PAUSA (mostrador)"
        elif atorada:
            cola += f" · atorada (umbral {cfg.alerts.backlog_threshold})"
        items.append(_item("cola", "Cola", None if (cfg.queue_paused or atorada) else True, cola))
    else:
        items.append(_item("cola", "Cola", False, "sin base de datos no se puede leer"))

    deseados = cfg.machine.workers
    analistas = f"corriendo {'?' if corriendo is None else corriendo} de {deseados} deseado(s)"
    if db["ok"]:
        last_done = db["last_done"]
        analistas += ("; último plato terminado " + _ago((now - last_done).total_seconds())
                      if last_done else "; ningún plato terminado todavía")
    items.append(_item("analistas", "Analistas (worker)", None if corriendo is None else corriendo == deseados,
                       analistas))

    ages = lane.frame_ages()
    fresh_s = 3 * cfg.capture.frame_poll_s
    lines: list[str] = []
    judged = 0
    all_fresh = True
    for site, site_cfg in sorted(cfg.sites.items()):
        if db["ok"] and site not in db["active"]:
            continue                               # a deactivated site sends nothing, by design
        if site_cfg.mantenimiento:                 # the bot does not judge it either
            if site_cfg.cameras:
                lines.append(f"{site}: en mantenimiento (sus cámaras no se revisan)")
            continue
        for name in sorted(site_cfg.cameras):
            judged += 1
            age = ages.get((site, name))
            if age is None:
                lines.append(f"{site}/{name}: sin copia desde que arrancó la ventanilla")
                all_fresh = False
            else:
                lines.append(f"{site}/{name}: última copia {_ago(age)}")
                all_fresh = all_fresh and age <= fresh_s
    if not judged:
        items.append(_item("camaras", "Cámaras", None, "ninguna cámara que revisar", lines))
    else:
        items.append(_item("camaras", "Cámaras", True if all_fresh else None,
                           "todas mandan imagen" if all_fresh else "alguna no manda imagen ahora", lines))

    seen = (getattr(state, "last_seen", None) or {}).get("bot")
    window = max(BOT_SEEN_MIN_S, 3 * cfg.alerts.poll_s)
    if seen is None:
        items.append(_item("bot", "Bot", False, "sin señales desde que arrancó la ventanilla"))
    else:
        age = (now - seen).total_seconds()
        items.append(_item("bot", "Bot", age <= window, "visto " + _ago(age)))

    items.append(_item("mayordomo", "Mayordomo", responde, "responde" if responde else "no responde"))
    return {
        "ahora": now.isoformat(),
        "items": items,
        "analistas": {"deseados": deseados, "corriendo": corriendo},
        "mayordomo": {"responde": responde},
        "reinicio_pendiente_s": _reboot_left(),
        "reinicio_ultimo": _reboot_last(),
    }


# --- the log viewer (SSE's one home) ----------------------------------------------------

def _log_files() -> list[dict[str, Any]]:
    try:
        entries = list(LOG_DIR.iterdir())
    except OSError:
        return []
    files = []
    for path in entries:
        if not LOG_NAME_RE.match(path.name):
            continue
        try:
            st = path.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):           # a link or anything else is never followed out of LOG_DIR
            continue
        files.append({"archivo": path.name, "bytes": st.st_size, "mtime": st.st_mtime})
    rank = {"fastapi.log": 0, "bot.log": 2}
    files.sort(key=lambda f: (rank.get(f["archivo"], 1), -f["mtime"], f["archivo"]))
    return [
        {"archivo": f["archivo"], "bytes": f["bytes"],
         "modificado": datetime.fromtimestamp(f["mtime"], timezone.utc).isoformat()}
        for f in files
    ]


@router.get("/api/admin/logs")
async def log_files(session: Session = Depends(require_machine)) -> dict[str, Any]:
    return {"archivos": await asyncio.to_thread(_log_files), "lineas": LOG_TAIL_LINES}


def _sse(data: str, event: str | None = None) -> str:
    return (f"event: {event}\n" if event else "") + "data: " + data + "\n\n"


def _clean(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace").replace("\r", "")
    return text if len(text) <= LOG_LINE_MAX else text[:LOG_LINE_MAX] + " …"


def _open_log(path: Path) -> Any:
    """Open a log file for reading: never through a symbolic link (O_NOFOLLOW), never blocking on a pipe
    (O_NONBLOCK), and only a plain file (OSError otherwise). The viewer reads LOG_DIR only."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError(f"not a plain file: {path.name}")
    return os.fdopen(fd, "rb")


def _open_tail(path: Path) -> tuple[int, int, list[str]]:
    """The file's inode, the offset of the first byte not sent yet, and its last LOG_TAIL_LINES complete lines
    (a line still being written is left for the next read)."""
    with _open_log(path) as fh:
        st = os.fstat(fh.fileno())
        start = max(0, st.st_size - LOG_TAIL_BYTES)
        fh.seek(start)
        data = fh.read(st.st_size - start)
    pieces = data.split(b"\n")
    pending = pieces.pop()
    if start > 0 and pieces:
        pieces = pieces[1:]                        # the first piece may begin mid-line
    return st.st_ino, start + len(data) - len(pending), [_clean(p) for p in pieces[-LOG_TAIL_LINES:]]


def _read_more(path: Path, ino: int, offset: int) -> tuple[str, int, int, list[str]]:
    """What arrived since `offset`: ("ok" | "rotated" | "skipped" | "gone", inode, new offset, complete lines). A
    new inode or a shorter file means the handler rotated it: the read starts over at its beginning."""
    try:
        fh = _open_log(path)
    except OSError:                                # missing (mid-rotation), or no longer a plain file
        return "gone", ino, offset, []
    with fh:
        st = os.fstat(fh.fileno())
        note = "ok"
        if st.st_ino != ino or st.st_size < offset:
            note, ino, offset = "rotated", st.st_ino, 0
        cut = False
        if st.st_size - offset > LOG_READ_MAX:
            offset, cut = st.st_size - LOG_READ_MAX, True
            note = "skipped" if note == "ok" else note
        fh.seek(offset)
        data = fh.read(st.st_size - offset)
    pieces = data.split(b"\n")
    pending = pieces.pop()
    if cut and pieces:
        pieces = pieces[1:]                        # a cut read begins mid-line
    return note, ino, offset + len(data) - len(pending), [_clean(p) for p in pieces]


async def _tail(request: Request, session: Session, path: Path) -> AsyncIterator[str]:
    try:
        ino, offset, lines = await asyncio.to_thread(_open_tail, path)
    except OSError:
        yield _sse("no se pudo abrir el registro", "cerrado")
        return
    yield _sse(f"{path.name}: sus últimas {len(lines)} líneas, y lo nuevo en vivo", "aviso")
    for line in lines:
        yield _sse(line)
    quiet = missing = 0.0
    while True:
        await asyncio.sleep(LOG_POLL_S)
        if await request.is_disconnected():
            return
        if machine_grant_left(session) <= 0:
            yield _sse("el cuarto se cerró: ábrelo otra vez para seguir viendo el registro", "cerrado")
            return
        note, ino, offset, fresh = await asyncio.to_thread(_read_more, path, ino, offset)
        if note == "gone":                         # mid-rotation the name is missing for an instant: wait it out
            missing += LOG_POLL_S
            if missing >= LOG_GONE_S:
                yield _sse("el archivo ya no existe", "cerrado")
                return
            continue
        missing = 0.0
        if note == "rotated":
            yield _sse("el archivo rotó: sigue desde su principio", "aviso")
        elif note == "skipped":
            yield _sse("llegaron demasiadas líneas de golpe: se saltaron algunas", "aviso")
        for line in fresh:
            yield _sse(line)
        if fresh or note != "ok":
            quiet = 0.0
            continue
        quiet += LOG_POLL_S
        if quiet >= LOG_KEEPALIVE_S:
            quiet = 0.0
            yield ": sigo aquí\n\n"


@router.get("/api/admin/logs/stream")
async def log_stream(request: Request, archivo: str = "", session: Session = Depends(require_machine)) -> Any:
    """The live viewer: a window, never the archive. Only the services' own log files, by their exact name."""
    if not LOG_NAME_RE.match(archivo):
        raise HTTPException(status_code=400, detail="registro desconocido: elige uno de la lista")
    path = LOG_DIR / archivo
    if path.is_symlink() or not path.is_file():
        raise HTTPException(status_code=404, detail=f"{archivo} no existe (todavía)")
    log.info("log viewer opened %s user=%s", archivo, session.user)
    return StreamingResponse(
        _tail(request, session, path),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


# --- the boot reconvergence (Fer's law: the number you had is the number you keep) --------

async def reconverge_once() -> dict[str, Any]:
    """Count until two counts in a row agree (never judge a moving world, never a "?"), then compare with
    machine.workers and call the butler ONCE when they differ. The system acts: usuario NULL in its row."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + RECONVERGE_WINDOW_S
    previous: int | None = None
    while True:
        corriendo = await census_now()
        if corriendo is not None and corriendo == previous:
            break
        previous = corriendo
        if loop.time() >= deadline:
            log.info("reconvergence: the count stayed unknown or moving (%s): no judgment", corriendo)
            return {"juicio": None, "corriendo": corriendo}
        await asyncio.sleep(RECONVERGE_RETRY_S)
    async with _SCALE_LOCK:                       # an Aplicar confirmed meanwhile is never undone by an older desire
        if await census_now() != corriendo:       # the count moved while the lock was held (an Aplicar): no judgment
            log.info("reconvergence: the count moved under an Aplicar: no judgment")
            return {"juicio": None, "corriendo": corriendo}
        deseados = (await asyncio.to_thread(load_config)).machine.workers
        if corriendo == deseados:
            log.info("reconvergence: %d analyst(s) running, as wanted", corriendo)
            return {"juicio": "igual", "corriendo": corriendo, "deseados": deseados}
        if await asyncio.to_thread(butler_probe):
            verdict, info = await _butler(_scale_line(deseados))
        else:
            verdict, info = "ausente", "no responde"
        if verdict == "ok":
            await _row(None, "workers.reconverge", {"deseados": deseados, "corriendo": corriendo, "mayordomo": info})
            log.info("reconvergence: %d running, %d wanted: the butler took the scale", corriendo, deseados)
            return {"juicio": "ajustado", "corriendo": corriendo, "deseados": deseados}
        await _row(None, "workers.reconverge_fallida",
                   {"deseados": deseados, "corriendo": corriendo, "motivo": verdict + ": " + info})
    log.warning("reconvergence: %d running, %d wanted, the butler did not take it (%s)", corriendo, deseados, verdict)
    return {"juicio": "fallido", "corriendo": corriendo, "deseados": deseados}


async def _reconverge_at_boot() -> None:
    try:
        await asyncio.sleep(RECONVERGE_SETTLE_S)
        await reconverge_once()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("reconvergence failed; the next boot tries again")


async def startup(app: FastAPI) -> None:
    """The hub runs this once the gate is up: the reconvergence waits in the background, never in the boot."""
    app.state.reconverge = asyncio.create_task(_reconverge_at_boot())
