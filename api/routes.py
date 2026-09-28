"""LPQ_VISION api/routes.py: every Fase-2 /api/* endpoint beyond the Fase-1 three and auth.

Section 2.6 (this file's birth) is the CAPTURE LANE: /api/upload_burst, the ephemeral frame
store with /api/camera/frame (POST + GET) and /api/camera/state, the alignment mode
(/api/camera/align/start|stop) and the calibration write (/api/camera/calibration).
Section 2.7 adds review, stats, gallery and the photo stream to this same file.

Session-free lane (the club is the outer wall this era; /captura runs as a kiosk; the bot's
/foto reads the frame over the internal network): upload_burst, camera/frame, camera/state.
Behind the session: align/start, align/stop, calibration, and everything 2.7 adds.

The frame store is RAM only (SPEC 1.5): copies overwrite, nothing reaches disk, DB or logs; a
restart blanks it and nothing breaks. It and the RAM sessions depend on the api being ONE
uvicorn process, which it is and must remain this era.

opencv-python-headless is imported HERE and nowhere under brain/: the api owns the one
sharpness thermometer of the whole system (Laplacian variance on a gray copy downscaled to
SHARPNESS_MAX_SIDE, so a phone burst frame and a small mirilla copy read on the same scale).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import JSONResponse, Response
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from api.auth import Session, require_session
from brain.db import queries as q
from brain.validator.models import (
    CONFIG_PATH,
    PRESENTATION_PROMPT_VERSION,
    PROMPT_VERSION,
    AlignStartRequest,
    AlignStopRequest,
    CalibrationRequest,
    CameraConfig,
    Config,
    SiteConfig,
    UploadRequest,
    load_config,
)

log = logging.getLogger("lpq.api.routes")

# --- named constants (SPEC section 4) -------------------------------------------------
JOB_KIND_ANALYZE = "analyze"
JOB_KIND_PRESENTATION = "presentation"
JOB_PRIORITY_RETURN = 100
JOB_PRIORITY_EXPRESS = 10          # the claim's ORDER BY priority makes any free worker serve it first
FRAME_POLL_S = 5                   # the mirilla's poll cadence (told to the pages via /api/camera/state)
WATCHED_TTL_S = 15                 # how long one GET of the frame keeps a camera "watched"
SHARPNESS_MAX_SIDE = 640           # px: every scored frame is downscaled to this longest side first
MAX_FRAME_BYTES = 8 * 1024 * 1024  # a mirilla copy larger than this is refused

router = APIRouter()


class Rejected(Exception):
    """A request the lane refuses, with the status and the plain reason the client gets."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


def _error(status: int, reason: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": reason})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "si")


# --- the one sharpness thermometer ----------------------------------------------------

def sharpness_of(jpeg: bytes) -> float | None:
    """Laplacian variance of the gray, downscaled frame; None when the bytes are not an image."""
    img = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return None
    h, w = img.shape[:2]
    longest = max(h, w)
    if longest > SHARPNESS_MAX_SIDE:
        scale = SHARPNESS_MAX_SIDE / longest
        img = cv2.resize(img, (max(1, round(w * scale)), max(1, round(h * scale))), interpolation=cv2.INTER_AREA)
    return round(float(cv2.Laplacian(img, cv2.CV_64F).var()), 2)


# --- config lookups -------------------------------------------------------------------

def _site_cfg(cfg: Config, site: str) -> SiteConfig:
    site_cfg = cfg.sites.get(site)
    if site_cfg is None:
        raise Rejected(400, f"unknown site: {site}")
    return site_cfg


def _camera_cfg(site_cfg: SiteConfig, site: str, camera: str) -> CameraConfig:
    cam = site_cfg.cameras.get(camera)
    if cam is None:
        known = ", ".join(sorted(site_cfg.cameras)) or "ninguna"
        raise Rejected(400, f"unknown camera {camera!r} for site {site!r}; known: {known}")
    return cam


def _own_site_or_403(session: Session, site: str) -> None:
    if not session.is_admin and session.site != site:
        raise Rejected(403, f"sitio fuera de tu alcance: {site}")


# --- the ephemeral frame store (SPEC 1.5) ---------------------------------------------

@dataclass
class FrameSlot:
    jpeg: bytes
    ts: datetime
    sharpness: float | None


# All three dicts are keyed by (site, camera) and guarded by ONE lock; scoring runs in threads.
_FRAMES: dict[tuple[str, str], FrameSlot] = {}       # the latest copy; the next one overwrites it
_WATCHED: dict[tuple[str, str], datetime] = {}       # until when someone is looking at that camera
_ALIGN: dict[tuple[str, str], datetime] = {}         # until when that camera's capture is PAUSED
_LOCK = threading.Lock()


def _is_watched(key: tuple[str, str], now: datetime) -> bool:
    until = _WATCHED.get(key)
    return until is not None and until > now


def _align_state(key: tuple[str, str], now: datetime) -> dict[str, Any]:
    """{active: false} or {active: true, until, seconds_left}; an expired rope is dropped here."""
    until = _ALIGN.get(key)
    if until is None:
        return {"active": False}
    if until <= now:
        _ALIGN.pop(key, None)
        return {"active": False}
    return {"active": True, "until": until.isoformat(), "seconds_left": int((until - now).total_seconds())}


@router.post("/api/camera/frame")
async def push_frame(
    frame: UploadFile | None = File(None),
    site: str | None = Form(None),
    camera: str | None = Form(None),
) -> Any:
    """From captura.js: store the latest copy in RAM, score it, and tell the page whether anyone
    is watching (keep pushing) and whether its capture is paused by the alignment mode."""
    if frame is None:
        return _error(400, "missing frame")
    if not site or not site.strip() or not camera or not camera.strip():
        return _error(400, "missing site or camera")
    site, camera = site.strip(), camera.strip()
    try:
        _camera_cfg(_site_cfg(load_config(), site), site, camera)
    except Rejected as exc:
        return _error(exc.status, exc.reason)
    data = await frame.read()
    if not data:
        return _error(400, "empty frame")
    if len(data) > MAX_FRAME_BYTES:
        return _error(400, f"frame too large: {len(data)} bytes, max {MAX_FRAME_BYTES}")
    score = await asyncio.to_thread(sharpness_of, data)
    if score is None:
        return _error(400, "frame is not a readable JPEG")
    now = _now()
    key = (site, camera)
    with _LOCK:
        _FRAMES[key] = FrameSlot(jpeg=data, ts=now, sharpness=score)
        watching = _is_watched(key, now)
        align = _align_state(key, now)
    return {"watching": watching, "sharpness": score, "align": align}


@router.get("/api/camera/frame")
async def latest_frame(site: str = "", camera: str = "") -> Any:
    """The mirilla (and the bot's /foto): the latest copy as a JPEG, with its sharpness and age in
    headers. The poll itself marks the camera watched for WATCHED_TTL_S, so captura keeps pushing."""
    if not site.strip() or not camera.strip():
        return _error(400, "missing site or camera")
    key = (site.strip(), camera.strip())
    now = _now()
    with _LOCK:
        _WATCHED[key] = now + timedelta(seconds=WATCHED_TTL_S)
        slot = _FRAMES.get(key)
    if slot is None:
        return _error(404, "sin copia fresca: nadie esta capturando ahora")
    age = (now - slot.ts).total_seconds()
    return Response(
        content=slot.jpeg,
        media_type="image/jpeg",
        headers={
            "X-Sharpness": str(slot.sharpness),
            "X-Frame-Age-S": f"{age:.1f}",
            "Cache-Control": "no-store",
        },
    )


@router.get("/api/camera/state")
async def camera_state(site: str = "") -> Any:
    """The site's cameras (role, source, kitchen_edge when calibrated, last-frame age, watching,
    align state) plus its funciones and the capture/rope knobs the pages need. Absent-when-off:
    no copy = no age key, no calibration = no kitchen_edge key."""
    if not site.strip():
        return _error(400, "missing site")
    site = site.strip()
    cfg = load_config()
    site_cfg = cfg.sites.get(site)
    if site_cfg is None:
        return _error(404, f"unknown site: {site}")
    now = _now()
    cameras: dict[str, Any] = {}
    with _LOCK:
        for name, cam in site_cfg.cameras.items():
            key = (site, name)
            entry: dict[str, Any] = {
                "source": cam.source,
                "role": cam.role,
                "watching": _is_watched(key, now),
                "align": _align_state(key, now),
            }
            if cam.kitchen_edge is not None:
                entry["kitchen_edge"] = cam.kitchen_edge
            slot = _FRAMES.get(key)
            if slot is not None:
                entry["last_frame_age_s"] = round((now - slot.ts).total_seconds(), 1)
                entry["sharpness"] = slot.sharpness
            cameras[name] = entry
    return {
        "site": site,
        "funciones": site_cfg.funciones.model_dump(),
        "capture": cfg.capture.model_dump(),
        "video_align": cfg.video_align.model_dump(),
        "frame_poll_s": FRAME_POLL_S,
        "now": now.isoformat(),
        "cameras": cameras,
    }


# --- the alignment mode (T-C6): the rope is mandatory, capped, never "never" ------------

@router.post("/api/camera/align/start")
async def align_start(body: AlignStartRequest, session: Session = Depends(require_session)) -> Any:
    try:
        _own_site_or_403(session, body.site)
        cfg = load_config()
        _camera_cfg(_site_cfg(cfg, body.site), body.site, body.camera)
    except Rejected as exc:
        return _error(exc.status, exc.reason)
    asked = body.minutes * 60 if body.minutes is not None else cfg.video_align.default_s
    seconds = min(asked, cfg.video_align.max_s)
    now = _now()
    until = now + timedelta(seconds=seconds)
    with _LOCK:
        _ALIGN[(body.site, body.camera)] = until
    log.info("align start site=%s camera=%s seconds=%d user=%s", body.site, body.camera, seconds, session.user)
    out: dict[str, Any] = {"active": True, "until": until.isoformat(), "seconds": seconds}
    if seconds != asked:
        out["capped"] = True
    return out


@router.post("/api/camera/align/stop")
async def align_stop(body: AlignStopRequest, session: Session = Depends(require_session)) -> Any:
    try:
        _own_site_or_403(session, body.site)
    except Rejected as exc:
        return _error(exc.status, exc.reason)
    with _LOCK:
        was = _ALIGN.pop((body.site, body.camera), None)
    log.info("align stop site=%s camera=%s was_active=%s user=%s", body.site, body.camera, was is not None, session.user)
    return {"active": False}


# --- the calibration write: the ONLY config write before FASE 3 (SPEC 1.3, HQ call 3) ---

_CONFIG_WRITE_LOCK = asyncio.Lock()


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_content(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def _block_end(lines: list[str], start: int, indent: int) -> int:
    """Index of the first content line after `start` at indent <= `indent`: where that block ends."""
    i = start + 1
    while i < len(lines):
        if _is_content(lines[i]) and _indent(lines[i]) <= indent:
            break
        i += 1
    return i


def _first_content_indent(lines: list[str], start: int, end: int) -> int | None:
    for i in range(start, end):
        if _is_content(lines[i]):
            return _indent(lines[i])
    return None


def _find_key(lines: list[str], start: int, end: int, indent: int, key: str) -> int | None:
    """Index of the line `key:` or `key: value` at exactly `indent` inside [start, end); None if absent."""
    for i in range(start, end):
        line = lines[i]
        if _is_content(line) and _indent(line) == indent:
            head = line.strip().split("#", 1)[0].strip()
            if head == f"{key}:" or head.startswith(f"{key}: "):
                return i
    return None


def write_kitchen_edge(config_path: Path, site: str, camera: str, edge: str) -> str:
    """Surgical one-line insertion (or replacement) of `kitchen_edge: <edge>` inside that camera's
    block. Never a yaml dump (pyyaml would destroy every comment): the file's text is edited line
    by line, validated by reloading it through the models BEFORE the swap, and written atomically
    (tmp + os.replace). Returns 'inserted' or 'replaced'."""
    text = config_path.read_text(encoding="utf-8")
    lines = text.split("\n")

    sites_i = _find_key(lines, 0, len(lines), 0, "sites")
    if sites_i is None:
        raise Rejected(500, "config.yaml: no top-level sites block")
    sites_end = _block_end(lines, sites_i, 0)
    site_indent = _first_content_indent(lines, sites_i + 1, sites_end)
    site_i = _find_key(lines, sites_i + 1, sites_end, site_indent or 0, site) if site_indent else None
    if site_i is None:
        raise Rejected(400, f"config.yaml: site {site!r} not found under sites")
    site_end = _block_end(lines, site_i, site_indent or 0)
    key_indent = _first_content_indent(lines, site_i + 1, site_end)
    cameras_i = _find_key(lines, site_i + 1, site_end, key_indent or 0, "cameras") if key_indent else None
    if cameras_i is None:
        raise Rejected(400, f"config.yaml: site {site!r} has no cameras block")
    cameras_end = _block_end(lines, cameras_i, key_indent or 0)
    cam_indent = _first_content_indent(lines, cameras_i + 1, cameras_end)
    cam_i = _find_key(lines, cameras_i + 1, cameras_end, cam_indent or 0, camera) if cam_indent else None
    if cam_i is None:
        raise Rejected(400, f"config.yaml: camera {camera!r} not found under sites.{site}.cameras")
    cam_end = _block_end(lines, cam_i, cam_indent or 0)
    field_indent = _first_content_indent(lines, cam_i + 1, cam_end)
    if field_indent is None:
        raise Rejected(400, f"config.yaml: camera {camera!r} block is empty")

    new_line = " " * field_indent + f"kitchen_edge: {edge}"
    existing = _find_key(lines, cam_i + 1, cam_end, field_indent, "kitchen_edge")
    if existing is not None:
        lines[existing] = new_line
        action = "replaced"
    else:
        # After the LAST key line of the block: comments that follow stay where they are.
        last_key = max(i for i in range(cam_i + 1, cam_end) if _is_content(lines[i]) and _indent(lines[i]) == field_indent)
        lines.insert(last_key + 1, new_line)
        action = "inserted"

    new_text = "\n".join(lines)
    tmp = config_path.with_name(config_path.name + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    try:
        load_config(tmp)                      # the models are the gate: an invalid result never lands
    except ValueError as exc:
        tmp.unlink(missing_ok=True)
        raise Rejected(400, f"calibration rejected by the config models: {exc}") from None
    os.replace(tmp, config_path)
    return action


@router.post("/api/camera/calibration")
async def calibration(body: CalibrationRequest, session: Session = Depends(require_session)) -> Any:
    try:
        _own_site_or_403(session, body.site)
        cfg = load_config()
        cam = _camera_cfg(_site_cfg(cfg, body.site), body.site, body.camera)
        if cam.role != "both":
            raise Rejected(400, f"la camara {body.camera!r} tiene rol {cam.role!r}: el borde-cocina solo aplica a rol 'both'")
        async with _CONFIG_WRITE_LOCK:
            action = await asyncio.to_thread(write_kitchen_edge, CONFIG_PATH, body.site, body.camera, body.kitchen_edge)
    except Rejected as exc:
        return _error(exc.status, exc.reason)
    # Read back through the models: what the file says now is what we answer.
    saved = load_config().sites[body.site].cameras[body.camera].kitchen_edge
    log.info("calibration site=%s camera=%s kitchen_edge=%s action=%s user=%s", body.site, body.camera, saved, action, session.user)
    return {"site": body.site, "camera": body.camera, "kitchen_edge": saved, "action": action}


# --- the burst upload (T-C1): server-side sharpness selection, one job, capture facts -----

def _register_burst(
    req: UploadRequest,
    rel_path: str,
    data: bytes,
    capture: dict[str, Any],
    job_kind: str,
    priority: int,
    prompt_version: str,
    cfg: Config,
) -> dict[str, Any]:
    """The upload transaction, the burst edition: identical discipline to api.main._register_plate
    (site check, duplicate lookup, plates + jobs insert, file write, ONE transaction) plus the
    capture JSONB written once here and never touched by the worker (SPEC 1.2 migration 3)."""
    from api.main import _write_photo  # lazy: api.main includes this router, so no import cycle at load

    with q.connect() as conn, conn.transaction():
        site_row = conn.execute(q.SELECT_SITE, {"site": req.site}).fetchone()
        if site_row is None or not site_row["active"]:
            return {"error": f"unknown site: {req.site}"}

        existing = conn.execute(q.SELECT_PLATE_ID_BY_PHOTO_PATH, {"photo_path": rel_path}).fetchone()
        if existing is not None:
            return {"plate_id": existing["id"], "duplicate": True}

        menu_version = int(conn.execute(q.MAX_MENU_VERSION).fetchone()["v"])
        inserted = conn.execute(
            q.INSERT_PLATE_WITH_CAPTURE,
            {
                "site": req.site,
                "camera": req.camera,
                "record_type": req.record_type,
                "photo_path": rel_path,
                "model": cfg.active_model,
                "prompt_version": prompt_version,
                "menu_version": menu_version,
                "capture": Jsonb(capture),
            },
        ).fetchone()
        if inserted is None:
            existing = conn.execute(q.SELECT_PLATE_ID_BY_PHOTO_PATH, {"photo_path": rel_path}).fetchone()
            return {"plate_id": existing["id"], "duplicate": True}

        plate_id = int(inserted["id"])
        conn.execute(q.INSERT_JOB, {"plate_id": plate_id, "kind": job_kind, "priority": priority})
        _write_photo(rel_path, data)
    return {"plate_id": plate_id}


@router.post("/api/upload_burst")
async def upload_burst(
    photos: list[UploadFile] = File(default=[]),
    site: str | None = Form(None),
    camera: str | None = Form(None),
    record_type: str | None = Form(None),
    direction_dudosa: str | None = Form(None),
) -> Any:
    """1..capture.burst_frames JPEGs -> the sharpest becomes the row (losers die in RAM), with
    capture.sharpness stamped, and ONE job: outgoing -> presentation at express priority (when the
    site's presentacion function is on), return -> analyze. A single-frame burst is the curl road."""
    from api.main import _photo_rel_path  # lazy: see _register_burst

    if not photos:
        return _error(400, "missing photos")
    if site is None or not site.strip():
        return _error(400, "missing site")
    try:
        req = UploadRequest(site=site, camera=camera, record_type=record_type or "return")
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        return _error(400, f"malformed upload: {where}: {first.get('msg', 'invalid')}")

    cfg = load_config()
    try:
        site_cfg = _site_cfg(cfg, req.site)
        if req.camera is not None:
            _camera_cfg(site_cfg, req.site, req.camera)
        if len(photos) > cfg.capture.burst_frames:
            raise Rejected(400, f"burst too large: {len(photos)} frames, max {cfg.capture.burst_frames}")
        # The server-side function gate: the browser gatekeeper already discards; a stray upload is
        # refused here, never stored, never billed.
        if req.record_type == "outgoing" and not site_cfg.funciones.presentacion:
            raise Rejected(400, f"la funcion presentacion esta apagada en el sitio {req.site}: el plato se descarta")
        if req.record_type == "return" and not site_cfg.funciones.merma:
            raise Rejected(400, f"la funcion merma esta apagada en el sitio {req.site}: el plato se descarta")
    except Rejected as exc:
        return _error(exc.status, exc.reason)

    frames = [await p.read() for p in photos]
    if any(not f for f in frames):
        return _error(400, "empty frame in burst")
    scores: list[float | None] = await asyncio.to_thread(lambda: [sharpness_of(f) for f in frames])
    readable = [i for i, s in enumerate(scores) if s is not None]
    if not readable:
        return _error(400, "no frame in the burst is a readable JPEG")
    best = max(readable, key=lambda i: scores[i])   # first index wins a tie
    data, score = frames[best], scores[best]

    capture: dict[str, Any] = {"sharpness": score}
    if _truthy(direction_dudosa):
        capture["direction_dudosa"] = True
    if req.record_type == "outgoing":
        job_kind, priority, prompt_version = JOB_KIND_PRESENTATION, JOB_PRIORITY_EXPRESS, PRESENTATION_PROMPT_VERSION
    else:
        job_kind, priority, prompt_version = JOB_KIND_ANALYZE, JOB_PRIORITY_RETURN, PROMPT_VERSION

    rel_path = _photo_rel_path(req.site, data)
    result = await asyncio.to_thread(
        _register_burst, req, rel_path, data, capture, job_kind, priority, prompt_version, cfg
    )
    if "error" in result:
        return _error(400, result["error"])

    log.info(
        "upload_burst site=%s record_type=%s camera=%s frames=%d scores=%s chosen=%d sharpness=%s "
        "direction_dudosa=%s kind=%s priority=%d plate_id=%s duplicate=%s photo=%s",
        req.site, req.record_type, req.camera, len(frames), scores, best, score,
        capture.get("direction_dudosa", False), job_kind, priority, result["plate_id"],
        result.get("duplicate", False), rel_path,
    )
    out: dict[str, Any] = {"plate_id": result["plate_id"], "sharpness": score, "frames": len(frames), "chosen": best, "kind": job_kind}
    if result.get("duplicate"):
        out["duplicate"] = True
    return out
