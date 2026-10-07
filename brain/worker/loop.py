"""The queue worker: orphan rescue -> claim -> process -> write, forever (SPEC 1.2).

The worker NEVER sleeps on a failed job: the ROW waits out its backoff on the rail
(run_after) and the worker claims the next job at once. It sleeps only when the queue is
empty (idle poll), while the queue is paused, and while waiting to be born (the schema
wait-loop, SPEC 1.8). Result and job completion are written in ONE transaction, and that
transaction overwrites the provenance columns with what was ACTUALLY used: a finished row
tells the physical truth.

Two lanes ride the same rail (Fase 2, T-E5): kind='analyze' (merma) and kind='presentation'
(the express grade of an outgoing plate: its own prompt, its own shape, its own JSONB, the
same discipline). The claim's ORDER BY priority is what makes the express lane jump the
queue; the worker itself has no second queue.

Fase 3 (SPEC 1.4, 1.5): config is still read per pass, and now also carries the machine
block (rescue window, attempts, the model's time limit and answer length), each lane's
SELECTED prompt, and the queue brake (queue_paused: claim nothing while it exists). Every
finished job writes its token usage into jobs.usage INSIDE the completion transaction. Once
an hour (PURGE_SWEEP_MIN) each worker sweeps: papelera plates past their retention lose their
jobs, their row and their photo (reference/ is never touched), and extensions long past leave
horario_extensiones; SKIP LOCKED keeps several workers off the same rows. Each replica logs to
its own /data/logs/worker-<hostname>.log, prunes stale replica files at boot, and touches its
own file hourly so an idle replica never ages out.
"""

from __future__ import annotations

import json
import logging
import re
import signal
import socket
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb
from psycopg import errors as pg_errors
from pydantic import ValidationError

from brain.adapter import llm, raw_log
from brain.capture.backends import PHOTO_ROOT
from brain.db import queries as q
from brain.validator import repair
from brain.validator.models import (
    MACHINE_DEFAULTS,
    USER_ERASE_WARN_DAYS,
    USER_RECOVERY_DAYS,
    Config,
    PresentationResponse,
    load_config,
    load_presentation_prompt,
    load_prompt,
    prompt_file_for,
    prompt_version_for,
)
from brain.worker.menu_builder import MenuReference, build_reference

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("lpq.worker")

# --- named constants (SPEC section 4) -------------------------------------------------
# ORPHAN_TIMEOUT and MAX_ATTEMPTS keep their names as the absent-key defaults of the machine
# block; their values have ONE home (brain/validator/models.py). The live values are read per pass.
ORPHAN_TIMEOUT = timedelta(minutes=MACHINE_DEFAULTS.orphan_timeout_min)   # ~2x the worst legitimate attempt
BACKOFF = (timedelta(seconds=2), timedelta(seconds=8), timedelta(seconds=30))
BACKOFF_CEILING = timedelta(seconds=30)
MAX_ATTEMPTS = MACHINE_DEFAULTS.max_attempts
JOB_KIND_ANALYZE = "analyze"
JOB_KIND_PRESENTATION = "presentation"
LAST_ERROR_MAX_CHARS = 2000
PURGE_SWEEP_MIN = 60                   # the hourly gate of the papelera + extensions sweep
PURGE_BATCH = 200                      # plates per purge transaction
EXTENSION_GRACE = timedelta(days=1)    # an extension this long past its close leaves the table
WORKER_LOG_PRUNE_DAYS = 7              # a replica log file untouched this long is a dead set's: pruned at boot
REFERENCE_PREFIX = "reference/"        # the sacred folder: a purge never deletes under it

# Anti-stupid asserts: a future edit that breaks the coherence of these constants must
# fail loudly at startup, with a message that names the real values and the fix. (The live
# machine-block values are checked at config LOAD by MachineConfig and bounce on screen.)
assert BACKOFF_CEILING >= max(BACKOFF), (
    f"CONFIG ERROR: BACKOFF_CEILING ({int(BACKOFF_CEILING.total_seconds())}s) is smaller than "
    f"your largest backoff step ({int(max(BACKOFF).total_seconds())}s). You changed the retry "
    f"steps without raising the ceiling: raise BACKOFF_CEILING to at least the largest step, "
    f"or your new wait will be silently capped at {int(BACKOFF_CEILING.total_seconds())}s."
)
assert llm.LLM_TIMEOUT_S < ORPHAN_TIMEOUT.total_seconds(), (
    f"CONFIG ERROR: LLM_TIMEOUT_S ({llm.LLM_TIMEOUT_S}s) is not smaller than ORPHAN_TIMEOUT "
    f"({int(ORPHAN_TIMEOUT.total_seconds())}s): a call slower than the rescue window would be "
    f"double-claimed by a live worker. Lower LLM_TIMEOUT_S or raise ORPHAN_TIMEOUT."
)


def backoff_for(attempts: int) -> timedelta:
    """The ONE place the retry wait is computed. attempts = the attempt that just failed (1-based)."""
    step = BACKOFF[min(max(attempts, 1), len(BACKOFF)) - 1]
    return min(step, BACKOFF_CEILING)


def usage_of(results: list[llm.LLMResult]) -> dict[str, int]:
    """jobs.usage for one finished job: the four token counters summed over its calls, the call count
    (2 when the bounded re-ask fired) and the total latency. The spend dashboard's raw material."""
    out = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "calls": len(results),
        "latency_ms": 0,
    }
    for result in results:
        for key, value in result.usage().items():
            out[key] += value
        out["latency_ms"] += result.latency_ms
    return out


@dataclass(frozen=True)
class Analysis:
    answer: repair.ValidatedAnswer
    model: str
    prompt_version: str
    menu_version: int
    calls: int
    usage: dict[str, int]


@dataclass(frozen=True)
class PresentationAnalysis:
    answer: PresentationResponse
    validator: dict[str, Any]        # absent-when-off: repairs / flags of the PARSE, like analyze
    model: str
    prompt_version: str
    menu_version: int
    calls: int
    usage: dict[str, int]


def _call(cfg: Config, messages: list[dict[str, Any]], provider: str | None) -> llm.LLMResult:
    """ONE model call with this pass's machine block (max_tokens, timeout)."""
    return llm.complete(
        cfg.active_model, messages, provider,
        max_tokens=cfg.machine.max_tokens, timeout_s=cfg.machine.llm_timeout_s,
    )


# --- the four steps of a loop pass ----------------------------------------------------

def wait_for_schema(poll_seconds: int) -> None:
    """Sleep until the fastapi init has built the tables. Legitimate sleep: nothing to do yet."""
    while True:
        try:
            with q.connect() as conn:
                conn.execute(q.WORKER_WAIT)
            log.info("schema present, entering the queue loop")
            return
        except Exception as exc:
            log.info("waiting for the schema (%s), retry in %ss", type(exc).__name__, poll_seconds)
            time.sleep(poll_seconds)


def rescue_orphans(orphan_timeout: timedelta = ORPHAN_TIMEOUT) -> int:
    with q.connect() as conn:
        rescued = conn.execute(q.RESCUE_ORPHANS, {"orphan_timeout": orphan_timeout}).rowcount
    if rescued:
        log.warning("rescued %d orphaned job(s) stuck 'working' > %s", rescued, orphan_timeout)
    return rescued


def claim() -> dict[str, Any] | None:
    with q.connect() as conn:
        return conn.execute(q.CLAIM_JOB).fetchone()


def analyze(plate_id: int, cfg: Config) -> Analysis:
    """Build the reference, make ONE call (plus at most one bounded re-ask), validate."""
    with q.connect() as conn:
        plate = conn.execute(q.SELECT_PLATE, {"id": plate_id}).fetchone()
        if plate is None:
            raise RuntimeError(f"plate {plate_id} does not exist")
        reference: MenuReference = build_reference(conn, plate["site"])

    jpeg = (PHOTO_ROOT / plate["photo_path"]).read_bytes()
    system_prompt = load_prompt(prompt_file_for(cfg, "merma"))   # the SELECTED prompt of the merma lane
    provider = llm.provider_for(cfg.active_model)
    cache_on = cfg.cache == "on"
    messages = llm.build_messages(system_prompt, reference, jpeg, cache_on, provider)

    exchanges: list[tuple[str, list[dict[str, Any]], llm.LLMResult]] = []
    first = _call(cfg, messages, provider)
    exchanges.append(("analyze", messages, first))

    def reask(bad_text: str) -> str:
        repair_messages = llm.build_repair_messages(messages, bad_text)
        second = _call(cfg, repair_messages, provider)
        exchanges.append(("repair", repair_messages, second))
        return second.text

    answer = repair.validate(first.text, reask, reference)

    if cfg.raw_logging:
        for kind, sent, result in exchanges:
            raw_log.record(
                kind=kind,
                model=result.model,
                messages=sent,
                response_text=result.text,
                usage=result.usage(),
                meta={"plate_id": plate_id, "site": plate["site"], "latency_ms": result.latency_ms},
            )

    return Analysis(
        answer=answer,
        model=cfg.active_model,
        prompt_version=prompt_version_for(cfg, "merma"),
        menu_version=reference.menu_version,
        calls=len(exchanges),
        usage=usage_of([result for _, _, result in exchanges]),
    )


def write_done(job_id: int, plate_id: int, analysis: Analysis) -> None:
    """Result + job completion (with its usage) + true provenance, welded in ONE transaction."""
    answer = analysis.answer
    with q.connect() as conn, conn.transaction():
        conn.execute(
            q.WRITE_ANALYZE_RESULT,
            {
                "plate_id": plate_id,
                "dish_predicted": answer.dish,
                "leftovers": Jsonb(answer.leftovers),
                "confidence": answer.confidence,
                "validator": Jsonb(answer.validator_json()),
                "model": analysis.model,
                "prompt_version": analysis.prompt_version,
                "menu_version": analysis.menu_version,
            },
        )
        conn.execute(q.MARK_JOB_DONE_WITH_USAGE, {"job_id": job_id, "usage": Jsonb(analysis.usage)})


# --- the presentation lane (T-E5) -----------------------------------------------------

# repair.py is sealed to the merma shape this era, so the express lane carries its own small
# parse: direct JSON, else strip fences/prose around the outermost {...}. Same philosophy:
# a bad answer is data, never a crash.
_FENCE_RE = re.compile(r"```[a-zA-Z]*")


def _try_parse_presentation(text: str) -> PresentationResponse | None:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return PresentationResponse.model_validate(data)
    except ValidationError:
        return None


def parse_presentation(text: str) -> tuple[PresentationResponse | None, int]:
    """(answer, repairs): repairs is 1 only when a strip was needed AND succeeded."""
    direct = _try_parse_presentation(text.strip())
    if direct is not None:
        return direct, 0
    cleaned = _FENCE_RE.sub("", text)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        stripped = cleaned[start : end + 1].strip()
        if stripped != text.strip():
            fixed = _try_parse_presentation(stripped)
            if fixed is not None:
                return fixed, 1
    return None, 0


def analyze_presentation(plate_id: int, cfg: Config) -> PresentationAnalysis:
    """The express grade: same reference, the presentation prompt, ONE call (+ one bounded re-ask)."""
    with q.connect() as conn:
        plate = conn.execute(q.SELECT_PLATE, {"id": plate_id}).fetchone()
        if plate is None:
            raise RuntimeError(f"plate {plate_id} does not exist")
        reference: MenuReference = build_reference(conn, plate["site"])

    jpeg = (PHOTO_ROOT / plate["photo_path"]).read_bytes()
    system_prompt = load_presentation_prompt(prompt_file_for(cfg, "presentacion"))   # the lane's SELECTED prompt
    provider = llm.provider_for(cfg.active_model)
    cache_on = cfg.cache == "on"
    messages = llm.build_messages(system_prompt, reference, jpeg, cache_on, provider)

    exchanges: list[tuple[str, list[dict[str, Any]], llm.LLMResult]] = []
    first = _call(cfg, messages, provider)
    exchanges.append(("presentation", messages, first))

    answer, repairs = parse_presentation(first.text)
    if answer is None:
        # The bounded re-ask: a deliberate second call inside the same attempt, never a transport retry.
        repairs += 1
        repair_messages = llm.build_repair_messages(messages, first.text)
        second = _call(cfg, repair_messages, provider)
        exchanges.append(("presentation_repair", repair_messages, second))
        answer, more = parse_presentation(second.text)
        repairs += more

    flags: list[str] = []
    if answer is None:
        # Nothing parseable twice: a flagged row without a grade; the review sees it.
        answer = PresentationResponse.model_validate(
            {"dish": repair.UNKNOWN_DISH, "score": 0, "pass": False, "issues": [], "flags": []}
        )
        flags.append(repair.FLAG_UNPARSEABLE)
    elif answer.dish != repair.UNKNOWN_DISH and answer.dish not in reference.dishes:
        flags.append(repair.FLAG_DISH_NOT_IN_MENU)

    validator: dict[str, Any] = {}
    if repairs:
        validator["repairs"] = repairs
    if flags:
        validator["flags"] = flags

    if cfg.raw_logging:
        for kind, sent, result in exchanges:
            raw_log.record(
                kind=kind,
                model=result.model,
                messages=sent,
                response_text=result.text,
                usage=result.usage(),
                meta={"plate_id": plate_id, "site": plate["site"], "latency_ms": result.latency_ms},
            )

    return PresentationAnalysis(
        answer=answer,
        validator=validator,
        model=cfg.active_model,
        prompt_version=prompt_version_for(cfg, "presentacion"),
        menu_version=reference.menu_version,
        calls=len(exchanges),
        usage=usage_of([result for _, _, result in exchanges]),
    )


def write_presentation_done(job_id: int, plate_id: int, analysis: PresentationAnalysis) -> None:
    """Grade + job completion (with its usage) + true provenance (the lane's own prompt version), ONE transaction."""
    with q.connect() as conn, conn.transaction():
        conn.execute(
            q.WRITE_PRESENTATION_RESULT,
            {
                "plate_id": plate_id,
                "dish_predicted": analysis.answer.dish,
                "presentation": Jsonb(analysis.answer.presentation_json()),
                "validator": Jsonb(analysis.validator),
                "model": analysis.model,
                "prompt_version": analysis.prompt_version,
                "menu_version": analysis.menu_version,
            },
        )
        conn.execute(q.MARK_JOB_DONE_WITH_USAGE, {"job_id": job_id, "usage": Jsonb(analysis.usage)})


# --- failure and dispatch -------------------------------------------------------------

def fail_attempt(job: dict[str, Any], exc: Exception, max_attempts: int = MAX_ATTEMPTS) -> None:
    """Back to the rail with a stamp, or 'failed' after the last attempt. The worker moves on."""
    job_id, attempts = int(job["id"]), int(job["attempts"])
    last_error = f"{type(exc).__name__}: {exc}"[:LAST_ERROR_MAX_CHARS]
    with q.connect() as conn:
        if attempts >= max_attempts:
            conn.execute(q.MARK_JOB_FAILED, {"job_id": job_id, "last_error": last_error})
            log.error("job=%d FAILED after %d attempts: %s", job_id, attempts, last_error)
        else:
            wait = backoff_for(attempts)
            conn.execute(q.REQUEUE_JOB, {"job_id": job_id, "backoff": wait, "last_error": last_error})
            log.warning(
                "job=%d attempt %d/%d failed, back to pending, run_after +%ds: %s",
                job_id, attempts, max_attempts, int(wait.total_seconds()), last_error,
            )


def process(job: dict[str, Any], cfg: Config) -> None:
    job_id, plate_id, kind = int(job["id"]), int(job["plate_id"]), str(job["kind"])
    max_attempts = cfg.machine.max_attempts
    log.info("claimed job=%d plate=%d kind=%s attempt=%d/%d", job_id, plate_id, kind, job["attempts"], max_attempts)
    try:
        if kind == JOB_KIND_ANALYZE:
            analysis = analyze(plate_id, cfg)
            write_done(job_id, plate_id, analysis)
            answer = analysis.answer
            log.info(
                "done job=%d plate=%d dish=%s confidence=%s leftovers=%s validator=%s calls=%d "
                "model=%s prompt_version=%s menu_version=%d usage=%s",
                job_id, plate_id, answer.dish, answer.confidence, answer.leftovers,
                answer.validator_json(), analysis.calls, analysis.model,
                analysis.prompt_version, analysis.menu_version, analysis.usage,
            )
        elif kind == JOB_KIND_PRESENTATION:
            presentation = analyze_presentation(plate_id, cfg)
            write_presentation_done(job_id, plate_id, presentation)
            grade = presentation.answer
            log.info(
                "done presentation job=%d plate=%d dish=%s score=%d pass=%s issues=%s flags=%s "
                "validator=%s calls=%d model=%s prompt_version=%s menu_version=%d usage=%s",
                job_id, plate_id, grade.dish, grade.score, grade.passed, grade.issues, grade.flags,
                presentation.validator, presentation.calls, presentation.model,
                presentation.prompt_version, presentation.menu_version, presentation.usage,
            )
        else:
            raise ValueError(f"unsupported job kind {kind!r}")
    except Exception as exc:  # any failure of the attempt: the rail decides, the worker moves on
        fail_attempt(job, exc, max_attempts)


# --- the hourly sweep (Fase 3) --------------------------------------------------------

def _unlink_plate_photo(rel_path: str) -> bool:
    """Delete one purged plate's photo. Anything under reference/ (the sacred originals and the diet
    copies) is refused by construction, and so is any path that tries to leave the photos root."""
    path = Path(rel_path)
    if rel_path.startswith(REFERENCE_PREFIX) or path.is_absolute() or ".." in path.parts:
        log.error("purge refused to delete a protected path: %s", rel_path)
        return False
    (PHOTO_ROOT / rel_path).unlink(missing_ok=True)
    return True


def purge_papelera(cfg: Config) -> int:
    """Plates discarded more than papelera_dias ago leave for good: their jobs FIRST (the foreign key
    has no cascade and schema.sql stays sealed), then the row, then the photo. Batches of PURGE_BATCH,
    each in its own transaction with its bitácora row, until nothing is due."""
    retention = timedelta(days=cfg.papelera_dias)
    total = 0
    while True:
        with q.connect() as conn, conn.transaction():
            rows = conn.execute(q.PURGE_DUE_DISCARDED, {"retention": retention, "limit": PURGE_BATCH}).fetchall()
            if not rows:
                break
            ids = [int(r["id"]) for r in rows]
            conn.execute(q.DELETE_JOBS_OF_PLATES, {"ids": ids})
            conn.execute(q.DELETE_PLATES_BY_IDS, {"ids": ids})
            conn.execute(
                q.INSERT_ADMIN_LOG,
                {"usuario": None, "action": "papelera.purge", "detail": Jsonb({"plates": ids, "dias": cfg.papelera_dias})},
            )
        # Files only after the rows are gone for good: a crash in between leaves an orphan file, never
        # a row pointing at a missing photo.
        for row in rows:
            _unlink_plate_photo(row["photo_path"])
        total += len(rows)
        if len(rows) < PURGE_BATCH:
            break
    return total


def purge_extensions() -> int:
    """One-night extensions whose close passed more than EXTENSION_GRACE ago leave horario_extensiones."""
    with q.connect() as conn:
        return conn.execute(q.PURGE_OLD_EXTENSIONS, {"grace": EXTENSION_GRACE}).rowcount


def warn_and_erase_users() -> tuple[int, int]:
    """Deleted accounts (owner ruling 3.9.2). USER_ERASE_WARN_DAYS before the erase: ONE 'user.erase_soon'
    bitácora row per account (the bot turns it into the admin chat's notice), marked with erase_warned_at.
    At USER_RECOVERY_DAYS the row leaves users for good, with a 'user.erase' row; its number is never reused.
    Returns (warned, erased). A database the api has not migrated yet this boot is skipped quietly."""
    retention = timedelta(days=USER_RECOVERY_DAYS)
    warn_after = timedelta(days=USER_RECOVERY_DAYS - USER_ERASE_WARN_DAYS)
    try:
        with q.connect() as conn, conn.transaction():
            due = conn.execute(q.USERS_TO_WARN, {"warn_after": warn_after, "retention": retention}).fetchall()
            for row in due:
                conn.execute(q.MARK_USER_WARNED, {"usuario": row["usuario"]})
                conn.execute(q.INSERT_ADMIN_LOG, {"usuario": None, "action": "user.erase_soon", "detail": Jsonb({
                    "usuario": row["usuario"], "uid": row["uid"],
                    "borra": (row["deleted_at"] + retention).isoformat(),
                })})
            erased = conn.execute(q.ERASE_DELETED_USERS, {"retention": retention}).fetchall()
            for row in erased:
                conn.execute(q.INSERT_ADMIN_LOG, {"usuario": None, "action": "user.erase", "detail": Jsonb({
                    "usuario": row["usuario"], "uid": row["uid"],
                })})
    except pg_errors.UndefinedColumn:
        log.info("account sweep skipped: the api has not run this boot's migrations yet")
        return 0, 0
    return len(due), len(erased)


def sweep(cfg: Config, own_log: Path | None) -> None:
    """The hourly duties. An error here never stops the queue: it is logged and retried next hour."""
    try:
        purged = purge_papelera(cfg)
        ended = purge_extensions()
        warned, erased = warn_and_erase_users()
        if purged or ended or warned or erased:
            log.info(
                "sweep: papelera purged %d plate(s) (retention %d days), %d old extension(s) removed, "
                "%d deleted account(s) warned, %d erased",
                purged, cfg.papelera_dias, ended, warned, erased,
            )
    except Exception:
        log.exception("sweep failed; retried next hour")
    if own_log is not None:
        try:
            own_log.touch(exist_ok=True)   # an idle replica's file must never look stale to a peer's prune
        except OSError:
            pass


# --- the loop -------------------------------------------------------------------------

@dataclass
class PassState:
    own_log: Path | None = None
    last_sweep: float | None = None   # monotonic time of the last sweep; None = sweep on the first pass
    paused: bool = False


def run_pass(cfg: Config, state: PassState) -> str:
    """One pass: sweep when due, rescue orphans, then (unless the brake is on) claim and process ONE job.
    Returns 'paused', 'idle' or 'worked'; the caller sleeps after the first two."""
    now = time.monotonic()
    if state.last_sweep is None or now - state.last_sweep >= PURGE_SWEEP_MIN * 60:
        state.last_sweep = now
        sweep(cfg, state.own_log)
    rescue_orphans(timedelta(minutes=cfg.machine.orphan_timeout_min))
    if cfg.queue_paused:
        if not state.paused:
            state.paused = True
            log.info("queue paused from the mostrador: claiming nothing until it resumes")
        return "paused"
    if state.paused:
        state.paused = False
        log.info("queue resumed: claiming again")
    job = claim()
    if job is None:
        return "idle"
    process(job, cfg)
    return "worked"


class _Stop:
    requested = False


def _on_sigterm(signum: int, frame: Any) -> None:
    # Finish the job in hand, then leave. A kill mid-call is covered by the orphan rescue.
    _Stop.requested = True
    log.info("SIGTERM received: finishing the current job, then stopping")


def main() -> None:
    signal.signal(signal.SIGTERM, _on_sigterm)
    own_log = raw_log.add_service_log_file(f"worker-{socket.gethostname()}")
    pruned = raw_log.prune_stale_logs("worker-*.log*", keep=own_log, days=WORKER_LOG_PRUNE_DAYS)
    cfg = load_config()
    m = cfg.machine
    log.info(
        "worker starting: orphan_timeout=%dmin backoff=%s ceiling=%s max_attempts=%d llm_timeout=%ds "
        "max_tokens=%d prompt_version=%s presentation_prompt_version=%s log_file=%s pruned_logs=%s",
        m.orphan_timeout_min, [int(b.total_seconds()) for b in BACKOFF], BACKOFF_CEILING,
        m.max_attempts, m.llm_timeout_s, m.max_tokens, prompt_version_for(cfg, "merma"),
        prompt_version_for(cfg, "presentacion"), own_log, pruned or "none",
    )
    wait_for_schema(cfg.poll_seconds)

    state = PassState(own_log=own_log)
    while not _Stop.requested:
        cfg = load_config()          # per pass = per job: every mostrador and machine-room knob enters hot
        if run_pass(cfg, state) != "worked":
            time.sleep(cfg.poll_seconds)   # idle or paused: the only sleeps in the busy loop

    log.info("worker stopped")


if __name__ == "__main__":
    main()
