"""The face's router: the five pages served as STATIC files, zero logic (SPEC 1.4).

No template engine exists or ever will (jinja2 never enters this repo): every page is a plain
HTML file that talks to /api/* with fetch. The assets (app.css, app.js, the page scripts) are
mounted by api/main.py at /static from STATIC_DIR. A page whose file is not built yet answers a
clear 404 instead of a 500. frontend/ stays deletable by design: nothing here touches the
database, the filesystem beyond its own folder, or the config.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

FRONTEND_DIR = Path(__file__).resolve().parent
PAGES_DIR = FRONTEND_DIR / "pages"
STATIC_DIR = FRONTEND_DIR / "static"

# URL name -> file. Only these five exist this era (SPEC 1.4).
PAGES: dict[str, str] = {
    "captura": "captura.html",
    "panel": "panel.html",
    "review": "review.html",
    "galeria": "galeria.html",
    "camara": "camara.html",
}

router = APIRouter()


def _serve(filename: str) -> Callable:
    async def page() -> FileResponse | JSONResponse:
        path = PAGES_DIR / filename
        if not path.is_file():
            return JSONResponse(status_code=404, content={"error": f"pagina {filename} aun no construida"})
        # no-store: a page edit (new image) must never be masked by a browser cache on the phone.
        return FileResponse(path, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-store"})

    page.__name__ = f"page_{filename.rsplit('.', 1)[0]}"
    return page


for _name, _filename in PAGES.items():
    # response_model=None: the handler returns Response objects, and FastAPI cannot build a
    # response model from the FileResponse | JSONResponse annotation (it fails at import).
    router.add_api_route(
        f"/{_name}", _serve(_filename), methods=["GET"], include_in_schema=False, response_model=None
    )
