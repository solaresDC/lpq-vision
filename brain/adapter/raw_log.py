"""The forensic river (river 2): full LLM request/response, minus image bytes, to day files.

Default OFF (config raw_logging: false); the worker calls record() only when it is on.
Images are ALWAYS stripped: a base64 photo in a log is bulk without evidence value.
Docker's log rotation does not reach these files, so the writer prunes itself: once the
folder exceeds RAW_LOG_MAX_MB, the oldest day files go, the current one never does.
"""

from __future__ import annotations

import copy
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("lpq.adapter.raw_log")

# --- named constants (SPEC section 4) -------------------------------------------------
RAW_LOG_DIR = Path("/data/logs/llm_raw")
RAW_LOG_MAX_MB = 200

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


def _prune() -> None:
    """Auto-poda: delete the oldest day files until the folder is under the cap; keep today's."""
    files = sorted(RAW_LOG_DIR.glob("*.jsonl"))   # day names sort chronologically
    total = sum(f.stat().st_size for f in files)
    cap = RAW_LOG_MAX_MB * 1024 * 1024
    while total > cap and len(files) > 1:
        oldest = files.pop(0)
        total -= oldest.stat().st_size
        oldest.unlink()
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
