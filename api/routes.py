"""LPQ_VISION api/routes.py: every Fase-2 /api/* endpoint beyond the Fase-1 three and auth.

Section 2.6 (this file's birth) is the CAPTURE LANE: /api/upload_burst, the ephemeral frame
store with /api/camera/frame (POST + GET) and /api/camera/state, the alignment mode
(/api/camera/align/start|stop) and the calibration write (/api/camera/calibration).
Section 2.7 adds the HUMAN LANE: /api/review (tabs, filters, pagination, verify, not_plate,
reopen, bulk; discard since 3.13, into the papelera; since 3.14 the list also carries each listed
site's confianza_minima, so the page paints the rows under it), /api/stats, /api/gallery,
/api/photo/{id} and /api/dishes.

The gafete wall (Section 3.13, SPEC 1.3's enforcement matrix): the capture lane's writes
(upload_burst, the frame push) need THAT site's gafete (a machine) or, since Section 3.13.2, the
pass of the phone the admin accepted on THAT camera; one camera, one source: a Pi camera takes the
gafete only, a phone camera its phone's pass only. Its reads (the frame, the state) need a session
of the site OR that site's gafete (the bot presents the key it reads from the database). The gafete
rides only the X-Device-Key header, never a URL or a cookie. Behind the session: align/start,
align/stop, calibration, and the whole human lane, every list FORCED to a site account's own site
(api.auth.scoped_site).

Section 3.13.2, the phone's door: a phone opens its camera's link (/captura#CODE: the code after
the # never reaches a server, so no log ever holds it), /api/captura/yo tells the page what to show,
/api/captura/pedir leaves its name waiting for the admin's Aceptar (the bitácora row telefono.pide:
the bot posts it in the site's group), and from the Aceptar on the phone's pass opens the lane for
that camera while it is a phone camera. The frame store remembers who sent each copy (the gafete or
which phone), so the mostrador's light names the real sender.

Section 3.13 also gives the lane its measurements and its per-site knobs: every pushed copy is
scored for sharpness AND mean brightness (luma) on the same small gray copy; a camera whose copies
stay under alerts.black_luma_max carries black_for_s (counted to its last copy) in the state, next
to the api's own uptime (up_s), so the bot can judge a covered camera and a silent one after a
restart; a burst whose every frame is black is refused at the door (never stored, never billed),
and a black frame never wins a mixed one; the guard knobs, the burst cap, the rope's default and
the black floor are THAT site's values (the cascade). The plate's photo is ONE light file (owner
ruling 3.10.1): the sharpest frame shrunk to PLATE_LIGHT_MAX_SIDE by the server's own image math.
The alignment mode and the calibration leave bitácora rows, and the calibration goes through the
ONE writer (api.config_writer).

The frame store is RAM only (SPEC 1.5): copies overwrite, nothing reaches disk, DB or logs; a
restart blanks it and nothing breaks. It and the RAM sessions depend on the api being ONE
uvicorn process, which it is and must remain this era.

opencv-python-headless is imported HERE and nowhere under brain/: the api owns the one
thermometer of the whole system (Laplacian variance and mean luma on a gray copy downscaled to
SHARPNESS_MAX_SIDE, so a phone burst frame and a small mirilla copy read on the same scale) and
the plate photo's one shrink. Every decode goes through _decode: JPEG or PNG only, sized from the
file's own header first (MAX_DECODE_PIXELS), and never an exception.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from math import ceil
from typing import Any

import cv2
import numpy as np
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse, Response
from psycopg import errors as pg_errors
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from api import config_writer as cw
from api.admin import alog
from api.auth import (
    Session,
    forget_fuera,
    fuera_of,
    guard_capture,
    guard_session_or_key,
    new_pase,
    pase_hash,
    pase_token,
    require_session,
    scoped_site,
    set_pase,
)
from brain.capture.backends import PHOTO_ROOT
from brain.db import queries as q
from brain.db.migrations import REVIEW_NOT_PLATE, REVIEW_STATUSES, REVIEW_UNREVIEWED, REVIEW_VERIFIED
from brain.validator import knobs
from brain.validator.models import (
    PRESENTATION_PROMPT_VERSION,
    PROMPT_VERSION,
    AlignStartRequest,
    AlignStopRequest,
    BulkReviewRequest,
    CalibrationRequest,
    CameraConfig,
    Config,
    PedirRequest,
    SiteConfig,
    UploadRequest,
    VerifyRequest,
    enlace_codigo,
    load_config,
    nombre_telefono,
)

log = logging.getLogger("lpq.api.routes")

# --- named constants (SPEC section 4) -------------------------------------------------
JOB_KIND_ANALYZE = "analyze"
JOB_KIND_PRESENTATION = "presentation"
JOB_PRIORITY_RETURN = 100
JOB_PRIORITY_EXPRESS = 10          # the claim's ORDER BY priority makes any free worker serve it first
WATCHED_TTL_S = 15                 # how long one GET of the frame keeps a camera "watched"
SHARPNESS_MAX_SIDE = 640           # px: every scored frame is downscaled to this longest side first
MAX_FRAME_BYTES = 8 * 1024 * 1024  # a mirilla copy larger than this is refused
PAGE_SIZE = 50                     # review and gallery pagination
UNKNOWN_DISH = "desconocido"       # the contract's own valid answer (same string as brain.validator.repair)
PLATE_LIGHT_MAX_SIDE = 1568        # px: the plate's ONE stored file (Anthropic scales any longer side down to about this anyway)
PLATE_LIGHT_QUALITY = 85           # its JPEG quality (the reference photos' light copy uses the same)
MAX_DECODE_PIXELS = 50_000_000     # a 48 MP phone photo decodes; a bigger file (or a decompression bomb) is never decoded
BLACK_GAP_PUSHES = 3               # a black streak survives a pause of at most this many pushes; a longer one starts it again
FRESH_PUSHES = 3                   # a copy younger than this many pushes = something is sending that camera now
PENDIENTES_MAX = 3                 # phones that may wait for the admin's Aceptar on one camera at once
PEDIR_AVISO_S = 600                # a phone asking again for a camera it asked for within this time is not posted again
ENLACE_HEADER = "X-Enlace"         # the link's code on /api/captura/yo: never in a URL the server sees (or logs)
CONFIANZA_RANK = {"alta": 3, "media": 2, "baja": 1}   # the same ranks STATS_LOW_CONFIDENCE uses
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}   # the frame headers (not DHT, JPG or DAC)

# The api's boot when the lifespan did not run (a test client): app.state.boot_at is the real one.
_IMPORTED_AT = datetime.now(timezone.utc)

# The review tabs the page shows -> the review_status each one lists (SPEC 1.2 contract).
REVIEW_TABS: dict[str, str] = {
    "pendientes": REVIEW_UNREVIEWED,
    "verificados": REVIEW_VERIFIED,
    "no_platos": REVIEW_NOT_PLATE,
}

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


# --- the one decoder, and the one thermometer: sharpness and brightness on the same small copy ---

def image_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) as the file's own header declares them, read without decoding: a PNG's IHDR or
    a JPEG's frame header. None for any other format, or for a header that does not parse."""
    if data.startswith(_PNG_MAGIC):
        if len(data) < 24 or data[12:16] != b"IHDR":
            return None
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if not data.startswith(b"\xff\xd8"):
        return None
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:                                   # a fill byte before the marker
            i += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:         # markers without a length
            i += 2
            continue
        if marker in (0xD9, 0xDA):                           # the image ends or its data starts: no frame header
            return None
        if marker in _JPEG_SOF:
            if i + 9 > len(data):
                return None
            return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
        length = int.from_bytes(data[i + 2:i + 4], "big")
        if length < 2:
            return None
        i += 2 + length
    return None


def _decode(data: bytes, flags: int) -> np.ndarray | None:
    """The api's one image decoder: JPEG or PNG only, at most MAX_DECODE_PIXELS by the file's own
    header, and never an exception. A broken or hostile file reads as "not an image", so it can
    neither crash a request nor fill the ONE process's memory."""
    size = image_size(data)
    if size is None or size[0] * size[1] > MAX_DECODE_PIXELS:
        return None
    try:
        return cv2.imdecode(np.frombuffer(data, dtype=np.uint8), flags)
    except cv2.error:
        return None


def measure(jpeg: bytes) -> tuple[float, float] | None:
    """(sharpness, luma) of one frame, both read from the SAME gray copy downscaled to
    SHARPNESS_MAX_SIDE: the Laplacian variance, and the mean brightness from 0 to 255 (Section 3.13).
    None when the bytes are not an image."""
    img = _decode(jpeg, cv2.IMREAD_GRAYSCALE)
    if img is None:
        return None
    h, w = img.shape[:2]
    longest = max(h, w)
    if longest > SHARPNESS_MAX_SIDE:
        scale = SHARPNESS_MAX_SIDE / longest
        img = cv2.resize(img, (max(1, round(w * scale)), max(1, round(h * scale))), interpolation=cv2.INTER_AREA)
    return round(float(cv2.Laplacian(img, cv2.CV_64F).var()), 2), round(float(img.mean()), 1)


def light_plate(jpeg: bytes) -> bytes:
    """The plate's ONE stored file (owner ruling 3.10.1): a frame longer than PLATE_LIGHT_MAX_SIDE is
    shrunk to it by the server's own image math (INTER_AREA, JPEG at PLATE_LIGHT_QUALITY). A frame that
    is already that small (read from its header, no decoding), bytes that are not an image, and a file
    over MAX_DECODE_PIXELS are kept exactly as they came (the worker's failure road still meets them).
    Deterministic: the same bytes always give the same file."""
    size = image_size(jpeg)
    if size is None or max(size) <= PLATE_LIGHT_MAX_SIDE:
        return jpeg
    img = _decode(jpeg, cv2.IMREAD_COLOR)
    if img is None:
        return jpeg
    h, w = img.shape[:2]
    longest = max(h, w)
    if longest <= PLATE_LIGHT_MAX_SIDE:
        return jpeg
    scale = PLATE_LIGHT_MAX_SIDE / longest
    small = cv2.resize(img, (max(1, round(w * scale)), max(1, round(h * scale))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, PLATE_LIGHT_QUALITY])
    return buf.tobytes() if ok else jpeg


def page_knobs(cfg: Config, site: str) -> dict[str, Any]:
    """The capture page's knobs with THIS site's own values (the cascade): the three guard knobs per
    site, plus the one global push cadence. The state and every push reply carry exactly this."""
    out = cfg.capture.model_dump()
    for name in ("diff_threshold", "settle_frames", "burst_frames"):
        out[name] = knobs.effective(cfg, "capture." + name, site)
    return out


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


def _source_or_403(by: int | None, cam: CameraConfig, camera: str) -> None:
    """One camera, one source (Section 3.13.2): a phone camera takes copies only from its own phone (its pass, by = its
    id), a Pi camera only from a machine (the gafete, by None). A camera that went back to its Pi refuses the phone,
    and one a phone took refuses its Pi until «Relevo a Pi», so no plate is ever captured twice."""
    if by is not None and cam.source != "phone":
        raise Rejected(403, f"{camera} es de Pi: este teléfono ya no la captura")
    if by is None and cam.source == "phone":
        raise Rejected(403, f"{camera} es de teléfono: su Pi vuelve a capturarla con «Relevo a Pi»")


# --- the ephemeral frame store (SPEC 1.5) ---------------------------------------------

@dataclass
class FrameSlot:
    jpeg: bytes
    ts: datetime
    sharpness: float | None
    luma: float | None = None             # mean brightness of this copy (0 to 255)
    black_since: datetime | None = None   # when the current black streak began; None = not black
    by: int | None = None                 # who sent it: None = a machine's gafete, else the phone's id (3.13.2)


# All three dicts are keyed by (site, camera) and guarded by ONE lock; scoring runs in threads.
_FRAMES: dict[tuple[str, str], FrameSlot] = {}       # the latest copy; the next one overwrites it
_WATCHED: dict[tuple[str, str], datetime] = {}       # until when someone is looking at that camera
_ALIGN: dict[tuple[str, str], datetime] = {}         # until when that camera's capture is PAUSED
_LOCK = threading.Lock()
# The group's posts of phone requests (Section 3.13.2), under the same lock: phone id -> (site, camera) -> when its
# request was posted. RAM only: a restart posts again, which is fine.
_AVISOS: dict[int, dict[tuple[str, str], float]] = {}


def _aviso_nuevo(phone_id: int, site: str, camera: str) -> bool:
    """Is this request news for the site's group: the first time this phone asks for this camera within
    PEDIR_AVISO_S. A phone flipping between two links is posted once per camera, never once per flip; a request for
    another camera (another site's included) is always posted."""
    now = time.monotonic()
    with _LOCK:
        for pid in list(_AVISOS):
            kept = {k: t for k, t in _AVISOS[pid].items() if now - t < PEDIR_AVISO_S}
            if kept:
                _AVISOS[pid] = kept
            else:
                del _AVISOS[pid]
        return (site, camera) not in _AVISOS.get(phone_id, {})


def _aviso_dado(phone_id: int, site: str, camera: str) -> None:
    with _LOCK:
        _AVISOS.setdefault(phone_id, {})[(site, camera)] = time.monotonic()


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
    request: Request,
    frame: UploadFile | None = File(None),
    site: str | None = Form(None),
    camera: str | None = Form(None),
) -> Any:
    """From captura.js: store the latest copy in RAM, score it, and tell the page whether anyone
    is watching (keep pushing) and whether its capture is paused by the alignment mode. The page
    knobs ride this reply (Section 3.12): the guard knobs (THIS site's values since 3.13), the push
    cadence, the site's two functions and the camera's source, so the page obeys a change mid-flight
    with zero extra requests. THAT site's gafete or the pass of the phone on THAT camera (the wall,
    3.13.2); the slot remembers which one sent it. A copy under the site's black floor starts or
    extends the camera's black streak; a brighter one ends it, and so does a pause longer than
    BLACK_GAP_PUSHES."""
    if frame is None:
        return _error(400, "missing frame")
    if not site or not site.strip() or not camera or not camera.strip():
        return _error(400, "missing site or camera")
    site, camera = site.strip(), camera.strip()
    by = await guard_capture(request, site, camera)
    try:
        cfg = load_config()
        site_cfg = _site_cfg(cfg, site)
        cam = _camera_cfg(site_cfg, site, camera)
        _source_or_403(by, cam, camera)
    except Rejected as exc:
        return _error(exc.status, exc.reason)
    data = await frame.read()
    if not data:
        return _error(400, "empty frame")
    if len(data) > MAX_FRAME_BYTES:
        return _error(400, f"frame too large: {len(data)} bytes, max {MAX_FRAME_BYTES}")
    measured = await asyncio.to_thread(measure, data)
    if measured is None:
        return _error(400, "frame is not a readable JPEG")
    score, luma = measured
    black_max = knobs.effective(cfg, "alerts.black_luma_max", site)
    gap = timedelta(seconds=BLACK_GAP_PUSHES * cfg.capture.frame_poll_s)
    now = _now()
    key = (site, camera)
    with _LOCK:
        previous = _FRAMES.get(key)
        since = None
        if luma < black_max:
            streak = previous is not None and previous.black_since is not None and now - previous.ts <= gap
            since = previous.black_since if streak else now
        _FRAMES[key] = FrameSlot(jpeg=data, ts=now, sharpness=score, luma=luma, black_since=since, by=by)
        watching = _is_watched(key, now)
        align = _align_state(key, now)
    return {
        "watching": watching,
        "sharpness": score,
        "align": align,
        "capture": page_knobs(cfg, site),
        "frame_poll_s": cfg.capture.frame_poll_s,
        "funciones": site_cfg.funciones.model_dump(),
        "source": cam.source,
    }


@router.get("/api/camera/frame")
async def latest_frame(request: Request, site: str = "", camera: str = "") -> Any:
    """The mirilla (and the bot's /foto): the latest copy as a JPEG, with its sharpness and age in
    headers. The poll itself marks the camera watched for WATCHED_TTL_S, so captura keeps pushing.
    A session of the site or that site's gafete (the wall)."""
    if not site.strip() or not camera.strip():
        return _error(400, "missing site or camera")
    await guard_session_or_key(request, site.strip())
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
async def camera_state(request: Request, site: str = "") -> Any:
    """The site's cameras (role, source, kitchen_edge when calibrated, last-frame age, sharpness and
    luma, black_for_s while its copies are black, watching, align state) plus its funciones, the
    capture/rope knobs the pages need (THIS site's values) and the api's own uptime (up_s: the bot's
    amnesia rule). Absent-when-off: no copy = no age, sharpness or luma key; not black = no
    black_for_s; no calibration = no kitchen_edge. A session of the site or that site's gafete."""
    if not site.strip():
        return _error(400, "missing site")
    site = site.strip()
    await guard_session_or_key(request, site)
    cfg = load_config()
    site_cfg = cfg.sites.get(site)
    if site_cfg is None:
        return _error(404, f"unknown site: {site}")
    now = _now()
    boot_at = getattr(request.app.state, "boot_at", None) or _IMPORTED_AT
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
                if slot.luma is not None:
                    entry["luma"] = slot.luma
                if slot.black_since is not None:
                    entry["black_for_s"] = round((slot.ts - slot.black_since).total_seconds(), 1)   # to the last copy
            cameras[name] = entry
    return {
        "site": site,
        "funciones": site_cfg.funciones.model_dump(),
        "capture": page_knobs(cfg, site),
        "video_align": {
            "default_s": knobs.effective(cfg, "video_align.default_s", site),
            "max_s": cfg.video_align.max_s,
        },
        "sharpness_min": knobs.effective(cfg, "alerts.sharpness_min", site),
        "frame_poll_s": cfg.capture.frame_poll_s,      # the knob (Perillas): the pages' one cadence
        "up_s": round((now - boot_at).total_seconds(), 1),
        "now": now.isoformat(),
        "cameras": cameras,
    }


# --- the alignment mode (T-C6): the rope is mandatory, capped, never "never" ------------

@router.post("/api/camera/align/start")
async def align_start(body: AlignStartRequest, session: Session = Depends(require_session)) -> Any:
    """The rope: that camera's capture PAUSES for the asked minutes, or THIS site's
    video_align.default_s, never beyond video_align.max_s. Its bitácora row is written first, so
    a rope that could not leave its row never starts."""
    try:
        _own_site_or_403(session, body.site)
        cfg = load_config()
        _camera_cfg(_site_cfg(cfg, body.site), body.site, body.camera)
    except Rejected as exc:
        return _error(exc.status, exc.reason)
    default_s = knobs.effective(cfg, "video_align.default_s", body.site)
    asked = body.minutes * 60 if body.minutes is not None else default_s
    seconds = min(asked, cfg.video_align.max_s)
    detail: dict[str, Any] = {"site": body.site, "camera": body.camera, "seconds": seconds}
    if seconds != asked:
        detail["capped"] = True
    await alog(session, "align.start", detail)
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
    """The rope cut early: the capture resumes at once. Its bitácora row follows the stop (a stop is
    the safe direction: it never waits on the database); a camera the site does not have, with no
    rope to cut, leaves no row."""
    try:
        _own_site_or_403(session, body.site)
    except Rejected as exc:
        return _error(exc.status, exc.reason)
    now = _now()
    key = (body.site, body.camera)
    with _LOCK:
        was_active = bool(_align_state(key, now)["active"])
        _ALIGN.pop(key, None)
    site_cfg = load_config().sites.get(body.site)
    if was_active or (site_cfg is not None and body.camera in site_cfg.cameras):
        await alog(session, "align.stop", {"site": body.site, "camera": body.camera, "was_active": was_active})
    log.info("align stop site=%s camera=%s was_active=%s user=%s", body.site, body.camera, was_active, session.user)
    return {"active": False}


# --- the calibration: the kitchen edge, through the ONE writer -------------------------

@router.post("/api/camera/calibration")
async def calibration(body: CalibrationRequest, session: Session = Depends(require_session)) -> Any:
    """The kitchen edge of a role-'both' camera, written by api.config_writer (surgical, read back,
    validated by the models, atomic, under the process's one write lock) with its bitácora row (the
    old value when there was one). Answers what the file says now."""
    try:
        _own_site_or_403(session, body.site)
        cfg = load_config()
        cam = _camera_cfg(_site_cfg(cfg, body.site), body.site, body.camera)
        if cam.role != "both":
            raise Rejected(400, f"la camara {body.camera!r} tiene rol {cam.role!r}: el borde-cocina solo aplica a rol 'both'")
    except Rejected as exc:
        return _error(exc.status, exc.reason)
    before = cam.kitchen_edge
    try:
        result = await cw.write([cw.set_op(("sites", body.site, "cameras", body.camera, "kitchen_edge"), body.kitchen_edge)])
    except cw.WriterError as exc:
        return _error(400, str(exc))
    action = result.changes[0]["action"]          # inserted or replaced
    # Read back through the models: what the file says now is what we answer.
    saved = load_config().sites[body.site].cameras[body.camera].kitchen_edge
    detail: dict[str, Any] = {"site": body.site, "camera": body.camera, "despues": saved}
    if before is not None:
        detail["antes"] = before
    await alog(session, "calibration", detail)
    log.info("calibration site=%s camera=%s kitchen_edge=%s action=%s user=%s", body.site, body.camera, saved, action, session.user)
    return {"site": body.site, "camera": body.camera, "kitchen_edge": saved, "action": action}


# --- the phone's door (Section 3.13.2): a camera's link, a name, the admin's Aceptar ---------

def frame_ages() -> dict[tuple[str, str], float]:
    """Seconds since each camera's last copy, for every camera that sent one since this api started (the machine
    room's checklist, Section 3.15). A camera with no copy is simply absent."""
    now = _now()
    with _LOCK:
        return {key: (now - slot.ts).total_seconds() for key, slot in _FRAMES.items()}


def fresh_by(site: str, camera: str, cfg: Config) -> tuple[bool, int | None]:
    """Is something sending this camera now (a copy within FRESH_PUSHES pushes), and who sent the last copy:
    None = a machine's gafete, else the phone's id."""
    now = _now()
    with _LOCK:
        slot = _FRAMES.get((site, camera))
    if slot is None or (now - slot.ts).total_seconds() > FRESH_PUSHES * cfg.capture.frame_poll_s:
        return False, None
    return True, slot.by


def luz(site: str, camera: str, source: str, phone_id: int | None, cfg: Config) -> str:
    """The mostrador's light: "pi" when a Pi camera's gafete sends fresh copies, "telefono" when a phone camera's
    own phone does, "nada" otherwise (a copy left by someone else is not this camera's sender)."""
    fresh, by = fresh_by(site, camera, cfg)
    if not fresh:
        return "nada"
    if source == "pi":
        return "pi" if by is None else "nada"
    return "telefono" if phone_id is not None and by == phone_id else "nada"


def _stage(cfg: Config, site: str, camera: str) -> dict[str, Any] | None:
    """What the capture page needs for its camera: role, source and calibration, the site's two functions, the
    guard knobs (THIS site's values), the push cadence and the alignment state. None when the camera is gone."""
    site_cfg = cfg.sites.get(site)
    cam = site_cfg.cameras.get(camera) if site_cfg is not None else None
    if cam is None:
        return None
    with _LOCK:
        align = _align_state((site, camera), _now())
    out: dict[str, Any] = {
        "role": cam.role,
        "source": cam.source,
        "funciones": site_cfg.funciones.model_dump(),
        "capture": page_knobs(cfg, site),
        "frame_poll_s": cfg.capture.frame_poll_s,
        "align": align,
    }
    if cam.kitchen_edge is not None:
        out["kitchen_edge"] = cam.kitchen_edge
    return out


def _camera_of(cfg: Config, site: str | None, camera: str | None) -> CameraConfig | None:
    site_cfg = cfg.sites.get(site) if site else None
    return site_cfg.cameras.get(camera) if site_cfg is not None and camera else None


def _yo(pase: str | None, codigo: str | None, given: bool, cfg: Config) -> dict[str, Any]:
    """The capture page's one question: what this phone is now and what the link it opened offers.

    The LIVE link decides first (it is what the person just opened). A link of a deactivated site says so; the expired
    link of the camera the phone waits for says pendiente; another camera's expired link says vencido. Then the
    phone's own camera: any old, expired or unknown code, or none, takes
    it straight there (the pass counts, the code only matters for joining), even while a request for another camera
    waits (the request rides along in pide). Then its request alone, then a site of its that is deactivated, then
    what is wrong with the code. A phone is on a camera only while that camera is a phone camera of an active site.
    A deactivated site never hides what the phone has: its request stays in pide (activo false) and its own camera,
    while its site sleeps, is dormida (so a page waiting for one can say so and go on when it wakes)."""
    with q.connect() as conn:
        phone = conn.execute(q.SELECT_TELEFONO_BY_PASE, {"pase": pase}).fetchone() if pase else None
        link = conn.execute(q.SELECT_ENLACE_BY_CODIGO, {"codigo": codigo}).fetchone() if codigo else None
        holder, waiting = None, 0
        if link is not None:
            holder = conn.execute(q.SELECT_TELEFONO_DE_CAMARA, {"site": link["site"], "camera": link["camera"]}).fetchone()
            waiting = int(conn.execute(q.COUNT_PEDIDOS_DE_CAMARA, {
                "site": link["site"], "camera": link["camera"], "id": phone["id"] if phone else -1,
            }).fetchone()["n"])
    own = _camera_of(cfg, phone["site"], phone["camera"]) if phone else None
    own_ok = bool(phone and own is not None and own.source == "phone")
    bound = bool(own_ok and phone["site_active"])
    asked = bool(phone and _camera_of(cfg, phone["pide_site"], phone["pide_camera"]))
    pending = bool(asked and phone["pide_active"])
    asleep = phone["site"] if phone and phone["site"] and not phone["site_active"] else (
        phone["pide_site"] if phone and phone["pide_site"] and not phone["pide_active"] else None)
    out: dict[str, Any] = {
        "paso": "sin_enlace",
        "nombre": phone["nombre"] if phone else None,
        "camara": {"site": phone["site"], "camera": phone["camera"]} if bound else None,
        "dormida": {"site": phone["site"], "camera": phone["camera"]} if own_ok and not bound else None,
        "pide": ({"site": phone["pide_site"], "camera": phone["pide_camera"], "activo": bool(phone["pide_active"])}
                 if asked else None),
        "enlace": None,
        "inactivo": None,
        "aviso": fuera_of(pase),
        "stage": None,
        "_known": phone is not None,
    }
    cam = _camera_of(cfg, link["site"], link["camera"]) if link is not None else None
    target = (link["site"], link["camera"]) if link is not None else None
    if cam is not None and link["active"] and not link["vencido"]:
        out["enlace"] = {"site": target[0], "camera": target[1], "pi": cam.source == "pi"}
        fresh, by = fresh_by(target[0], target[1], cfg)
        if bound and (phone["site"], phone["camera"]) == target:
            out["paso"] = "capturar"
        elif pending and (phone["pide_site"], phone["pide_camera"]) == target:
            out["paso"] = "pendiente"
        elif holder is not None:
            out["paso"] = "llena"
        elif cam.source == "pi" and fresh and by is None:
            out["paso"] = "pi_viva"
        elif waiting >= PENDIENTES_MAX:
            out["paso"] = "pedidos_llenos"
        else:
            out["paso"] = "pedir"
    elif cam is not None and not link["active"]:
        out["paso"], out["inactivo"] = "sitio_inactivo", target[0]
    elif cam is not None and pending and (phone["pide_site"], phone["pide_camera"]) == target:
        out["paso"] = "pendiente"                  # its own request's link expired meanwhile: the request still waits
    elif cam is not None and not (bound and (phone["site"], phone["camera"]) == target):
        out["paso"] = "vencido"
    elif bound:
        out["paso"] = "capturar"
    elif pending:
        out["paso"] = "pendiente"
    elif asleep is not None:
        out["paso"], out["inactivo"] = "sitio_inactivo", asleep
    elif given:
        out["paso"] = "desconocido"
    if bound:
        out["stage"] = _stage(cfg, out["camara"]["site"], out["camara"]["camera"])
    return out


@router.get("/api/captura/yo")
async def captura_yo(request: Request, response: Response) -> Any:
    """/captura's one question (Section 3.13.2), from the phone's pass and the link it opened: paso = capturar |
    pendiente | pedir | llena | pi_viva | pedidos_llenos | vencido | desconocido | sitio_inactivo | sin_enlace, with
    the phone's name, its camera (or dormida, while its site is deactivated), its request (activo false while that
    site is deactivated), what the link offers, a deactivated site, the reason it lost its
    camera (aviso, for a day) and, when it has a camera, that camera's stage. The link's code rides the X-Enlace
    header (the page reads it from the address's #, which never reaches a server or its logs). Writes nothing in the
    database: a browser with no pass gets a fresh one here (so its first request and any retry share one pass), and
    a known pass gets its 400 days again."""
    raw = request.headers.get(ENLACE_HEADER) or ""
    token = pase_token(request)
    out = await asyncio.to_thread(_yo, pase_hash(token) if token else None, enlace_codigo(raw), bool(raw.strip()),
                                  load_config())
    known = out.pop("_known")
    if token is None:
        set_pase(response, new_pase())
    elif known:
        set_pase(response, token)
    return out


def _pedir(pase: str, codigo: str, nombre: str, cfg: Config) -> dict[str, Any]:
    """The request, in one transaction under the camera's lock (LOCK_CAMARA, the one Aceptar and Quitar la cámara
    take): the link must be live, the camera free (no other phone, not a Pi that is sending) and fewer than
    PENDIENTES_MAX others waiting. The same request again only refreshes the name (no new row for the bot). A new
    one leaves the bitácora row telefono.pide, which the bot posts in the site's group, unless this phone already
    asked for this same camera less than PEDIR_AVISO_S ago (a phone flipping between links: aviso false, the row
    stays, the post does not)."""
    try:
        with q.connect() as conn, conn.transaction():
            link = conn.execute(q.SELECT_ENLACE_BY_CODIGO, {"codigo": codigo}).fetchone()
            cam = _camera_of(cfg, link["site"], link["camera"]) if link is not None else None
            if cam is not None:
                conn.execute(q.LOCK_CAMARA, {"site": link["site"], "camera": link["camera"]})
                link = conn.execute(q.SELECT_ENLACE_BY_CODIGO, {"codigo": codigo}).fetchone()   # as it is now
            if cam is None or link is None:
                return {"status": 404, "error": "Ese enlace no sirve: pide a tu gerente el enlace de tu cámara."}
            if not link["active"]:
                return {"status": 409, "error": f"El sitio {link['site']} está desactivado: cuando lo activen, abre el enlace otra vez."}
            if link["vencido"]:
                return {"status": 410, "error": "Ese enlace ya venció: pide a tu gerente el enlace nuevo de la cámara."}
            site, camera = link["site"], link["camera"]
            phone = conn.execute(q.LOCK_TELEFONO_BY_PASE, {"pase": pase}).fetchone()
            if phone is not None and (phone["site"], phone["camera"]) == (site, camera):
                return {"paso": "capturar"}
            if conn.execute(q.SELECT_TELEFONO_DE_CAMARA, {"site": site, "camera": camera}).fetchone() is not None:
                return {"status": 409, "error": f"{camera} ya tiene teléfono."}
            fresh, by = fresh_by(site, camera, cfg)
            if cam.source == "pi" and fresh and by is None:
                return {"status": 409, "error": f"La Pi de {camera} está mandando imagen: no hace falta un teléfono."}
            if phone is not None and (phone["pide_site"], phone["pide_camera"]) == (site, camera):
                if phone["nombre"] != nombre:
                    conn.execute(q.SET_TELEFONO_NOMBRE, {"id": phone["id"], "nombre": nombre})
                return {"paso": "pendiente"}
            waiting = int(conn.execute(q.COUNT_PEDIDOS_DE_CAMARA, {
                "site": site, "camera": camera, "id": phone["id"] if phone is not None else -1,
            }).fetchone()["n"])
            if waiting >= PENDIENTES_MAX:
                return {"status": 429, "error": f"{camera} ya tiene {PENDIENTES_MAX} teléfonos esperando al admin: espera a que los atienda."}
            if phone is None:
                phone_id = int(conn.execute(q.INSERT_TELEFONO_PIDE, {
                    "pase": pase, "nombre": nombre, "site": site, "camera": camera,
                }).fetchone()["id"])
            else:
                phone_id = int(phone["id"])
                conn.execute(q.SET_TELEFONO_PIDE, {"id": phone_id, "nombre": nombre, "site": site, "camera": camera})
            aviso = _aviso_nuevo(phone_id, site, camera)
            conn.execute(q.INSERT_ADMIN_LOG, {
                "usuario": "teléfono: " + nombre,
                "action": "telefono.pide",
                "detail": Jsonb({"site": site, "camera": camera, "nombre": nombre, "telefono": phone_id, "aviso": aviso}),
            })
    except pg_errors.UniqueViolation:
        return {"status": 409, "error": "Otro pedido de este teléfono se cruzó: vuelve a intentarlo."}
    if aviso:
        _aviso_dado(phone_id, site, camera)          # the row landed: the bot posts it
    return {"paso": "pendiente", "nuevo": True, "telefono": phone_id, "site": site, "camera": camera}


@router.post("/api/captura/pedir")
async def captura_pedir(body: PedirRequest, request: Request, response: Response) -> Any:
    """A phone asks to be the camera of the link it opened, with a name (Section 3.13.2). Its pass normally comes
    from /api/captura/yo; a browser that still has none gets one here (Set-Cookie, httponly). The answer is paso
    pendiente (or capturar when it already is that camera), or a plain refusal the page shows as it comes."""
    try:
        nombre = nombre_telefono(body.nombre)
    except ValueError as exc:
        return _error(400, str(exc))
    codigo = enlace_codigo(body.enlace)
    if codigo is None:
        return _error(404, "Ese enlace no sirve: pide a tu gerente el enlace de tu cámara.")
    token = pase_token(request) or new_pase()
    pase = pase_hash(token)
    result = await asyncio.to_thread(_pedir, pase, codigo, nombre, load_config())
    if "error" in result:
        return _error(result["status"], result["error"])
    set_pase(response, token)
    if result.pop("nuevo", False):
        forget_fuera(pase)
        log.info("phone request site=%s camera=%s telefono=%s", result.pop("site"), result.pop("camera"), result.pop("telefono"))
    return {"paso": result["paso"]}


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
    request: Request,
    photos: list[UploadFile] = File(default=[]),
    site: str | None = Form(None),
    camera: str | None = Form(None),
    record_type: str | None = Form(None),
    direction_dudosa: str | None = Form(None),
) -> Any:
    """1..capture.burst_frames JPEGs (THIS site's cap) -> the sharpest becomes the row (losers die in
    RAM), with capture.sharpness stamped, and ONE job: outgoing -> presentation at express priority
    (when the site's presentacion function is on), return -> analyze. A single-frame burst is the curl
    road. THAT site's gafete or the pass of the phone on THAT camera (the wall, 3.13.2). A burst whose
    every readable frame sits under the site's alerts.black_luma_max is refused at the door (never
    stored, never billed; the reply says black), and in a mixed burst only the frames above the floor
    compete. The row's photo is the winner as ONE light file, shrunk to PLATE_LIGHT_MAX_SIDE."""
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
    by = await guard_capture(request, req.site, req.camera)

    cfg = load_config()
    burst_max = knobs.effective(cfg, "capture.burst_frames", req.site)
    try:
        site_cfg = _site_cfg(cfg, req.site)
        if req.camera is not None:
            _source_or_403(by, _camera_cfg(site_cfg, req.site, req.camera), req.camera)
        if len(photos) > burst_max:
            raise Rejected(400, f"burst too large: {len(photos)} frames, max {burst_max}")
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
    measured = await asyncio.to_thread(lambda: [measure(f) for f in frames])
    scores: list[float | None] = [m[0] if m else None for m in measured]
    lumas: list[float | None] = [m[1] if m else None for m in measured]
    readable = [i for i, m in enumerate(measured) if m is not None]
    if not readable:
        return _error(400, "no frame in the burst is a readable JPEG")
    black_max = knobs.effective(cfg, "alerts.black_luma_max", req.site)
    lit = [i for i in readable if lumas[i] >= black_max]
    if not lit:
        log.info(
            "upload_burst refused black site=%s camera=%s frames=%d lumas=%s floor=%s",
            req.site, req.camera, len(frames), lumas, black_max,
        )
        return JSONResponse(status_code=400, content={
            "error": f"ráfaga negra: todos los cuadros están bajo el brillo {black_max:g}; no se guardó nada",
            "black": True,
        })
    best = max(lit, key=lambda i: scores[i])   # a black frame never wins (its noise can read sharp); first index wins a tie
    original, score = frames[best], scores[best]
    started = time.perf_counter()
    data = await asyncio.to_thread(light_plate, original)
    shrink_ms = (time.perf_counter() - started) * 1000

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
        "upload_burst site=%s record_type=%s camera=%s frames=%d scores=%s lumas=%s chosen=%d sharpness=%s "
        "direction_dudosa=%s kind=%s priority=%d plate_id=%s duplicate=%s photo=%s bytes_in=%d bytes=%d shrink_ms=%.1f",
        req.site, req.record_type, req.camera, len(frames), scores, lumas, best, score,
        capture.get("direction_dudosa", False), job_kind, priority, result["plate_id"],
        result.get("duplicate", False), rel_path, len(original), len(data), shrink_ms,
    )
    out: dict[str, Any] = {"plate_id": result["plate_id"], "sharpness": score, "frames": len(frames), "chosen": best, "kind": job_kind}
    if result.get("duplicate"):
        out["duplicate"] = True
    if data is not original:
        out["shrink_ms"] = round(shrink_ms, 1)     # absent when the winner was already light
    return out


# --- the human lane (2.7): review, stats, gallery, photo, dishes ---------------------------

def _scope(session: Session) -> str | None:
    """The one-row scope parameter: None for admin (any site), the account's site otherwise."""
    return scoped_site(session, None)


def _date_or_400(value: str | None, name: str) -> str | None:
    """YYYY-MM-DD or None; anything else is a 400 with the field named."""
    if value is None or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        raise Rejected(400, f"{name} must be YYYY-MM-DD, got {value!r}") from None


def _record_type_or_400(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    value = value.strip()
    if value not in ("return", "outgoing"):
        raise Rejected(400, f"record_type must be return or outgoing, got {value!r}")
    return value


def _filters(session: Session, site: str | None, record_type: str | None, date_from: str | None, date_to: str | None) -> dict[str, Any]:
    """The NULL-tolerant parameter set every list statement shares; the site is SCOPED here."""
    return {
        "site": scoped_site(session, (site or "").strip() or None),
        "record_type": _record_type_or_400(record_type),
        "date_from": _date_or_400(date_from, "date_from"),
        "date_to": _date_or_400(date_to, "date_to"),
    }


def _pages(total: int) -> int:
    return max(1, ceil(total / PAGE_SIZE))


def _fetch_one(sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
    with q.connect() as conn, conn.transaction():
        return conn.execute(sql, params).fetchone()


def _execute_count(sql: str, params: dict[str, Any]) -> int:
    with q.connect() as conn, conn.transaction():
        return conn.execute(sql, params).rowcount


def _readable(key: str) -> str:
    """A component key in plain words: the label of a component saved before labels existed, or of a key the menu
    no longer has (an old plate keeps the keys of the menu it was analyzed with)."""
    return key.replace("_", " ").capitalize()


def _dish_name(dish: str, names: dict[str, str]) -> str:
    """A dish as a person reads it: its name; a dish the menu never had, in plain words; the model's own
    "desconocido" as it is. Never a clave (Section 3.14)."""
    if dish in names or dish == UNKNOWN_DISH:
        return names.get(dish, dish)
    return _readable(dish)


def _labels_of(componentes: list[dict[str, Any]]) -> dict[str, str]:
    return {c["nombre"]: c.get("etiqueta") or _readable(c["nombre"]) for c in componentes}


def _menu_words(conn: Any) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """Every dish's name and its components' labels, active or not: the pages show names, never claves or keys
    (Section 3.14)."""
    rows = conn.execute(q.SELECT_ALL_DISHES).fetchall()
    return {r["dish_id"]: r["nombre"] for r in rows}, {r["dish_id"]: _labels_of(r["componentes"]) for r in rows}


def _active_dishes() -> list[dict[str, Any]]:
    with q.connect() as conn:
        rows = conn.execute(q.SELECT_ACTIVE_DISHES).fetchall()
    return [
        {
            "dish_id": r["dish_id"], "nombre": r["nombre"], "componentes": [c["nombre"] for c in r["componentes"]],
            "etiquetas": _labels_of(r["componentes"]),
        }
        for r in rows
    ]


@router.get("/api/dishes")
async def dishes(session: Session = Depends(require_session)) -> Any:
    """The corrector's universe: active dishes, their component keys and, since 3.14, each key's label (from the DATABASE)."""
    return await asyncio.to_thread(_active_dishes)


@router.get("/api/review")
async def review_list(
    tab: str = "pendientes",
    site: str | None = None,
    record_type: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = 1,
    session: Session = Depends(require_session),
) -> Any:
    """The three tabs with live counts, the filters, one page of rows (newest first)."""
    try:
        status = REVIEW_TABS.get(tab)
        if status is None:
            raise Rejected(400, f"unknown tab {tab!r}; valid: {', '.join(REVIEW_TABS)}")
        params = _filters(session, site, record_type, date_from, date_to)
    except Rejected as exc:
        return _error(exc.status, exc.reason)
    page = max(1, page)

    def work() -> tuple[list[dict[str, Any]], list[dict[str, Any]], tuple[dict[str, str], dict[str, dict[str, str]]]]:
        with q.connect() as conn:
            counts = conn.execute(q.REVIEW_COUNTS, params).fetchall()
            rows = conn.execute(
                q.REVIEW_LIST, {**params, "status": status, "limit": PAGE_SIZE, "offset": (page - 1) * PAGE_SIZE}
            ).fetchall()
            words = _menu_words(conn)
        return counts, rows, words

    count_rows, rows, (names, labels) = await asyncio.to_thread(work)
    by_status = {r["review_status"]: int(r["n"]) for r in count_rows}
    counts = {name: by_status.get(st, 0) for name, st in REVIEW_TABS.items()}
    total = counts[tab]
    # Section 3.14: each site's confianza_minima, so the page paints the rows under it (the panel's tile counts them).
    cfg = await asyncio.to_thread(load_config)
    minimas = {s: knobs.effective(cfg, "confianza_minima", s) for s in sorted({r["site"] for r in rows})}
    listed = sorted({d for r in rows for d in (r["dish_predicted"], r["dish_verified"]) if d})
    return {
        "tab": tab,
        "counts": counts,
        "minimas": minimas,
        "nombres": {d: names[d] for d in listed if d in names},       # the listed dishes' names (3.14: never a clave)
        "etiquetas": {d: labels[d] for d in listed if d in labels},   # and their components' labels
        "rows": jsonable_encoder(rows),
        "page": page,
        "pages": _pages(total),
        "page_size": PAGE_SIZE,
        "total": total,
    }


@router.post("/api/review/{plate_id}/verify")
async def review_verify(plate_id: int, body: VerifyRequest, session: Session = Depends(require_session)) -> Any:
    """The one-by-one correction. Validated against the active dishes so a typo never becomes truth."""
    universe = {d["dish_id"]: d["componentes"] for d in await asyncio.to_thread(_active_dishes)}
    if body.dish_verified != UNKNOWN_DISH and body.dish_verified not in universe:
        return _error(400, f"dish_verified {body.dish_verified!r} is not an active dish (or 'desconocido')")
    if body.leftovers_verified is not None:
        if body.dish_verified == UNKNOWN_DISH:
            return _error(400, "leftovers_verified makes no sense for 'desconocido': omit it")
        unknown = sorted(k for k in body.leftovers_verified if k not in universe[body.dish_verified])
        if unknown:
            return _error(400, f"leftovers_verified has components not in {body.dish_verified!r}: {unknown}")
    params = {
        "id": plate_id,
        "site": _scope(session),
        "dish_verified": body.dish_verified,
        "leftovers_verified": Jsonb(body.leftovers_verified) if body.leftovers_verified is not None else None,
    }
    row = await asyncio.to_thread(_fetch_one, q.VERIFY_PLATE, params)
    if row is None:
        return _error(404, f"plate {plate_id} not found")
    log.info(
        "review verify plate=%d dish_verified=%s corrected_pct=%s user=%s",
        plate_id, body.dish_verified, body.leftovers_verified is not None, session.user,
    )
    return jsonable_encoder(row)


async def _set_status(plate_id: int, status: str, session: Session) -> Any:
    row = await asyncio.to_thread(_fetch_one, q.SET_REVIEW_STATUS, {"id": plate_id, "status": status, "site": _scope(session)})
    if row is None:
        return _error(404, f"plate {plate_id} not found")
    log.info("review status plate=%d status=%s user=%s", plate_id, status, session.user)
    return jsonable_encoder(row)


@router.post("/api/review/{plate_id}/not_plate")
async def review_not_plate(plate_id: int, session: Session = Depends(require_session)) -> Any:
    return await _set_status(plate_id, REVIEW_NOT_PLATE, session)


@router.post("/api/review/{plate_id}/reopen")
async def review_reopen(plate_id: int, session: Session = Depends(require_session)) -> Any:
    """Back to pendientes; a previous correction stays in the row until the next verify."""
    return await _set_status(plate_id, REVIEW_UNREVIEWED, session)


@router.post("/api/review/{plate_id}/discard")
async def review_discard(plate_id: int, session: Session = Depends(require_session)) -> Any:
    """Descartar (SPEC 1.9): into the papelera ('discarded' + discarded_at starts the retention clock);
    the papelera restores it. Outside the session's site, unknown, or already discarded = 404."""
    row = await asyncio.to_thread(_fetch_one, q.DISCARD_PLATE, {"id": plate_id, "site": _scope(session)})
    if row is None:
        return _error(404, f"plate {plate_id} not found")
    log.info("review discard plate=%d user=%s", plate_id, session.user)
    return jsonable_encoder(row)


@router.post("/api/review/bulk")
async def review_bulk(body: BulkReviewRequest, session: Session = Depends(require_session)) -> Any:
    """One action over a multi-selection; correcting is one by one by design (Arch section 9)."""
    ids = sorted(set(body.ids))
    params: dict[str, Any] = {"ids": ids, "site": _scope(session)}
    if body.action == "verify_as_is":
        updated = await asyncio.to_thread(_execute_count, q.BULK_VERIFY_AS_IS, params)
    elif body.action == "discard":
        updated = await asyncio.to_thread(_execute_count, q.BULK_DISCARD, params)
    else:
        status = REVIEW_NOT_PLATE if body.action == "not_plate" else REVIEW_UNREVIEWED
        updated = await asyncio.to_thread(_execute_count, q.BULK_SET_REVIEW_STATUS, {**params, "status": status})
    log.info("review bulk action=%s requested=%d updated=%d user=%s", body.action, len(ids), updated, session.user)
    return {"action": body.action, "requested": len(ids), "updated": updated}


@router.get("/api/stats")
async def stats(
    site: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    session: Session = Depends(require_session),
) -> Any:
    """The panel: per dish and component averages (return records), the live queue, the presentation
    summary, and the confianza count (Section 3.13): analyzed return rows ranked under EACH site's own
    confianza_minima. "minima" names the level when every site in view shares it, and is absent when
    they differ."""
    try:
        params = _filters(session, site, None, date_from, date_to)
    except Rejected as exc:
        return _error(exc.status, exc.reason)
    cfg = load_config()

    def work() -> dict[str, Any]:
        with q.connect() as conn:
            comps = conn.execute(q.STATS_COMPONENTS, params).fetchall()
            dish_counts = conn.execute(q.STATS_DISH_PLATES, params).fetchall()
            names, labels = _menu_words(conn)
            queue = conn.execute(q.STATS_QUEUE, {"site": params["site"]}).fetchone() or {}
            pres = conn.execute(q.STATS_PRESENTATION, params).fetchone() or {}
            in_view = [params["site"]] if params["site"] else [r["site"] for r in conn.execute(q.SELECT_SITES_ALL).fetchall()]
            minimas = {s: knobs.effective(cfg, "confianza_minima", s) for s in in_view}
            bajo_umbral = sum(
                int(conn.execute(q.STATS_LOW_CONFIDENCE, {**params, "site": s, "min_rank": CONFIANZA_RANK[m]}).fetchone()["n"])
                for s, m in minimas.items()
            )
        levels = set(minimas.values()) or {knobs.effective(cfg, "confianza_minima")}
        confianza: dict[str, Any] = {"bajo_umbral": bajo_umbral}
        if len(levels) == 1:
            confianza["minima"] = levels.pop()
        by_dish: dict[str, list[dict[str, Any]]] = {}
        for r in comps:
            by_dish.setdefault(r["dish_id"], []).append({
                "component": r["component"],
                "etiqueta": labels.get(r["dish_id"], {}).get(r["component"]) or _readable(r["component"]),
                "avg_left_pct": round(float(r["avg_left"]), 1),
                "return_rate_pct": round(float(r["return_rate"]), 1),
                "n": int(r["n"]),
            })
        dishes_out = [
            {"dish_id": r["dish_id"], "nombre": _dish_name(r["dish_id"], names), "plates": int(r["plates"]), "components": by_dish.get(r["dish_id"], [])}
            for r in dish_counts
        ]
        avg_s = queue.get("avg_upload_to_done_s_24h")
        graded = int(pres.get("graded") or 0)
        return {
            "site": params["site"],
            "date_from": params["date_from"],
            "date_to": params["date_to"],
            "dishes": dishes_out,
            "queue": {
                "pending": int(queue.get("pending") or 0),
                "working": int(queue.get("working") or 0),
                "failed_24h": int(queue.get("failed_24h") or 0),
                "done_24h": int(queue.get("done_24h") or 0),
                "avg_upload_to_done_s_24h": round(float(avg_s), 1) if avg_s is not None else None,
            },
            "presentation": {
                "graded": graded,
                "flagged": int(pres.get("flagged") or 0),
                "avg_score": round(float(pres["avg_score"]), 1) if graded and pres.get("avg_score") is not None else None,
                "pass_rate_pct": round(float(pres["pass_rate"]), 1) if graded and pres.get("pass_rate") is not None else None,
            },
            "confianza": confianza,
        }

    return await asyncio.to_thread(work)


@router.get("/api/gallery")
async def gallery(
    site: str | None = None,
    dish: str | None = None,
    record_type: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    review_status: str | None = None,
    page: int = 1,
    session: Session = Depends(require_session),
) -> Any:
    """Thumbnails list with filters; each row carries its photo_url (the evidence view)."""
    try:
        params = _filters(session, site, record_type, date_from, date_to)
        status = (review_status or "").strip() or None
        if status is not None and status not in REVIEW_STATUSES:
            raise Rejected(400, f"review_status must be one of {', '.join(REVIEW_STATUSES)}")
    except Rejected as exc:
        return _error(exc.status, exc.reason)
    params["dish"] = (dish or "").strip() or None
    params["review_status"] = status
    page = max(1, page)

    def work() -> tuple[int, list[dict[str, Any]], dict[str, str]]:
        with q.connect() as conn:
            total = int(conn.execute(q.GALLERY_COUNT, params).fetchone()["n"])
            rows = conn.execute(q.GALLERY_LIST, {**params, "limit": PAGE_SIZE, "offset": (page - 1) * PAGE_SIZE}).fetchall()
            names, _labels = _menu_words(conn)
        return total, rows, names

    total, rows, names = await asyncio.to_thread(work)
    out_rows = []
    for r in rows:
        item = dict(r)
        item["dish"] = r["dish_verified"] or r["dish_predicted"]
        item["nombre"] = _dish_name(item["dish"], names) if item["dish"] else None   # 3.14: the caption's name
        item["photo_url"] = f"/api/photo/{r['id']}"
        out_rows.append(item)
    return {"rows": jsonable_encoder(out_rows), "page": page, "pages": _pages(total), "page_size": PAGE_SIZE, "total": total}


@router.get("/api/photo/{plate_id}")
async def photo(plate_id: int, session: Session = Depends(require_session)) -> Any:
    """The plate's JPEG from PHOTO_ROOT; the path comes from the row, never from the client."""
    row = await asyncio.to_thread(_fetch_one, q.SELECT_PHOTO_PATH_SCOPED, {"id": plate_id, "site": _scope(session)})
    if row is None:
        return _error(404, f"plate {plate_id} not found")
    path = PHOTO_ROOT / row["photo_path"]
    if not path.is_file():
        return _error(404, f"photo of plate {plate_id} not on disk")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})
