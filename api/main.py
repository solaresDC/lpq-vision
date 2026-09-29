"""LPQ_VISION API (la ventanilla): the three Fase-1 endpoints, plus the Fase-2 routers.

GET  /api/health       -> alive + queue depth; 503 {"ok": false, "db": false} if Postgres is unreachable
POST /api/upload       -> saves the photo, inserts plates + jobs in ONE transaction, {"plate_id": n}
GET  /api/plates/{id}  -> the full row as JSON, or 404

Fase 2 mounts, without touching the three above: api/auth.py (login/logout/session),
api/routes.py (every other /api/* endpoint: the capture lane from 2.6, review/stats/gallery from 2.7), frontend/routes.py (the five
pages as static files) and /static (the page assets). Ownership: this service runs
brain.db.init at startup, BEFORE serving (SPEC 1.8). The auto docs stay switched off.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from api import auth
from api import routes as api_routes
from brain.capture.backends import PHOTO_ROOT
from brain.db import init as db_init
from brain.db import queries as q
from brain.validator.models import PROMPT_VERSION, UploadRequest, load_config
from frontend import routes as frontend_routes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("lpq.api")

# --- named constants (SPEC section 4) -------------------------------------------------
# PHOTO_ROOT is imported from brain.capture.backends: the ONE home of the photos root (the
# Fase-1 duplication died at the first Fase-2 touch of this file). Rows store paths RELATIVE
# to it; the root itself never enters a row.
# The pinned uuid5 namespace for photo filenames. It is part of the idempotency contract
# (filename = uuid5(PHOTO_NS, sha256 of the bytes)) and it NEVER changes.
PHOTO_NS = uuid.UUID("7f2a9c1e-3b4d-4f6a-8e5c-1d2b3a4c5e6f")
JOB_KIND_ANALYZE = "analyze"
JOB_PRIORITY_RETURN = 100


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Init is existence-gated and idempotent; running it here, before the first request,
    # is what makes /api/health answer only once the tables exist.
    outcome = await asyncio.to_thread(db_init.run)
    log.info("startup: db init %s, serving", outcome)
    yield


app = FastAPI(
    title="LPQ_VISION API",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


def _error(status: int, reason: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": reason})


@app.exception_handler(RequestValidationError)
async def _on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Malformed input is a 400 with a reason, never FastAPI's default 422.
    first = exc.errors()[0]
    where = ".".join(str(p) for p in first.get("loc", ()))
    return _error(400, f"malformed request: {where}: {first.get('msg', 'invalid')}")


@app.exception_handler(StarletteHTTPException)
async def _on_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    # The session dependency (401) and unknown routes (404) keep the system's {"error": ...} shape.
    return _error(exc.status_code, str(exc.detail))


@app.exception_handler(Exception)
async def _on_unhandled(request: Request, exc: Exception) -> JSONResponse:
    # The traceback goes to the log river; the client gets a plain JSON error, no internals.
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return _error(500, "internal error")


# --- health ---------------------------------------------------------------------------

def _queue_pending() -> int:
    with q.connect() as conn:
        return int(conn.execute(q.COUNT_PENDING_JOBS).fetchone()["n"])


@app.get("/api/health")
async def health() -> Any:
    try:
        pending = await asyncio.to_thread(_queue_pending)
    except Exception as exc:  # any DB failure: the system never dresses up as healthy
        log.warning("health: postgres unreachable (%s)", type(exc).__name__)
        return JSONResponse(status_code=503, content={"ok": False, "db": False})
    cfg = load_config()
    return {
        "ok": True,
        "model": cfg.active_model,
        "cascade": cfg.cascade,
        "queue_pending": pending,
        "db": True,
    }


# --- upload ---------------------------------------------------------------------------

def _photo_rel_path(site: str, data: bytes) -> str:
    """<site>/<yyyymmdd>/<uuid5 of the sha256>.jpg: deterministic for identical bytes on the same day."""
    digest = hashlib.sha256(data).hexdigest()
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"{site}/{day}/{uuid.uuid5(PHOTO_NS, digest)}.jpg"


def _write_photo(rel_path: str, data: bytes) -> None:
    """Atomic write: the file appears complete or not at all (never a half-written jpg)."""
    target = PHOTO_ROOT / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, target)


def _register_plate(req: UploadRequest, rel_path: str, data: bytes) -> dict[str, Any]:
    """The upload transaction: site check, duplicate lookup, plates + jobs insert, file write.

    Everything runs inside ONE transaction, the file write included: a disk failure rolls
    the row back, and a row never exists without its photo. The provenance columns get the
    plan of record from config; the worker's result transaction overwrites them with the
    truth actually used (SPEC 1.3).
    """
    cfg = load_config()
    with q.connect() as conn, conn.transaction():
        site_row = conn.execute(q.SELECT_SITE, {"site": req.site}).fetchone()
        if site_row is None or not site_row["active"]:
            return {"error": f"unknown site: {req.site}"}

        existing = conn.execute(q.SELECT_PLATE_ID_BY_PHOTO_PATH, {"photo_path": rel_path}).fetchone()
        if existing is not None:
            return {"plate_id": existing["id"], "duplicate": True}

        menu_version = int(conn.execute(q.MAX_MENU_VERSION).fetchone()["v"])
        inserted = conn.execute(
            q.INSERT_PLATE,
            {
                "site": req.site,
                "camera": req.camera,
                "record_type": req.record_type,
                "photo_path": rel_path,
                "model": cfg.active_model,
                "prompt_version": PROMPT_VERSION,
                "menu_version": menu_version,
            },
        ).fetchone()
        if inserted is None:
            # Identical bytes raced past the lookup; the UNIQUE lock held and ON CONFLICT
            # returned no row. Answer the same duplicate response, never an error.
            existing = conn.execute(q.SELECT_PLATE_ID_BY_PHOTO_PATH, {"photo_path": rel_path}).fetchone()
            return {"plate_id": existing["id"], "duplicate": True}

        plate_id = int(inserted["id"])
        conn.execute(
            q.INSERT_JOB,
            {"plate_id": plate_id, "kind": JOB_KIND_ANALYZE, "priority": JOB_PRIORITY_RETURN},
        )
        _write_photo(rel_path, data)
    return {"plate_id": plate_id}


@app.post("/api/upload")
async def upload(
    photo: UploadFile | None = File(None),
    site: str | None = Form(None),
    camera: str | None = Form(None),
    record_type: str | None = Form(None),
) -> Any:
    if photo is None:
        return _error(400, "missing photo")
    if site is None or not site.strip():
        return _error(400, "missing site")
    try:
        req = UploadRequest(site=site, camera=camera, record_type=record_type or "return")
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        return _error(400, f"malformed upload: {where}: {first.get('msg', 'invalid')}")

    data = await photo.read()
    if not data:
        return _error(400, "empty photo")

    rel_path = _photo_rel_path(req.site, data)
    result = await asyncio.to_thread(_register_plate, req, rel_path, data)
    if "error" in result:
        return _error(400, result["error"])

    log.info(
        "upload site=%s record_type=%s camera=%s plate_id=%s duplicate=%s bytes=%d photo=%s",
        req.site, req.record_type, req.camera, result["plate_id"],
        result.get("duplicate", False), len(data), rel_path,
    )
    return result


# --- plate read -----------------------------------------------------------------------

def _fetch_plate(plate_id: int) -> dict[str, Any] | None:
    with q.connect() as conn:
        return conn.execute(q.SELECT_PLATE, {"id": plate_id}).fetchone()


@app.get("/api/plates/{plate_id}")
async def get_plate(plate_id: int) -> Any:
    row = await asyncio.to_thread(_fetch_plate, plate_id)
    if row is None:
        return _error(404, f"plate {plate_id} not found")
    # JSONB columns arrive as Python objects, timestamps as datetimes: the encoder makes them JSON/ISO.
    return jsonable_encoder(row)


# --- Fase-2 wiring (the three endpoints above are untouched) --------------------------

app.include_router(auth.router)
app.include_router(api_routes.router)
app.include_router(frontend_routes.router)


class NoCacheStaticFiles(StaticFiles):
    """/static never stale across deploys: the browser keeps its copy but revalidates it (ETag/Last-Modified)."""
    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"   # the 200 and the 304 alike
        return response


app.mount("/static", NoCacheStaticFiles(directory=str(frontend_routes.STATIC_DIR)), name="static")
