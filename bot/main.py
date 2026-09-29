"""The real Telegram bot (Fase 2, T-D1/T-D2): @LPQ_vision_bot on python-telegram-bot 22.8.

Commands: /id (the chat's numeric id: the enrollment tool), /cola (queue depth + last processed
plate), /foto (what the camera sees RIGHT NOW: the ephemeral copy fetched from the api over the
internal docker network and sent as a photo with its sharpness).

One READ-ONLY round every alerts.poll_s: the six alert families (dirty lens, backlog, failures +
silent worker, ventanilla not answering, forensic river reminder, birth greeting) and the 🟡/🟢
alignment notices discovered by polling GET /api/camera/state. Every rule is a pure function of
the round's inputs and the bot's RAM state, so the logic is testable without Telegram.

Destination: sites.telegram_chat_id FIRST when filled (FASE 3's mostrador writes it), else the
TEMPORAL .env key TELEGRAM_CHAT_ID (pasted once after /id; it dies in FASE 3). No destination =
commands still work, alerts are logged as undeliverable. The bot never writes a business row.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from brain.db import queries as q
from brain.validator.models import Config, load_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("lpq.bot")

# --- named constants (SPEC section 4) -------------------------------------------------
API_BASE = "http://fastapi:8000"          # the compose SERVICE name; internal network only
HTTP_TIMEOUT_S = 5
BIND_REMINDER_COOLDOWN = timedelta(minutes=15)
RAW_LOG_REMINDER_HOURS = 48
RAW_LOG_AUTO_OFF_HOURS = 24               # named now, wired in FASE 3 (config writes belong to the mostrador)
REBOOT_NOTICE_DELAY = timedelta(seconds=60)  # the pre-reboot helper's default; consumed by FASE 3's butler
LAST_ERROR_MAX_CHARS = 300
ENV_TOKEN = "TELEGRAM_BOT_TOKEN"
ENV_CHAT_ID = "TELEGRAM_CHAT_ID"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- the round's inputs ------------------------------------------------------------------

@dataclass
class ApiState:
    ok: bool
    reason: str = ""
    align: dict[tuple[str, str], bool] = field(default_factory=dict)   # (site, camera) -> active


@dataclass
class DbState:
    pending: int = 0
    working: int = 0
    last_done_at: datetime | None = None
    new_failed: list[dict[str, Any]] = field(default_factory=list)
    sharpness: dict[tuple[str, str], list[float]] = field(default_factory=dict)  # newest first
    max_failed_id: int = 0


# --- the bot's RAM (disposable by construction; a restart restarts every clock) --------------

@dataclass
class BotState:
    failed_after_id: int = 0
    backlog_active: bool = False
    silent_active: bool = False
    api_down_since: datetime | None = None
    api_last_reminder: datetime | None = None
    lens_last_alert: dict[tuple[str, str], datetime] = field(default_factory=dict)
    raw_log_first_seen: datetime | None = None
    raw_log_last_reminder: datetime | None = None
    align_active: dict[tuple[str, str], bool] = field(default_factory=dict)
    align_seen_once: bool = False


# --- the rules: pure functions returning the messages to send ---------------------------------

def rule_backlog(state: BotState, db: DbState, cfg: Config) -> list[str]:
    out: list[str] = []
    if db.pending > cfg.alerts.backlog_threshold and not state.backlog_active:
        state.backlog_active = True
        out.append(f"📈 Cola atorada: {db.pending} platos esperando (umbral {cfg.alerts.backlog_threshold}). El analista no da abasto.")
    elif db.pending <= cfg.alerts.backlog_threshold and state.backlog_active:
        state.backlog_active = False
        out.append(f"✅ La cola volvió a la normalidad: {db.pending} pendientes.")
    return out


def rule_failures(state: BotState, db: DbState) -> list[str]:
    out: list[str] = []
    for job in db.new_failed:
        err = (job["last_error"] or "sin detalle")[:LAST_ERROR_MAX_CHARS]
        out.append(f"❌ El análisis del plato #{job['plate_id']} falló definitivamente tras {job['attempts']} intentos (job {job['id']}): {err}")
        state.failed_after_id = max(state.failed_after_id, int(job["id"]))
    return out


def rule_silent_worker(state: BotState, db: DbState, cfg: Config, now: datetime) -> list[str]:
    out: list[str] = []
    silence = timedelta(minutes=cfg.alerts.worker_silence_min)
    quiet = db.last_done_at is None or (now - db.last_done_at) > silence
    if db.pending > 0 and quiet and not state.silent_active:
        state.silent_active = True
        out.append(f"😶 El analista lleva más de {cfg.alerts.worker_silence_min} min sin terminar nada y hay {db.pending} platos esperando. ¿Está vivo el worker?")
    elif state.silent_active and not quiet:
        state.silent_active = False
        out.append("✅ El analista volvió a terminar platos.")
    return out


def rule_api(state: BotState, api: ApiState, now: datetime) -> list[str]:
    out: list[str] = []
    if not api.ok:
        if state.api_down_since is None:
            state.api_down_since = now
            state.api_last_reminder = now
            out.append(f"🔴 La ventanilla no responde ({api.reason}). Nadie puede subir fotos hasta que vuelva.")
        elif now - (state.api_last_reminder or now) >= BIND_REMINDER_COOLDOWN:
            state.api_last_reminder = now
            mins = int((now - state.api_down_since).total_seconds() // 60)
            out.append(f"🔴 La ventanilla sigue sin responder desde hace {mins} min ({api.reason}).")
    elif state.api_down_since is not None:
        mins = int((now - state.api_down_since).total_seconds() // 60)
        state.api_down_since = None
        state.api_last_reminder = None
        out.append(f"🟢 La ventanilla volvió después de {mins} min.")
    return out


def rule_lens(state: BotState, db: DbState, cfg: Config, now: datetime) -> list[str]:
    out: list[str] = []
    n, floor = cfg.alerts.sharpness_n, cfg.alerts.sharpness_min
    cooldown = timedelta(minutes=cfg.alerts.sharpness_cooldown_min)
    for key, scores in db.sharpness.items():
        if len(scores) < n or any(s >= floor for s in scores[:n]):
            continue
        last = state.lens_last_alert.get(key)
        if last is not None and now - last < cooldown:
            continue
        state.lens_last_alert[key] = now
        shown = ", ".join(f"{s:.0f}" for s in scores[:n])
        out.append(f"🧽 Lente sucio en {key[0]}/{key[1]}: las últimas {n} fotos salieron borrosas (nitidez {shown}, piso {floor:.0f}). Limpien el vidrio de la cámara.")
    return out


def rule_raw_log(state: BotState, cfg: Config, now: datetime) -> list[str]:
    """The "since when" clock is the bot's own first sighting, in RAM (config mtime is banned: the
    calibration write touches that file). A restart restarts the clock: late, never lost."""
    if not cfg.raw_logging:
        state.raw_log_first_seen = None
        state.raw_log_last_reminder = None
        return []
    if state.raw_log_first_seen is None:
        state.raw_log_first_seen = now
        return []
    reminder = timedelta(hours=RAW_LOG_REMINDER_HOURS)
    since = state.raw_log_last_reminder or state.raw_log_first_seen
    if now - since >= reminder:
        state.raw_log_last_reminder = now
        hours = int((now - state.raw_log_first_seen).total_seconds() // 3600)
        return [f"📝 El registro forense (raw_logging) lleva {hours} h PRENDIDO, ¿a propósito? Llena disco (tope 200 MB). Apágalo en config.yaml cuando termines."]
    return []


def rule_align(state: BotState, api: ApiState) -> list[str]:
    """🟡 when a camera's capture pauses, 🟢 when it wakes. The first round only records."""
    if not api.ok:
        return []
    out: list[str] = []
    if state.align_seen_once:
        for key, active in api.align.items():
            was = state.align_active.get(key, False)
            if active and not was:
                out.append(f"🟡 Modo alineación en {key[0]}/{key[1]}: la captura de esa cámara está en pausa.")
            elif was and not active:
                out.append(f"🟢 Alineación terminada en {key[0]}/{key[1]}: la captura despertó.")
    state.align_active = dict(api.align)
    state.align_seen_once = True
    return out


# --- gathering the inputs (threads: psycopg and urllib are sync) -------------------------------

def _http_get(path: str) -> tuple[int, bytes, dict[str, str]]:
    req = urllib.request.Request(API_BASE + path, headers={"User-Agent": "lpq-bot"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            return resp.status, resp.read(), {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), {}


def read_api(cfg: Config) -> ApiState:
    try:
        status, body, _ = _http_get("/api/health")
    except Exception as exc:
        return ApiState(ok=False, reason=f"sin conexión: {type(exc).__name__}")
    if status != 200:
        return ApiState(ok=False, reason=f"health {status}")
    align: dict[tuple[str, str], bool] = {}
    for site in cfg.sites:
        try:
            st_status, st_body, _ = _http_get(f"/api/camera/state?site={site}")
            if st_status == 200:
                for cam, entry in json.loads(st_body)["cameras"].items():
                    align[(site, cam)] = bool(entry.get("align", {}).get("active"))
        except Exception as exc:
            log.warning("state read failed for %s: %s", site, type(exc).__name__)
    return ApiState(ok=True, align=align)


def read_db(cfg: Config, failed_after_id: int) -> DbState:
    db = DbState()
    with q.connect() as conn:
        db.pending = int(conn.execute(q.COUNT_PENDING_JOBS).fetchone()["n"])
        db.working = int(conn.execute(q.COUNT_WORKING_JOBS).fetchone()["n"])
        db.last_done_at = conn.execute(q.BOT_LAST_DONE_AT).fetchone()["at"]
        db.new_failed = conn.execute(q.BOT_NEW_FAILED, {"after_id": failed_after_id}).fetchall()
        db.max_failed_id = int(conn.execute(q.BOT_MAX_FAILED_ID).fetchone()["id"])
        for site, site_cfg in cfg.sites.items():
            for cam in site_cfg.cameras:
                rows = conn.execute(q.BOT_RECENT_SHARPNESS, {"site": site, "camera": cam, "n": cfg.alerts.sharpness_n}).fetchall()
                db.sharpness[(site, cam)] = [float(r["sharpness"]) for r in rows]
    return db


def destination(cfg: Config) -> str | None:
    """sites.telegram_chat_id first (the definitive home), else the temporal .env key. Never logged."""
    try:
        with q.connect() as conn:
            for site in cfg.sites:
                row = conn.execute(q.SELECT_SITE, {"site": site}).fetchone()
                if row and row["telegram_chat_id"]:
                    return str(row["telegram_chat_id"]).strip()
    except Exception as exc:
        log.warning("destination lookup failed (%s); falling back to env", type(exc).__name__)
    env = os.environ.get(ENV_CHAT_ID, "").strip()
    return env or None


# --- the bot ----------------------------------------------------------------------------------

class Bot:
    def __init__(self) -> None:
        self.state = BotState()

    async def send(self, app: Application, text: str) -> None:
        chat = await asyncio.to_thread(destination, load_config())
        if chat is None:
            log.warning("alerta sin destino (pega TELEGRAM_CHAT_ID en .env tras /id): %s", text)
            return
        try:
            await app.bot.send_message(chat_id=chat, text=text)
        except Exception as exc:
            log.error("send failed (%s): %s", type(exc).__name__, text)

    async def notify_reboot(self, app: Application, minutes: int = 1) -> None:
        """The pre-reboot notice helper (FASE 3's butler calls it; unused this era)."""
        await self.send(app, f"🔄 El cerebro se reiniciará en {minutes} min por mantenimiento. Vuelvo en unos minutos.")

    async def round(self, app: Application) -> None:
        cfg = load_config()
        now = _now()
        api = await asyncio.to_thread(read_api, cfg)
        messages: list[str] = []
        messages += rule_api(self.state, api, now)
        messages += rule_align(self.state, api)
        try:
            db = await asyncio.to_thread(read_db, cfg, self.state.failed_after_id)
            messages += rule_failures(self.state, db)
            messages += rule_backlog(self.state, db, cfg)
            messages += rule_silent_worker(self.state, db, cfg, now)
            messages += rule_lens(self.state, db, cfg, now)
        except Exception as exc:
            log.warning("db round failed: %s", type(exc).__name__)
        messages += rule_raw_log(self.state, cfg, now)
        for text in messages:
            await self.send(app, text)

    async def loop(self, app: Application) -> None:
        # Failures announced from this boot on: remember today's highest failed id before the first round.
        try:
            self.state.failed_after_id = await asyncio.to_thread(lambda: read_db(load_config(), 0).max_failed_id)
        except Exception as exc:
            log.warning("could not read the failed-id baseline (%s): starting at 0", type(exc).__name__)
        await self.send(app, "🟢 Hola, estoy en línea. Vigilo la cola, las fallas, el lente y la ventanilla.")
        while True:
            try:
                await self.round(app)
            except Exception as exc:
                log.exception("round crashed: %s", type(exc).__name__)
            await asyncio.sleep(load_config().alerts.poll_s)

    # --- commands ---
    async def cmd_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        await update.effective_message.reply_text(f"Este chat es el {chat.id}. Pégalo como TELEGRAM_CHAT_ID en el .env (temporal; en FASE 3 vive en el sitio).")

    async def cmd_cola(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            def work() -> tuple[int, int, dict[str, Any] | None]:
                with q.connect() as conn:
                    pending = int(conn.execute(q.COUNT_PENDING_JOBS).fetchone()["n"])
                    working = int(conn.execute(q.COUNT_WORKING_JOBS).fetchone()["n"])
                    last = conn.execute(q.BOT_LAST_DONE).fetchone()
                return pending, working, last
            pending, working, last = await asyncio.to_thread(work)
        except Exception as exc:
            await update.effective_message.reply_text(f"No pude leer la cola ({type(exc).__name__}).")
            return
        if last is None:
            tail = "Todavía no se ha terminado ningún plato."
        else:
            age = int(float(last["age_s"]))
            ago = f"hace {age} s" if age < 120 else f"hace {age // 60} min"
            tail = f"Último plato terminado: #{last['plate_id']} ({last['dish'] or 'sin platillo'}) {ago}."
        await update.effective_message.reply_text(f"📦 Cola: {pending} pendientes · {working} en proceso. {tail}")

    async def cmd_foto(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        cfg = load_config()
        site = next(iter(cfg.sites))
        cameras = list(cfg.sites[site].cameras)
        if not cameras:
            await update.effective_message.reply_text(f"El sitio {site} no tiene cámaras configuradas.")
            return
        camera = context.args[0] if context.args and context.args[0] in cameras else cameras[0]
        try:
            status, body, headers = await asyncio.to_thread(_http_get, f"/api/camera/frame?site={site}&camera={camera}")
        except Exception as exc:
            await update.effective_message.reply_text(f"La ventanilla no respondió ({type(exc).__name__}).")
            return
        if status == 404:
            await update.effective_message.reply_text("Sin copia fresca: nadie está capturando ahora.")
            return
        if status != 200:
            await update.effective_message.reply_text(f"La ventanilla contestó {status}.")
            return
        caption = f"📷 {site}/{camera} · nitidez {headers.get('x-sharpness', '?')} · copia de hace {headers.get('x-frame-age-s', '?')} s"
        await update.effective_message.reply_photo(photo=body, caption=caption)


def main() -> None:
    token = os.environ.get(ENV_TOKEN, "").strip()
    if not token:
        raise RuntimeError(f"{ENV_TOKEN} is not set: is .env present and passed through env_file?")
    cfg = load_config()
    bot = Bot()
    log.info("bot starting: poll_s=%d sites=%s", cfg.alerts.poll_s, list(cfg.sites))

    async def post_init(app: Application) -> None:
        app.create_task(bot.loop(app))

    app = Application.builder().token(token).post_init(post_init).build()
    app.add_handler(CommandHandler("id", bot.cmd_id))
    app.add_handler(CommandHandler("cola", bot.cmd_cola))
    app.add_handler(CommandHandler("foto", bot.cmd_foto))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
