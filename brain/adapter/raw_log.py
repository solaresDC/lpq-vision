"""The log rivers on disk: the forensic river (river 2) and, from Fase 3, each service's log file.

River 2, the forensic river: full LLM request/response, minus image bytes, to day files.
Default OFF (config raw_logging: false); the worker calls record() only when it is on.
Images are ALWAYS stripped: a base64 photo in a log is bulk without evidence value.
Docker's log rotation does not reach these files, so the writer prunes itself: once the
folder exceeds RAW_LOG_MAX_MB, the oldest day files go, the current one never does. With
several worker replicas writing and pruning the same folder, a file can vanish between the
listing, the size read and the delete: all three tolerate it.

Service log files (Fase 3, SPEC 1.6): each Python service keeps a RotatingFileHandler
(LOG_FILE_MB x LOG_FILE_KEEP) beside its stdout logging, in LOG_DIR, the folder the machine
room's SSE viewer tails. Stdout stays the first river (docker compose logs); these files are
the viewer's window, never the archive. A scaled worker replica writes its own
worker-<hostname>.log (the handler is single-process), and stale replica files are pruned at
boot: recreated containers get new hostnames, and dead sets must never grow the volume.
"""

from __future__ import annotations

import copy
import json
import logging
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

log = logging.getLogger("lpq.adapter.raw_log")

# --- named constants (SPEC section 4) -------------------------------------------------
RAW_LOG_DIR = Path("/data/logs/llm_raw")
RAW_LOG_MAX_MB = 200
LOG_DIR = Path("/data/logs")                 # the service log files the SSE viewer tails
LOG_FILE_MB = 5
LOG_FILE_KEEP = 2
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

IMAGE_PLACEHOLDER = "<image stripped: {n} chars base64>"


def strip_images(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deep copy of the messages with every image block replaced by a size placeholder."""
    stripped = copy.deepcopy(messages)
    for message in stripped:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "image_url":
                url = str(block.get("image_url", {}).get("url", ""))
                block["image_url"] = {"url": IMAGE_PLACEHOLDER.format(n=len(url))}
    return stripped


def _day_file(now: datetime) -> Path:
    return RAW_LOG_DIR / f"{now.strftime('%Y%m%d')}.jsonl"


def _size(path: Path) -> int:
    """A file's size, or 0 when another replica deleted it after the listing."""
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _prune() -> None:
    """Auto-poda: delete the oldest day files until the folder is under the cap; keep today's.
    Race-tolerant: a file another replica already removed counts as 0 bytes and unlinks quietly."""
    files = sorted(RAW_LOG_DIR.glob("*.jsonl"))   # day names sort chronologically
    sizes = {f: _size(f) for f in files}
    total = sum(sizes.values())
    cap = RAW_LOG_MAX_MB * 1024 * 1024
    while total > cap and len(files) > 1:
        oldest = files.pop(0)
        total -= sizes[oldest]
        oldest.unlink(missing_ok=True)
        log.info("raw log pruned: %s", oldest.name)


def record(
    kind: str,
    model: str,
    messages: list[dict[str, Any]],
    response_text: str,
    usage: dict[str, int],
    meta: dict[str, Any],
) -> Path:
    """Append one JSON line (request without images, response, usage, meta) to today's file."""
    now = datetime.now(timezone.utc)
    entry = {
        "ts": now.isoformat(),
        "kind": kind,
        "model": model,
        "request": strip_images(messages),
        "response": response_text,
        "usage": usage,
        **meta,
    }
    RAW_LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = _day_file(now)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    _prune()
    return path


def add_service_log_file(name: str) -> Path | None:
    """Attach a RotatingFileHandler for this process at LOG_DIR/<name>.log, beside stdout.
    Returns the path, or None when the folder is not writable: a service never dies for its log file."""
    path = LOG_DIR / f"{name}.log"
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path, maxBytes=LOG_FILE_MB * 1024 * 1024, backupCount=LOG_FILE_KEEP, encoding="utf-8"
        )
    except OSError as exc:
        log.warning("service log file %s unavailable (%s): stdout only", path, type(exc).__name__)
        return None
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logging.getLogger().addHandler(handler)
    return path


def prune_stale_logs(pattern: str, keep: Path | None, days: int) -> list[str]:
    """Delete LOG_DIR files matching `pattern` untouched for `days`+ days, never `keep` or its rotations.
    Returns the names removed; a file that vanished meanwhile is simply skipped."""
    cutoff = time.time() - days * 86400
    removed: list[str] = []
    for f in LOG_DIR.glob(pattern):
        if keep is not None and (f.name == keep.name or f.name.startswith(keep.name + ".")):
            continue
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
                removed.append(f.name)
        except FileNotFoundError:
            continue
    return removed
