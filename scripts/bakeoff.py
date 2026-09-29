"""T-E3: the six-string exam over VERIFIED rows (Arch section 22). Read-only; writes nothing to plates.

    docker compose -f compose.mac.yaml exec fastapi python -m scripts.bakeoff [--limit N] [--models a,b]

Every verified RETURN row with a dish (not 'desconocido') is re-run through each provider string:
ONE call per row per model, no re-ask (an unparseable answer counts as wrong: the exam grades the
raw answer). Truth: dish_verified and COALESCE(leftovers_verified, leftovers). Providers whose API
key is absent from the environment are SKIPPED and printed "sin llave" (absent-when-off applied to
keys): the exam runs Anthropic-only today and completes itself the day other keys are pasted.
It runs at ANY row count and prints the honest N, with a warning under BAKEOFF_MIN_ROWS.
Its calls go to the structured log with kind=bakeoff. Costs come from LiteLLM's cost table when
the model is known there, else "?".
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import litellm

from brain.adapter import llm
from brain.capture.backends import PHOTO_ROOT
from brain.db import queries as q
from brain.validator import repair
from brain.validator.models import BUCKET_SIZE, load_config, load_prompt
from brain.worker.menu_builder import MenuReference, build_reference

log = logging.getLogger("lpq.scripts.bakeoff")

BAKEOFF_MIN_ROWS = 200      # below this the table is printed with an honesty warning
UNKNOWN_DISH = repair.UNKNOWN_DISH


@dataclass(frozen=True)
class Contender:
    """One exam string: the lane, the LiteLLM model string, and the env key that unlocks it.

    Rule 1: strings and prices are verified at the source the day their key exists. The two
    Anthropic strings are verified today (LiteLLM's cost table); the four others carry the names
    the SPEC gives them and stay unverified until their keys are pasted (they are skipped anyway).
    """

    lane: str            # 'alto' | 'bajo'
    label: str
    model: str
    env_key: str


CONTENDERS: tuple[Contender, ...] = (
    Contender("alto", "Sonnet 5", "claude-sonnet-5", "ANTHROPIC_API_KEY"),
    Contender("alto", "Terra", "terra", "TERRA_API_KEY"),                         # verify when its key exists
    Contender("alto", "Gemini 3.1 Pro", "gemini/gemini-3.1-pro", "GEMINI_API_KEY"),   # verify when its key exists
    Contender("bajo", "Haiku 4.5", "claude-haiku-4-5-20251001", "ANTHROPIC_API_KEY"),
    Contender("bajo", "Luna", "luna", "LUNA_API_KEY"),                             # verify when its key exists
    Contender("bajo", "Gemini 3.6 Flash", "gemini/gemini-3.6-flash", "GEMINI_API_KEY"),  # verify when its key exists
)


@dataclass
class Score:
    n: int = 0
    dish_ok: int = 0
    comp_total: int = 0
    comp_exact: int = 0
    comp_within: int = 0
    calls: int = 0
    cost_usd: float = 0.0
    cost_known: bool = True
    latency_ms: list[int] = field(default_factory=list)
    errors: int = 0

    def row(self, label: str, lane: str) -> str:
        if self.n == 0:
            return f"{lane:<5} {label:<16} {'n=0':<8} {'-':>8} {'-':>10} {'-':>10} {'-':>8} {'-':>10}"
        dish = 100.0 * self.dish_ok / self.n
        exact = 100.0 * self.comp_exact / self.comp_total if self.comp_total else 0.0
        within = 100.0 * self.comp_within / self.comp_total if self.comp_total else 0.0
        cost = f"${self.cost_usd:.4f}" if self.cost_known else "?"
        lat = sum(self.latency_ms) // len(self.latency_ms) if self.latency_ms else 0
        return f"{lane:<5} {label:<16} n={self.n:<6} {dish:>7.1f}% {exact:>9.1f}% {within:>9.1f}% {lat:>6}ms {cost:>10}"


def _truth_rows(limit: int | None) -> list[dict[str, Any]]:
    with q.connect() as conn:
        rows = conn.execute(q.BAKEOFF_VERIFIED_ROWS, {"limit": limit}).fetchall()
    return rows


def _grade(score: Score, answer: repair.ValidatedAnswer | None, truth_dish: str, truth_pct: dict[str, int]) -> None:
    score.n += 1
    if answer is None or answer.dish != truth_dish:
        return
    score.dish_ok += 1
    for comp, truth in truth_pct.items():
        score.comp_total += 1
        pred = answer.leftovers.get(comp)
        if pred is None:
            continue
        if pred == truth:
            score.comp_exact += 1
        if abs(pred - truth) <= BUCKET_SIZE:
            score.comp_within += 1


def _cost_of(response: Any) -> float | None:
    try:
        return float(litellm.completion_cost(completion_response=response))
    except Exception:
        return None


def run_contender(c: Contender, rows: list[dict[str, Any]], references: dict[str, MenuReference], system_prompt: str, cache_on: bool) -> Score:
    score = Score()
    provider = llm.provider_for(c.model)
    for r in rows:
        ref = references[r["site"]]
        jpeg = (PHOTO_ROOT / r["photo_path"]).read_bytes()
        messages = llm.build_messages(system_prompt, ref, jpeg, cache_on, provider)
        started = time.monotonic()
        try:
            response = litellm.completion(model=c.model, messages=messages, max_tokens=llm.MAX_TOKENS, timeout=llm.LLM_TIMEOUT_S, num_retries=0, custom_llm_provider=provider)
        except Exception as exc:
            score.errors += 1
            score.n += 1
            log.warning("kind=bakeoff model=%s plate=%d error=%s", c.model, r["id"], type(exc).__name__)
            continue
        latency = int((time.monotonic() - started) * 1000)
        content = response.choices[0].message.content
        text = content if isinstance(content, str) else ""
        parsed, _ = repair.parse(text)
        answer = None
        if parsed is not None:
            answer = repair.ValidatedAnswer(dish=parsed.dish, leftovers=dict(parsed.leftovers), confidence=parsed.confidence)
        truth_pct = {k: int(v) for k, v in (r["truth_pct"] or {}).items()}
        _grade(score, answer, r["dish_verified"], truth_pct)
        score.calls += 1
        score.latency_ms.append(latency)
        cost = _cost_of(response)
        if cost is None:
            score.cost_known = False
        else:
            score.cost_usd += cost
        log.info(
            "kind=bakeoff model=%s plate=%d truth=%s answer=%s dish_ok=%s latency_ms=%d cost=%s",
            c.model, r["id"], r["dish_verified"], answer.dish if answer else None,
            bool(answer and answer.dish == r["dish_verified"]), latency, f"{cost:.5f}" if cost is not None else "?",
        )
    return score


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    litellm.suppress_debug_info = True
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="only the N newest verified rows")
    ap.add_argument("--models", type=str, default=None, help="comma-separated labels to run, e.g. 'Sonnet 5,Haiku 4.5'")
    args = ap.parse_args()

    cfg = load_config()
    rows = _truth_rows(args.limit)
    if not rows:
        print("Sin filas verificadas: verifica platos en /review y vuelve a correr.")
        return 0
    if len(rows) < BAKEOFF_MIN_ROWS:
        print(f"AVISO: solo {len(rows)} filas verificadas; el examen es indicativo hasta tener ~{BAKEOFF_MIN_ROWS}.")
    print(f"Examen sobre N={len(rows)} filas verificadas (return, con platillo). Modelo titular hoy: {cfg.active_model}.")

    wanted = {s.strip() for s in args.models.split(",")} if args.models else None
    system_prompt = load_prompt()
    cache_on = cfg.cache == "on"
    with q.connect() as conn:
        references = {site: build_reference(conn, site) for site in {r["site"] for r in rows}}

    print()
    print(f"{'lane':<5} {'modelo':<16} {'N':<8} {'dish':>8} {'bucket=':>10} {'bucket±1':>10} {'lat':>8} {'costo':>10}")
    print("-" * 82)
    for c in CONTENDERS:
        if wanted is not None and c.label not in wanted:
            continue
        if not os.environ.get(c.env_key, "").strip():
            print(f"{c.lane:<5} {c.label:<16} sin llave ({c.env_key} ausente): omitido")
            continue
        score = run_contender(c, rows, references, system_prompt, cache_on)
        line = score.row(c.label, c.lane)
        if score.errors:
            line += f"   errores={score.errors}"
        print(line)
    print()
    print("dish = platillo correcto; bucket= = componente exacto; bucket±1 = a un cubo de 10; costo = suma de las llamadas (LiteLLM).")
    print("Nada se escribió en plates.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
