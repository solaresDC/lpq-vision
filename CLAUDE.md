# LPQ_VISION — Project Context

## What this is
Plate-waste computer-vision system for a restaurant chain (~21 cafés, CDMX).
Cameras photograph plates (returns at the bar; outgoing per camera role); a worker sends each photo + a
per-site menu reference to Claude (Sonnet 5); the validated result (dish,
% leftover per component, confidence) lands as one row per plate in Postgres. Outgoing plates get
an express presentation grade against their own reference photo.
Current phase (Fase 2 — The Face and the Voice): five static pages, the simple session, the
capture lane (burst + server-side sharpness), review/stats/gallery, the real Telegram bot, the
presentation lane live, real menu seed, bake-off, backups; then deploy + full Fase-1 regression.
Authoritative spec: the LPQ-FASE2-SPEC pasted in the build conversation. FASE 3 (the admin
module) is NOT built yet: its kit is written only after this phase's checklist is green.

## Sealed stack (LAW — fix implementations, never substitute pieces)
| Piece | Choice | Notes |
|---|---|---|
| Language | Python | containers on python:3.12.14-slim (pinned at build); host has 3.11.5 |
| API | FastAPI + uvicorn | `api/main.py` (the three Fase-1 endpoints, untouched), `api/auth.py` (session), `api/routes.py` (every other /api/*); ONE uvicorn process this era (RAM sessions + frame store depend on it) |
| Validation | pydantic | every boundary; ALL shapes in `brain/validator/models.py` |
| LLM adapter | LiteLLM, LIBRARY mode, PINNED | model from config: `claude-sonnet-5`; cascade OFF; prompt caching ON (`cache: "on"`): ephemeral 5m mark on the stable prefix, photo after the mark; discount never a dependency |
| DB + queue | Postgres 16.15; queue = `jobs` table, FOR UPDATE SKIP LOCKED | NO Redis/broker, ever; ALL SQL in `brain/db/queries.py`; `schema.sql` verbatim forever, evolution ONLY via `brain/db/migrations.py` (existence-gated, ADD-only, run on every boot) |
| Sharpness | opencv-python-headless + numpy (the ONE Fase-2 dependency family, pinned) | imported ONLY by `api/routes.py`; never under brain/ |
| Bot | python-telegram-bot 22.8, PINNED | real from Fase 2: /id /cola /foto + six alert families; ONE poller per token: the laptop's bot is stopped whenever the server's runs |
| Frontend | `frontend/` static HTML + vanilla JS, deletable by design | pages served as files (FileResponse), `/static` mounted; NO template engine ever, NO React/htmx/websockets/build step; pages talk ONLY to /api/* |
| Deploy | Docker Compose, TWO files | `compose.mac.yaml` runs on BOTH laptop and VPS this era; `compose.universal.yaml` carries the future ollama block, never invoked now; bare `docker compose up` MUST fail (no compose.yaml exists) |

## Compose lineage (the ageless truth)
"docker compose" means the compose PLUGIN (v2+ lineage), never the legacy `docker-compose` v1 binary.
Real versions recorded 28 sep 2026 (rule 1: re-verify with `docker compose version` before judging):
laptop Docker 27.1.1 + Compose v2.29.1-desktop.1; server Docker 29.8.0 + Compose v5.5.1.

## File map (Fase 2)
- `api/main.py` health/upload/plates + wiring · `api/auth.py` login/logout/session, PBKDF2, site scope · `api/routes.py` upload_burst, frame store, camera state, align, calibration, review, stats, gallery, photo, dishes
- `brain/db/` `schema.sql` (sealed) · `migrations.py` · `queries.py` (all SQL) · `init.py`
- `brain/worker/loop.py` analyze + presentation lanes · `brain/adapter/llm.py` · `brain/validator/models.py` + `repair.py` · `brain/capture/backends.py` (PHOTO_ROOT)
- `bot/main.py` the real bot · `scripts/seed_menu.py` `bakeoff.py` `pg_backup.sh`
- `frontend/routes.py` · `frontend/pages/{captura,panel,review,galeria,camara}.html` · `frontend/static/{app,captura,camara}.js` `app.css`
- `config/config.yaml` (capture, alerts, video_align, sites with funciones + cameras; kitchen_edge only once calibrated) · `config/menu.yaml` (real dishes) · `config/prompts/prompt_sonnet_v1.txt` `prompt_presentation_v1.txt`

## The live infrastructure (exists — touch only via listed commands)
- VPS `lpq-brain`: Hetzner CX23, Ubuntu 26.04.1, Tailscale; UFW deny-all + tailscale0 only; public
  port 22 DELETED. Access: `ssh root@lpq-brain` (key auth over Tailscale). Deploy dir: /opt/lpq_vision.
- Tailscale club: lpq-brain (100.126.179.123) + this laptop + iphone-14. The phone's camera needs a
  secure origin: HTTPS inside the club via Tailscale Serve (prepared at the Fase-2 deploy, HQ ruling).
- Anthropic: prepaid account, auto-reload OFF (spending hard-lock). Key in .env only.
- Telegram: bot @LPQ_vision_bot + group "LPQ demo-alertas"; the group's id lives in .env (TELEGRAM_CHAT_ID, temporal).

## HARD RULES (never violate)
1. Never trust remembered prices/versions/tags — verify at the source, then PIN.
2. Secrets never in chat/repo/logs. `.env` git-ignored from commit zero; REPLACE_WITH_ placeholders
   everywhere; never echo env values. Password HASHES live only in .env (single-quoted: the format
   carries $); plaintext passwords typed only by the human; sessions httponly; nothing auth-related logged.
3. Sealed architecture is law: broken? fix the implementation, or STOP and flag.
4. Two-compose design exact; no compose.yaml ever; `-f compose.mac.yaml` always.
5. Absent-when-off: no null/disabled placeholder keys in configs or row JSON.
   YAML orthography: string values colliding with YAML magic words are ALWAYS quoted
   (`cascade: "off"`); bare values are reserved for real booleans (`merma: true`).
6. Verify freshness before judging (docker ps, git status, the actual file).
7. Server: only the commands a section lists. UFW/Tailscale/sshd/Hetzner: never (Tailscale Serve
   only by the listed deploy commands).
8. Job status + plate result commit in ONE transaction; the verbatim SKIP LOCKED claim with
   run_after; a failed attempt re-enters 'pending' immediately with backoff; a worker NEVER sleeps;
   orphan rescue every pass. The presentation lane rides the SAME rail (priority 10).
9. Container exposure is the compose BIND (${BIND_IP:-127.0.0.1}:8000), never UFW; the boot-order
   gate (tailscale-ready) is installed on the server.
10. Extend, never break: the three Fase-1 endpoints, the analyze path, schema.sql and the compose
    files are byte-compatible; migrations only ADD.
11. Ephemeral means ephemeral: mirilla copies and alignment video live in the api's RAM only; the
    rope is mandatory, capped (video_align.max_s), never "never". The calibration write
    (kitchen_edge, one surgical line) is the ONLY config write before FASE 3.
12. The one-way lock: an outgoing plate that looks eaten falls to review flagged; doubt about
    direction goes to the merma lane WITH direction_dudosa; the reverse correction does not exist.
13. The browser gatekeeper is real: a function-off direction is discarded on the phone; the server
    refuses a stray one too.
14. Three TEMPORAL .env keys (TELEGRAM_CHAT_ID, ADMIN_PASSWORD_HASH, DEMO_PASSWORD_HASH) die in
    FASE 3; config.yaml carries ZERO temporal keys.
15. Reference photo ORIGINALS are sacred: parked under photos/reference/<dish>/original/ (never
    read, never overwritten); the diet copy at the dish path is what the system uses.
16. Rule 8 of the kit: verification one-liners with `$?`, escaped quotes or regex run from BASH.

## Environment variables (.env — values typed only by the human)
ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB, DATABASE_URL
(+ BIND_IP in the SERVER's .env only), TELEGRAM_CHAT_ID, ADMIN_PASSWORD_HASH, DEMO_PASSWORD_HASH.
Committed only as .env.example.

## My workflow
Windows 11 (Acer Predator) · PowerShell (bash exists; bash-only for the $?/quotes/regex one-liners) ·
VS Code + Claude Code · python 3.11.5 on host · project folder C:\Users\danie\Downloads\LPQ_VISION ·
server ssh root@lpq-brain · local http://localhost:8000/{api/health,panel,review,galeria,camara,captura} ·
VPS http://lpq-brain:8000/... (club members only) · GitHub repo: lpq-vision (private). Claude Code
commits; the human pushes.

## Session-logging model thresholds
Warn at 7 / 12 / 15 exchanges (update when the model changes).
