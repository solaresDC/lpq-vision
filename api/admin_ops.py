"""LPQ_VISION api/admin_ops.py: the mostrador's operations, "Cola, papelera y gasto" (SPEC 1.5, 1.9; a family of the
admin hub; Section 3.14).

GET  /api/admin/cola?site=               the queue: its brake, its counts and the failed plates (a manager: its own site)
POST /api/admin/cola/pausa               the brake of the whole chain (admin): queue_paused true, or the key deleted
POST /api/admin/cola/reintentar          the failed plates back to the queue, attempts at zero (a manager: its own site)
GET  /api/admin/papelera?site=&page=     the discarded plates, newest first, each with the day it leaves for good
POST /api/admin/papelera/restaurar       back to Review's Pendientes, their corrections kept
POST /api/admin/papelera/eliminar        eliminar ya: step 1 of the double confirm (the diff names every plate)
GET  /api/admin/gasto?site=&dias=        gasto de filas vivas (admin): tokens per site, local day and model, and the Leer
                                         ficha readings; money only where a human entered a price ("sin precio" otherwise)
POST /api/admin/gasto/precio             one model's price per million tokens, copied from the provider's page (admin)
POST /api/admin/gasto/precio/quitar      back to "sin precio" (admin)
POST /api/admin/gasto/tipo_cambio        one currency's exchange rate, typed by a human (admin; Section 3.17.1)
POST /api/admin/gasto/tipo_cambio/quitar back to USD only for that currency (admin)

The constantes are read in Inicio (the hub's /api/admin/constantes): this tab links there instead of repeating them.
Every action that changes something leaves ONE bitácora row (ids, counts, a model and its prices; never a key, a code
or a password), in the same transaction as the database change; a config change (the brake, a price) and its row land
together under the writer's lock, and a failed row puts the file back. A manager sees and acts only on its own site
(its queue's retry, its papelera); the brake, the gasto and the prices are the admin's. The worker reads queue_paused
on every pass and papelera_dias (a Perillas knob) on its hourly purge: nothing here talks to it. The gasto shows only what was spent: tokens that went in (the photo, the menu and the
instructions, however the provider billed them) and tokens that came out, and money where a price exists; no cache
number anywhere (owner, 8 Oct). It is the gasto of LIVE rows: a purged plate leaves with its jobs, and failed attempts or
bake-off calls never land here (the provider's console is the exact ledger). A plate discarded while its analysis still
waits in the queue is analyzed anyway (the claim is the queue's verbatim one): Esperando counts it until then.
Section 3.17.1: the gasto can also show its money in pesos (MXN) or Canadian dollars (CAD), converted on the page with
the exchange rate a human typed (tipos_cambio, with the day it was typed); the prices stay in USD per million tokens,
as the provider's page gives them.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable
from datetime import datetime, time, timedelta, timezone
from math import ceil
from typing import Any, get_args
from zoneinfo import ZoneInfo

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from psycopg.types.json import Jsonb

from api import config_writer as cw
from api.admin import config_action, propose, require_admin, require_mostrador, write_scope
from api.auth import Session, scoped_site
from brain.capture.backends import unlink_plate_photo
from brain.db import queries as q
from brain.validator import knobs
from brain.validator.models import (
    Config,
    IdsRequest,
    ModelNameRequest,
    Moneda,
    MonedaRequest,
    PriceConfig,
    PriceRequest,
    RetryRequest,
    SwitchRequest,
    TipoCambioRequest,
    keyed_contenders,
    load_config,
)

log = logging.getLogger("lpq.api.admin_ops")

router = APIRouter()

# --- named constants ------------------------------------------------------------------
PAGE_SIZE = 50                 # papelera rows per page (Review's page size)
FAILED_SHOWN = 50              # failed plates listed in the Cola (Reintentar takes them ALL)
ERROR_SHOWN_CHARS = 160        # a failure's note, shortened for the list
DIFF_LIST_MAX = 50             # plates named one by one in eliminar-ya's diff
PAGE_MAX = 100_000             # a page number beyond this is answered as this one (an OFFSET must fit a bigint)
GASTO_DIAS = (7, 30, 90)       # the gasto's ranges, in local days
GASTO_DIAS_DEFAULT = 30
PRICE_MAX_MTOK = 1000.0        # USD per million tokens: anything above is a typo (per token, or a missing point)
# What one US dollar is worth in each currency at least and at most (Section 3.17.1): a rate outside is a typo (a
# misplaced point, or the rate written backwards). Each with its words and an example for the refusal.
TIPO_CAMBIO_RANGO = {"MXN": (5.0, 60.0), "CAD": (0.5, 3.0)}
MONEDA_PALABRAS = {"MXN": ("pesos", "18.45"), "CAD": ("dólares canadienses", "1.38")}
TIPO_CAMBIO_DECIMALES = 6      # a rate keeps at most this many decimals (what a bank or Banxico's FIX ever shows)
assert set(TIPO_CAMBIO_RANGO) == set(MONEDA_PALABRAS) == set(get_args(Moneda)), "admin_ops: a Moneda without its range or words"
MTOK = 1_000_000
FLUJO = {"return": "regresa", "outgoing": "sale"}
UNKNOWN_DISH = "desconocido"   # the model's own word for a plate it does not know


# --- helpers ----------------------------------------------------------------------------

def _tx(fn: Callable[[psycopg.Connection], Any]) -> Any:
    with q.connect() as conn, conn.transaction():
        return fn(conn)


def _bitacora(conn: psycopg.Connection, actor: Session, action: str, detail: dict[str, Any]) -> None:
    """A bitácora row INSIDE the caller's transaction (it lands only if the action lands)."""
    conn.execute(q.INSERT_ADMIN_LOG_BY, {
        "usuario": actor.user, "usuario_uid": actor.uid, "action": action, "detail": Jsonb(detail),
    })


def _site_or_404(conn: psycopg.Connection, site: str | None) -> None:
    if site is not None and conn.execute(q.SELECT_SITE, {"site": site}).fetchone() is None:
        raise HTTPException(status_code=404, detail=f"sitio desconocido: {site}")


def _requested(site: str | None) -> str | None:
    return (site or "").strip() or None


def _help(name: str) -> dict[str, str]:
    """The three helps of a special knob (it lives in this tab, so its helps travel with the tab's data)."""
    knob = knobs.KNOBS[name]
    return {"name": knob.name, "que_es": knob.que_es, "ejemplo": knob.ejemplo, "recomendacion": knob.recomendacion}


def _short(text: str | None) -> str:
    plain = " ".join(str(text or "sin detalle").split())
    return plain if len(plain) <= ERROR_SHOWN_CHARS else plain[: ERROR_SHOWN_CHARS - 1] + "…"


def _dish_names(conn: psycopg.Connection) -> dict[str, str]:
    return {r["dish_id"]: r["nombre"] for r in conn.execute(q.SELECT_DISH_NAMES).fetchall()}


def _dish_label(dish: str | None, names: dict[str, str]) -> str:
    """A dish as a person reads it: its name; a dish the menu never had, in plain words; never a clave."""
    if not dish:
        return "sin analizar"
    if dish in names or dish == UNKNOWN_DISH:
        return names.get(dish, dish)
    words = dish.replace("_", " ")
    return words[:1].upper() + words[1:]


# --- the queue ----------------------------------------------------------------------------

@router.get("/api/admin/cola")
async def cola(site: str | None = None, session: Session = Depends(require_mostrador)) -> dict[str, Any]:
    scope = scoped_site(session, _requested(site))       # a manager is forced to its own site

    def work() -> tuple[Config, dict[str, Any], list[dict[str, Any]]]:
        cfg = load_config()
        with q.connect() as conn:
            _site_or_404(conn, scope)
            counts = conn.execute(q.COLA_COUNTS, {"site": scope}).fetchone() or {}
            failed = conn.execute(q.COLA_FAILED_LIST, {"site": scope, "limit": FAILED_SHOWN}).fetchall()
        return cfg, counts, failed

    cfg, counts, failed = await asyncio.to_thread(work)
    return jsonable_encoder({
        "site": scope,
        "pausada": bool(cfg.queue_paused),
        "puede_pausar": session.is_admin,
        "esperando": int(counts.get("esperando") or 0),
        "en_proceso": int(counts.get("en_proceso") or 0),
        "fallidos": int(counts.get("fallidos") or 0),
        "fallidos_lista": [
            {
                "job": r["job"], "plato": r["plate"], "site": r["site"], "camera": r["camera"],
                "flujo": FLUJO.get(r["record_type"], r["record_type"]), "intentos": r["attempts"],
                "error": _short(r["last_error"]), "cuando": r["finished_at"],
            }
            for r in failed
        ],
        "ayuda_pausa": _help("queue_paused"),
    })


@router.post("/api/admin/cola/pausa")
async def pausa(body: SwitchRequest, session: Session = Depends(require_admin)) -> dict[str, Any]:
    """The brake is ONE for the whole chain (the analysts serve every café from one queue): queue_paused true while
    on, the key deleted when off (absent-when-off). Plates keep arriving and wait; the worker claims nothing."""
    path = ("queue_paused",)

    def decide(cfg: Config) -> tuple[list[cw.Op], str, dict[str, Any], dict[str, Any]]:
        if bool(cfg.queue_paused) == body.on:
            return [], "", {}, {"pausada": body.on}
        op = cw.set_op(path, True) if body.on else cw.delete_op(path)
        return [op], "cola.pausa" if body.on else "cola.reanuda", {}, {"pausada": body.on}

    answer = await config_action(session, decide)
    if answer["cambio"]:
        log.info("queue %s from the mostrador user=%s", "paused" if body.on else "resumed", session.user)
    return answer


@router.post("/api/admin/cola/reintentar")
async def reintentar(body: RetryRequest, session: Session = Depends(require_mostrador)) -> dict[str, Any]:
    """Every failed plate of the scope back to the queue with its attempts at zero (a plate in the papelera never).
    A manager: its own site, whatever it asked; the admin: the site it names, or every site."""
    scope = write_scope(session, _requested(body.site))

    def work(conn: psycopg.Connection) -> int:
        _site_or_404(conn, scope)
        rows = conn.execute(q.RETRY_FAILED, {"site": scope}).fetchall()
        if rows:
            per_site: dict[str, int] = {}
            for r in rows:
                per_site[r["site"]] = per_site.get(r["site"], 0) + 1
            _bitacora(conn, session, "cola.reintenta", {"site": scope, "jobs": len(rows), "sitios": per_site})
        return len(rows)

    count = await asyncio.to_thread(_tx, work)
    log.info("queue retry site=%s jobs=%d user=%s", scope or "todos", count, session.user)
    return {"site": scope, "reintentados": count}


# --- the papelera -------------------------------------------------------------------------

@router.get("/api/admin/papelera")
async def papelera(site: str | None = None, page: int = 1, session: Session = Depends(require_mostrador)) -> dict[str, Any]:
    scope = scoped_site(session, _requested(site))
    page = min(max(1, page), PAGE_MAX)

    def work() -> tuple[Config, int, list[dict[str, Any]], dict[str, str]]:
        cfg = load_config()
        with q.connect() as conn:
            _site_or_404(conn, scope)
            total = int(conn.execute(q.PAPELERA_COUNT, {"site": scope}).fetchone()["n"])
            rows = conn.execute(
                q.PAPELERA_LIST, {"site": scope, "limit": PAGE_SIZE, "offset": (page - 1) * PAGE_SIZE}
            ).fetchall()
            names = _dish_names(conn)
        return cfg, total, rows, names

    cfg, total, rows, names = await asyncio.to_thread(work)
    retention = timedelta(days=cfg.papelera_dias)
    out = []
    for r in rows:
        dish = r["dish_verified"] or r["dish_predicted"]
        out.append({
            "id": r["id"], "site": r["site"], "camera": r["camera"],
            "flujo": FLUJO.get(r["record_type"], r["record_type"]), "ts": r["ts"],
            "descartado": r["discarded_at"],
            "se_borra": r["discarded_at"] + retention if r["discarded_at"] else None,
            "platillo": _dish_label(dish, names) if dish else None,
            "confianza": r["confidence"],
            "photo_url": "/api/photo/" + str(r["id"]),
        })
    return jsonable_encoder({
        "site": scope, "dias": cfg.papelera_dias, "total": total, "page": page,
        "pages": max(1, ceil(total / PAGE_SIZE)), "page_size": PAGE_SIZE, "rows": out,
    })


@router.post("/api/admin/papelera/restaurar")
async def restaurar(body: IdsRequest, session: Session = Depends(require_mostrador)) -> dict[str, Any]:
    """Back to Review's Pendientes with their corrections kept and the retention clock cleared. Only discarded plates
    of the caller's scope move; the rest are counted as not moved."""
    scope = write_scope(session, None)
    ids = sorted(set(body.ids))

    def work(conn: psycopg.Connection) -> list[int]:
        done = sorted(int(r["id"]) for r in conn.execute(q.PAPELERA_RESTORE, {"ids": ids, "site": scope}).fetchall())
        if done:
            _bitacora(conn, session, "papelera.restaura", {"plates": done, "site": scope})
        return done

    done = await asyncio.to_thread(_tx, work)
    log.info("papelera restore plates=%d of %d site=%s user=%s", len(done), len(ids), scope or "todos", session.user)
    return {"restaurados": len(done), "pedidos": len(ids)}


def _diff_line(row: dict[str, Any], names: dict[str, str], cfg: Config) -> str:
    dish = row["dish_verified"] or row["dish_predicted"]
    when = ""
    if row["discarded_at"] is not None:
        local = row["discarded_at"].astimezone(ZoneInfo(knobs.timezone_of(cfg, row["site"])))
        when = " · descartado el " + local.strftime("%d/%m/%Y")
    where = row["site"] + ("/" + row["camera"] if row["camera"] else "")
    return (
        "#" + str(row["id"]) + " · " + where + " · " + FLUJO.get(row["record_type"], row["record_type"])
        + " · " + _dish_label(dish, names) + when
    )


@router.post("/api/admin/papelera/eliminar")
async def eliminar(body: IdsRequest, session: Session = Depends(require_mostrador)) -> dict[str, Any]:
    """Eliminar ya, step 1: the diff names every plate that dies NOW (its row, its jobs, its photo). Step 2 (the hub's
    confirm) locks those rows again and deletes only the ones still in the papelera and in scope: jobs FIRST (the
    foreign key has no cascade and schema.sql stays sealed), then the rows with their bitácora row, then the photos,
    through the purge's own guard (nothing under reference/ ever)."""
    scope = write_scope(session, None)
    ids = sorted(set(body.ids))

    def read() -> tuple[Config, list[dict[str, Any]], dict[str, str]]:
        cfg = load_config()
        with q.connect() as conn:
            rows = conn.execute(q.PAPELERA_SELECT_IDS, {"ids": ids, "site": scope}).fetchall()
            names = _dish_names(conn)
        return cfg, rows, names

    cfg, rows, names = await asyncio.to_thread(read)
    if not rows:
        raise HTTPException(status_code=404, detail="Ninguno de esos platos sigue en la papelera de tu alcance: la lista se vuelve a leer.")
    chosen = [int(r["id"]) for r in rows]
    lista = [_diff_line(r, names, cfg) for r in rows[:DIFF_LIST_MAX]]
    if len(rows) > DIFF_LIST_MAX:
        lista.append("y " + str(len(rows) - DIFF_LIST_MAX) + " más")

    def work() -> list[int]:
        with q.connect() as conn, conn.transaction():
            locked = conn.execute(q.PAPELERA_LOCK_FOR_DELETE, {"ids": chosen, "site": scope}).fetchall()
            gone = sorted(int(r["id"]) for r in locked)
            if gone:
                conn.execute(q.DELETE_JOBS_OF_PLATES, {"ids": gone})
                conn.execute(q.DELETE_PLATES_BY_IDS, {"ids": gone})
                _bitacora(conn, session, "papelera.elimina_ya", {"plates": gone, "site": scope})
        # Files only after the rows are gone for good (the purge's order): a crash in between leaves an orphan
        # file, never a row pointing at a missing photo.
        for r in locked:
            unlink_plate_photo(r["photo_path"])
        return gone

    async def run() -> dict[str, Any]:
        gone = await asyncio.to_thread(work)
        log.info("papelera eliminar-ya plates=%d of %d site=%s user=%s", len(gone), len(chosen), scope or "todos", session.user)
        return {"eliminados": len(gone), "pedidos": len(chosen)}

    count = len(rows)
    platos = "1 plato" if count == 1 else str(count) + " platos"
    if count == 1:
        resumen = ("1 plato se borra para siempre ahora: su fila, sus análisis y su foto. No se puede deshacer. "
                   "Sin esto, la papelera lo borra sola a los " + str(cfg.papelera_dias) + " días de descartado.")
    else:
        resumen = (platos + " se borran para siempre ahora: cada uno con su fila, sus análisis y su foto. No se puede "
                   "deshacer. Sin esto, la papelera los borra sola a los " + str(cfg.papelera_dias) + " días de descartados.")
    return propose(
        session,
        action="eliminar ya " + platos + " de la papelera",
        diff={"resumen": resumen, "lista": lista},
        run=run,
    )


# --- the gasto ------------------------------------------------------------------------------

def _local_start(now: datetime, tz: str, dias: int) -> datetime:
    """The first instant of the range: midnight of the local day `dias - 1` days before today, in `tz`."""
    zone = ZoneInfo(tz)
    first = now.astimezone(zone).date() - timedelta(days=dias - 1)
    return datetime.combine(first, time(0), tzinfo=zone)


def _usd(row: dict[str, Any], price: PriceConfig | None) -> float | None:
    """Money of one usage row with its model's price (USD per million tokens); None = sin precio. The stored
    prompt_tokens already counts every input token, the cached ones included (LiteLLM adds Anthropic's cache
    writes and reads into it), so only what is left over is billed at the plain input price."""
    if price is None:
        return None
    written, read = int(row["cache_creation_input_tokens"]), int(row["cache_read_input_tokens"])
    plain = max(0, int(row["prompt_tokens"]) - written - read)
    total = (
        plain * price.in_mtok
        + int(row["completion_tokens"]) * price.out_mtok
        + written * price.cache_write_mtok
        + read * price.cache_read_mtok
    )
    return round(total / MTOK, 6)


def _tokens_in(row: dict[str, Any]) -> int:
    """Every token that went in, however it was billed: the stored prompt count already holds them all."""
    return int(row["prompt_tokens"])


def _summary(rows: list[dict[str, Any]], count_key: str) -> dict[str, Any]:
    priced = [r["usd"] for r in rows if r["usd"] is not None]
    return {
        count_key: sum(int(r[count_key]) for r in rows),
        "entrada": sum(r["entrada"] for r in rows),
        "salida": sum(r["salida"] for r in rows),
        "usd": round(sum(priced), 6) if priced else None,
        "sin_precio": len(rows) - len(priced),
    }


def _priceable(cfg: Config, model: str) -> bool:
    """A price may be entered for a keyed model, an already priced one, or one a plate or a ficha reading used.
    Sync: call it off the event loop."""
    if model in cfg.prices or model in {c.model for c in keyed_contenders()}:
        return True
    with q.connect() as conn:
        return bool(conn.execute(q.MODEL_USED, {"model": model}).fetchone()["used"])


@router.get("/api/admin/gasto")
async def gasto(site: str | None = None, dias: int = GASTO_DIAS_DEFAULT, session: Session = Depends(require_admin)) -> dict[str, Any]:
    if dias not in GASTO_DIAS:
        raise HTTPException(status_code=400, detail="El rango va en días: " + ", ".join(str(d) for d in GASTO_DIAS[:-1])
                            + " o " + str(GASTO_DIAS[-1]) + ".")
    scope = _requested(site)

    def work() -> dict[str, Any]:
        cfg = load_config()
        now = datetime.now(timezone.utc)
        sitios, filas = [], []
        with q.connect() as conn:
            _site_or_404(conn, scope)
            names = [scope] if scope else [r["site"] for r in conn.execute(q.SELECT_SITES_ALL).fetchall()]
            for name in names:
                tz = knobs.timezone_of(cfg, name)
                rows = conn.execute(q.SPEND_BY_DAY, {"site": name, "tz": tz, "since": _local_start(now, tz, dias)}).fetchall()
                mine = [
                    {
                        "dia": r["day"], "site": name, "modelo": r["model"], "platos": int(r["jobs"]),
                        "llamadas": int(r["calls"]), "entrada": _tokens_in(r), "salida": int(r["completion_tokens"]),
                        "usd": _usd(r, cfg.prices.get(r["model"])),
                    }
                    for r in rows
                ]
                filas += mine
                sitios.append({"site": name, "zona": tz, **_summary(mine, "platos")})
            fichas = None
            if scope is None:
                rows = conn.execute(
                    q.GASTO_FICHAS, {"tz": cfg.timezone, "since": _local_start(now, cfg.timezone, dias)}
                ).fetchall()
                mine = [
                    {
                        "dia": r["day"], "site": None, "modelo": r["model"], "lecturas": int(r["lecturas"]),
                        "entrada": _tokens_in(r), "salida": int(r["completion_tokens"]),
                        "usd": _usd(r, cfg.prices.get(r["model"] or "")),
                    }
                    for r in rows
                ]
                filas += mine
                fichas = {"zona": cfg.timezone, **_summary(mine, "lecturas")}
        filas.sort(key=lambda f: (f["site"] is None, f["site"] or "", f["modelo"] or ""))   # sites by name, fichas last
        filas.sort(key=lambda f: f["dia"], reverse=True)                                    # newest day first (stable)
        total = {
            "entrada": sum(f["entrada"] for f in filas),
            "salida": sum(f["salida"] for f in filas),
            "usd": round(sum(f["usd"] for f in filas if f["usd"] is not None), 6) if any(f["usd"] is not None for f in filas) else None,
            "sin_precio": sum(1 for f in filas if f["usd"] is None),
        }
        return {
            "site": scope, "dias": dias, "dias_opciones": list(GASTO_DIAS),
            "sitios": sitios, "fichas": fichas, "filas": filas, "total": total,
            "precios": {model: price.model_dump() for model, price in sorted(cfg.prices.items())},
            "modelos": sorted({f["modelo"] for f in filas if f["modelo"]} | {c.model for c in keyed_contenders()} | set(cfg.prices)),
            "ayuda_precios": _help("prices"),
            "monedas": list(TIPO_CAMBIO_RANGO),
            "tipos_cambio": {
                moneda: {"valor": rate.valor, "fecha": rate.fecha.isoformat()}
                for moneda, rate in sorted(cfg.tipos_cambio.items())
            },
            "ayuda_tipos_cambio": _help("tipos_cambio"),
        }

    return jsonable_encoder(await asyncio.to_thread(work))


@router.post("/api/admin/gasto/precio")
async def guardar_precio(body: PriceRequest, session: Session = Depends(require_admin)) -> dict[str, Any]:
    """One model's four prices, USD per million tokens, typed by a human from the provider's own price page (rule 1:
    prices ship empty and are never guessed). Written under prices.<model> through the ONE writer."""
    model = body.model.strip()
    values = {
        "in_mtok": body.in_mtok, "out_mtok": body.out_mtok,
        "cache_write_mtok": body.cache_write_mtok, "cache_read_mtok": body.cache_read_mtok,
    }
    if any(v > PRICE_MAX_MTOK for v in values.values()):
        raise HTTPException(
            status_code=400,
            detail="Revisa el precio: va en dólares por MILLÓN de tokens (por ejemplo 3 o 15) y ninguno pasa de 1000.",
        )
    cfg = await asyncio.to_thread(load_config)
    if not await asyncio.to_thread(_priceable, cfg, model):
        raise HTTPException(status_code=400, detail="«" + model + "» no es un modelo que este sistema use: elige uno de la lista.")

    def decide(now: Config) -> tuple[list[cw.Op], str, dict[str, Any], dict[str, Any]]:
        before = now.prices.get(model)
        antes = before.model_dump() if before is not None else None
        answer = {"modelo": model, "precio": values}
        if antes == values:
            return [], "", {}, answer
        return [cw.set_op(("prices", model), values)], "precio.guarda", {"modelo": model, "antes": antes, "despues": values}, answer

    answer = await config_action(session, decide)
    if answer["cambio"]:
        log.info("price set model=%s user=%s", model, session.user)
    return answer


@router.post("/api/admin/gasto/precio/quitar")
async def quitar_precio(body: ModelNameRequest, session: Session = Depends(require_admin)) -> dict[str, Any]:
    """Back to "sin precio": the model's line leaves the file (absent-when-off); its tokens keep showing."""
    model = body.model.strip()

    def decide(now: Config) -> tuple[list[cw.Op], str, dict[str, Any], dict[str, Any]]:
        before = now.prices.get(model)
        answer = {"modelo": model, "precio": None}
        if before is None:
            return [], "", {}, answer
        return [cw.delete_op(("prices", model))], "precio.quita", {"modelo": model, "antes": before.model_dump()}, answer

    answer = await config_action(session, decide)
    if answer["cambio"]:
        log.info("price removed model=%s user=%s", model, session.user)
    return answer


# --- the exchange rates (Section 3.17.1) ------------------------------------------------------

def _rate_text(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


@router.post("/api/admin/gasto/tipo_cambio")
async def guardar_tipo_cambio(body: TipoCambioRequest, session: Session = Depends(require_admin)) -> dict[str, Any]:
    """One currency's exchange rate, how many of it one US dollar is worth, typed by a human (owner, 16 Oct), with the
    day it was typed (the global timezone's calendar). Written under tipos_cambio.<moneda> through the ONE writer; the
    page converts the gasto with it."""
    low, high = TIPO_CAMBIO_RANGO[body.moneda]
    words, example = MONEDA_PALABRAS[body.moneda]
    if not math.isfinite(body.valor) or not low <= body.valor <= high:
        raise HTTPException(
            status_code=400,
            detail="Revisa el tipo de cambio: 1 dólar vale entre " + _rate_text(low) + " y " + _rate_text(high) + " "
            + words + ". Escribe cuántos " + words + " vale 1 dólar, como " + example + ".",
        )
    valor = round(body.valor, TIPO_CAMBIO_DECIMALES)

    def decide(now: Config) -> tuple[list[cw.Op], str, dict[str, Any], dict[str, Any]]:
        hoy = datetime.now(timezone.utc).astimezone(ZoneInfo(now.timezone)).date().isoformat()
        before = now.tipos_cambio.get(body.moneda)
        antes = {"valor": before.valor, "fecha": before.fecha.isoformat()} if before is not None else None
        values = {"valor": valor, "fecha": hoy}
        answer = {"moneda": body.moneda, "tipo_cambio": values}
        if antes == values:
            return [], "", {}, answer
        return ([cw.set_op(("tipos_cambio", body.moneda), values)], "tipo_cambio.guarda",
                {"moneda": body.moneda, "antes": antes, "despues": values}, answer)

    answer = await config_action(session, decide)
    if answer["cambio"]:
        log.info("exchange rate set currency=%s user=%s", body.moneda, session.user)
    return answer


@router.post("/api/admin/gasto/tipo_cambio/quitar")
async def quitar_tipo_cambio(body: MonedaRequest, session: Session = Depends(require_admin)) -> dict[str, Any]:
    """Back to USD only for that currency: its line leaves the file (absent-when-off)."""

    def decide(now: Config) -> tuple[list[cw.Op], str, dict[str, Any], dict[str, Any]]:
        before = now.tipos_cambio.get(body.moneda)
        answer = {"moneda": body.moneda, "tipo_cambio": None}
        if before is None:
            return [], "", {}, answer
        antes = {"valor": before.valor, "fecha": before.fecha.isoformat()}
        return [cw.delete_op(("tipos_cambio", body.moneda))], "tipo_cambio.quita", {"moneda": body.moneda, "antes": antes}, answer

    answer = await config_action(session, decide)
    if answer["cambio"]:
        log.info("exchange rate removed currency=%s user=%s", body.moneda, session.user)
    return answer
