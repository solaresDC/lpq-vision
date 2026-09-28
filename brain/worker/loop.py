"""The queue worker: orphan rescue -> claim -> process -> write, forever (SPEC 1.2).

The worker NEVER sleeps on a failed job: the ROW waits out its backoff on the rail
(run_after) and the worker claims the next job at once. It sleeps only when the queue is
empty (idle poll) and while waiting to be born (the schema wait-loop, SPEC 1.8). Result
and job completion are written in ONE transaction, and that transaction overwrites the
provenance columns with what was ACTUALLY used: a finished row tells the physical truth.

Two lanes ride the same rail (Fase 2, T-E5): kind='analyze' (merma, the Fase-1 path,
untouched) and kind='presentation' (the express grade of an outgoing plate: its own prompt,
its own shape, its own JSONB, the same discipline). The claim's ORDER BY priority is what
makes the express lane jump the queue; the worker itself has no second queue.
"""

from __future__ import annotations

import json
import logging
import re
import signal
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from psycopg.types.json import Jsonb
from pydantic import ValidationError

from brain.adapter import llm, raw_log
from brain.capture.backends import PHOTO_ROOT
from brain.db import queries as q
from brain.validator import repair
from brain.validator.models import (
    PRESENTATION_PROMPT_VERSION,
    PROMPT_VERSION,
    Config,
    PresentationResponse,
    load_config,
    load_presentation_prompt,
    load_prompt,
)
from brain.worker.menu_builder import MenuReference, build_reference

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("lpq.worker")

# --- named constants (SPEC section 4) -------------------------------------------------
ORPHAN_TIMEOUT = timedelta(minutes=10)      # ~2x the worst legitimate attempt: a SAFETY margin
BACKOFF = (timedelta(seconds=2), timedelta(seconds=8), timedelta(seconds=30))
BACKOFF_CEILING = timedelta(seconds=30)
MAX_ATTEMPTS = 3
JOB_KIND_ANALYZE = "analyze"
JOB_KIND_PRESENTATION = "presentation"
LAST_ERROR_MAX_CHARS = 2000

# Anti-stupid asserts: a future edit that breaks the coherence of these constants must
# fail loudly at startup, with a message that names the real values and the fix.
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


@dataclass(frozen=True)
class Analysis:
    answer: repair.ValidatedAnswer
    model: str
    prompt_version: str
    menu_version: int
    calls: int


@dataclass(frozen=True)
class PresentationAnalysis:
    answer: PresentationResponse
    validator: dict[str, Any]        # absent-when-off: repairs / flags of the PARSE, like analyze
    model: str
    prompt_version: str
    menu_version: int
    calls: int


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


def rescue_orphans() -> int:
    with q.connect() as conn:
        rescued = conn.execute(q.RESCUE_ORPHANS, {"orphan_timeout": ORPHAN_TIMEOUT}).rowcount
    if rescued:
        log.warning("rescued %d orphaned job(s) stuck 'working' > %s", rescued, ORPHAN_TIMEOUT)
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
    system_prompt = load_prompt()
    provider = llm.provider_for(cfg.active_model)
    cache_on = cfg.cache == "on"
    messages = llm.build_messages(system_prompt, reference, jpeg, cache_on, provider)

    exchanges: list[tuple[str, list[dict[str, Any]], llm.LLMResult]] = []
    first = llm.complete(cfg.active_model, messages, provider)
    exchanges.append(("analyze", messages, first))

    def reask(bad_text: str) -> str:
        repair_messages = llm.build_repair_messages(messages, bad_text)
        second = llm.complete(cfg.active_model, repair_messages, provider)
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
        prompt_version=PROMPT_VERSION,
        menu_version=reference.menu_version,
        calls=len(exchanges),
    )


def write_done(job_id: int, plate_id: int, analysis: Analysis) -> None:
    """Result + job completion + true provenance, welded in ONE transaction."""
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
        conn.execute(q.MARK_JOB_DONE, {"job_id": job_id})


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
    system_prompt = load_presentation_prompt()
    provider = llm.provider_for(cfg.active_model)
    cache_on = cfg.cache == "on"
    messages = llm.build_messages(system_prompt, reference, jpeg, cache_on, provider)

    exchanges: list[tuple[str, list[dict[str, Any]], llm.LLMResult]] = []
    first = llm.complete(cfg.active_model, messages, provider)
    exchanges.append(("presentation", messages, first))

    answer, repairs = parse_presentation(first.text)
    if answer is None:
        # The bounded re-ask: a deliberate second call inside the same attempt, never a transport retry.
        repairs += 1
        repair_messages = llm.build_repair_messages(messages, first.text)
        second = llm.complete(cfg.active_model, repair_messages, provider)
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
        prompt_version=PRESENTATION_PROMPT_VERSION,
        menu_version=reference.menu_version,
        calls=len(exchanges),
    )


def write_presentation_done(job_id: int, plate_id: int, analysis: PresentationAnalysis) -> None:
    """Grade + job completion + true provenance (the presentation prompt's own version), ONE transaction."""
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
        conn.execute(q.MARK_JOB_DONE, {"job_id": job_id})


# --- failure and dispatch -------------------------------------------------------------

def fail_attempt(job: dict[str, Any], exc: Exception) -> None:
    """Back to the rail with a stamp, or 'failed' after the last attempt. The worker moves on."""
    job_id, attempts = int(job["id"]), int(job["attempts"])
    last_error = f"{type(exc).__name__}: {exc}"[:LAST_ERROR_MAX_CHARS]
    with q.connect() as conn:
        if attempts >= MAX_ATTEMPTS:
            conn.execute(q.MARK_JOB_FAILED, {"job_id": job_id, "last_error": last_error})
            log.error("job=%d FAILED after %d attempts: %s", job_id, attempts, last_error)
        else:
            wait = backoff_for(attempts)
            conn.execute(q.REQUEUE_JOB, {"job_id": job_id, "backoff": wait, "last_error": last_error})
            log.warning(
                "job=%d attempt %d/%d failed, back to pending, run_after +%ds: %s",
                job_id, attempts, MAX_ATTEMPTS, int(wait.total_seconds()), last_error,
            )


def process(job: dict[str, Any], cfg: Config) -> None:
    job_id, plate_id, kind = int(job["id"]), int(job["plate_id"]), str(job["kind"])
    log.info("claimed job=%d plate=%d kind=%s attempt=%d/%d", job_id, plate_id, kind, job["attempts"], MAX_ATTEMPTS)
    try:
        if kind == JOB_KIND_ANALYZE:
            analysis = analyze(plate_id, cfg)
            write_done(job_id, plate_id, analysis)
            answer = analysis.answer
            log.info(
                "done job=%d plate=%d dish=%s confidence=%s leftovers=%s validator=%s calls=%d "
                "model=%s prompt_version=%s menu_version=%d",
                job_id, plate_id, answer.dish, answer.confidence, answer.leftovers,
                answer.validator_json(), analysis.calls, analysis.model,
                analysis.prompt_version, analysis.menu_version,
            )
        elif kind == JOB_KIND_PRESENTATION:
            presentation = analyze_presentation(plate_id, cfg)
            write_presentation_done(job_id, plate_id, presentation)
            grade = presentation.answer
            log.info(
                "done presentation job=%d plate=%d dish=%s score=%d pass=%s issues=%s flags=%s "
                "validator=%s calls=%d model=%s prompt_version=%s menu_version=%d",
                job_id, plate_id, grade.dish, grade.score, grade.passed, grade.issues, grade.flags,
                presentation.validator, presentation.calls, presentation.model,
                presentation.prompt_version, presentation.menu_version,
            )
        else:
            raise ValueError(f"unsupported job kind {kind!r}")
    except Exception as exc:  # any failure of the attempt: the rail decides, the worker moves on
        fail_attempt(job, exc)


# --- the loop -------------------------------------------------------------------------

class _Stop:
    requested = False


def _on_sigterm(signum: int, frame: Any) -> None:
    # Finish the job in hand, then leave. A kill mid-call is covered by the orphan rescue.
    _Stop.requested = True
    log.info("SIGTERM received: finishing the current job, then stopping")


def main() -> None:
    signal.signal(signal.SIGTERM, _on_sigterm)
    log.info(
        "worker starting: orphan_timeout=%s backoff=%s ceiling=%s max_attempts=%d llm_timeout=%ds "
        "prompt_version=%s presentation_prompt_version=%s",
        ORPHAN_TIMEOUT, [int(b.total_seconds()) for b in BACKOFF], BACKOFF_CEILING,
        MAX_ATTEMPTS, llm.LLM_TIMEOUT_S, PROMPT_VERSION, PRESENTATION_PROMPT_VERSION,
    )
    wait_for_schema(load_config().poll_seconds)

    while not _Stop.requested:
        cfg = load_config()          # per pass = per job: the contract for the future admin
        rescue_orphans()
        job = claim()
        if job is None:
            time.sleep(cfg.poll_seconds)   # idle poll: the only sleep in the busy loop
            continue
        process(job, cfg)

    log.info("worker stopped")


if __name__ == "__main__":
    main()
