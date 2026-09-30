# LPQ_VISION: Project Context

## What this is
Plate-waste computer-vision system for a restaurant chain (~21 cafés, CDMX).
Cameras photograph plates (returns and outgoing, per camera role); a worker sends each photo + a
per-site menu reference to Claude (Sonnet 5); the validated result (dish, % leftover per component,
confidence) lands as one row per plate in Postgres. Outgoing plates get an express presentation
grade against their own reference photo.
Fases 1 and 2 are LIVE on this laptop and on lpq-brain (deployed at main 1286eb9).
Current phase (Fase 3, El Producto, the Build Plan's Etapa F): the ADMIN module. Daily operation
becomes a page, never an SSH session: two floors (/admin mostrador, /maquinas machine room), the
knob registry + the one config writer + the knob cascade, the per-site gafete, horario + vigilantes
+ /extender, papelera, menus edited in the DB + the AMO-Y-SOMBRA uploader, Crear restaurante, the
spend dashboard, bot texts with fichas, the host butler, and the death of the three temporal .env
keys. Authoritative spec: the LPQ-FASE3-SPEC (v3.8) pasted in the build conversation.
THE SYSTEM IS ONE: the demo happens only after this phase's checklist is green.

## Sealed stack (LAW: fix implementations, never substitute pieces)
| Piece | Choice | Notes |
|---|---|---|
| Language | Python | containers on python:3.12.14-slim (pinned at build); host has 3.11.5 |
| API | FastAPI + uvicorn, ONE process | main.py (the three Fase-1 endpoints), auth.py (users, sessions, grant, gafete), routes.py (capture + human lanes), config_writer.py (the one writer), admin.py hub + six admin_*.py families; RAM sessions, grants, pending confirms, the frame store and the black clocks depend on ONE process |
| Validation | pydantic | ALL shapes in brain/validator/models.py; knobs.py = the knob registry (the whitelist); textos.py = bot texts, fichas, renderer, validator, destinations map (pure code) |
| LLM adapter | LiteLLM, LIBRARY mode, PINNED | model from config (claude-sonnet-5); max_tokens and llm_timeout_s from the machine block; model `cascade` stays "off"; prompt caching on |
| DB + queue | Postgres 16.15; queue = `jobs` table, FOR UPDATE SKIP LOCKED | NO Redis/broker, ever; ALL SQL in brain/db/queries.py; schema.sql sealed forever; evolution ONLY via migrations.py (existence-gated ADDs of columns and tables, every boot) |
| Pixels | opencv-python-headless + numpy (pinned) | sharpness, luma, resize; imported ONLY under api/ (routes.py and the admin families), never under brain/ |
| Bot | python-telegram-bot 22.8, PINNED | /id /cola /foto /extender, alert families + vigilantes; writes exactly ONE table (horario_extensiones); ONE poller per token |
| Frontend | static HTML + vanilla ES modules, deletable | seven pages served as files; admin.js shell + admin-knobs/voice/sites/menu/ops.js + admin-kit.js; maquinas.js; live views by polling; SSE ONLY for the log viewer; no websockets, no template engine, no build step |
| Butler | stdlib Python + systemd on the HOST | deaf to every network: unix-socket mailbox /run/lpq-butler/butler.sock mounted ONLY into fastapi; three verbs (reboot, scale worker 1..4, restart fastapi/worker/bot); never a service on the laptop |
| Deploy | Docker Compose, TWO files | compose.mac.yaml runs on laptop and VPS; compose.universal.yaml carries the future ollama block; bare `docker compose up` MUST fail |

## Compose lineage (the ageless truth)
"docker compose" means the compose PLUGIN (v2+ lineage), never the legacy `docker-compose` v1 binary.
Recorded 28 sep 2026 (rule 1: re-verify with `docker compose version` before judging):
laptop Docker 27.1.1 + Compose v2.29.1-desktop.1; server Docker 29.8.0 + Compose v5.5.1.

## File map (Fase 3 target; "F3" = born this phase)
- api/: main.py, auth.py, routes.py, config_writer.py (F3), admin.py (F3 hub), admin_knobs.py, admin_voice.py, admin_sites.py, admin_menu.py, admin_ops.py, admin_machine.py (F3)
- brain/db/: schema.sql (sealed), migrations.py, queries.py (all SQL), init.py
- brain/worker/: loop.py, menu_builder.py · brain/adapter/: llm.py, raw_log.py · brain/capture/backends.py (PHOTO_ROOT)
- brain/validator/: models.py, repair.py, knobs.py (F3), textos.py (F3)
- bot/main.py · butler/lpq_butler.py + butler/lpq-butler.service (F3, installed on the host only)
- scripts/: seed_menu.py, bakeoff.py, pg_backup.sh, set_password.py (F3)
- frontend/: routes.py; pages/ captura, panel, review, galeria, camara, admin (F3), maquinas (F3); static/ app.css, app.js, captura.js, camara.js, admin.js, admin-kit.js, admin-knobs.js, admin-voice.js, admin-sites.js, admin-menu.js, admin-ops.js, maquinas.js (F3)
- config/: config.yaml (OPERATOR STATE from Fase 3), textos.yaml (F3, machine-owned), menu.yaml (seed and backup road; the DB is the edited menu), prompts/

## The live infrastructure (exists: touch only via listed commands)
- VPS `lpq-brain`: Hetzner CX23, Ubuntu 26.04.1, Tailscale; UFW deny-all + tailscale0 only; public
  port 22 DELETED. Access: `ssh root@lpq-brain` (key auth over Tailscale). Deploy dir: /opt/lpq_vision.
- Club: lpq-brain (100.126.179.123) + this laptop + iphone-14. Club HTTPS via Tailscale Serve:
  https://lpq-brain.taild4ddb0.ts.net (tailnet only, no Funnel).
- Nightly pg_dump timer at 03:30 UTC with a proven restore road (from Fase 3 it also copies
  config.yaml and textos.yaml).
- Anthropic: prepaid account, auto-reload OFF (spending hard-lock). Key in .env only.
- Telegram: bot @LPQ_vision_bot + group "LPQ demo-alertas"; the group's id lives TEMPORARILY in .env
  (TELEGRAM_CHAT_ID) until the ceremony moves it to sites.telegram_chat_id.
- The butler: installed on the server only by the deploy section's listed commands.

## HARD RULES (never violate)
1. Never trust remembered prices/versions/tags: verify at the source, then PIN.
2. Secrets never in chat/repo/logs. `.env` git-ignored; REPLACE_WITH_ placeholders; never echo env
   values, passwords, hashes or gafetes. Account hashes live in the users table from Fase 3;
   plaintext passwords are typed only by the human (UI over club HTTPS, or set_password via
   getpass); nothing auth-related is logged beyond the user name. A gafete lives only in the DB and
   on the enrolled device.
3. Sealed architecture is law: broken? fix the implementation, or STOP and flag.
4. Two-compose design exact; no compose.yaml ever; `-f compose.mac.yaml` always.
5. Absent-when-off everywhere (overrides, textos, prices, mantenimiento, raw_logging_off_at,
   queue_paused, admin_chat_id, kitchen_edge). YAML orthography: string values that collide with
   YAML magic words are quoted, and the writer quotes EVERY string and time it emits; real booleans
   stay bare.
6. Verify freshness before judging (docker ps, git status, the actual file on the actual machine).
7. Server: only the commands a section lists. UFW, Tailscale (Serve included), sshd, Hetzner: never.
8. Queue: job status + plate result (+ jobs.usage) commit in ONE transaction; the verbatim SKIP LOCKED
   claim with run_after; a failed attempt re-enters 'pending' with backoff; a worker NEVER sleeps
   on a failure; orphan rescue every pass; both lanes ride the same rail.
9. Container exposure is the compose BIND (${BIND_IP:-127.0.0.1}:8000), never UFW; the boot-order
   gate (tailscale-ready) is installed on the server.
10. Extend, never break: Fase-1/2 endpoints, pages, schema and lanes stay byte-compatible (the gafete
    guards ACCESS, shapes untouched); migrations only ADD.
11. ONE writer: config.yaml is written only by api/config_writer.py (line-based, comments survive,
    reloaded through the models before the atomic swap, ONE asyncio lock); pyyaml never dumps it;
    textos.yaml is the one machine-owned file (whole-file atomic rewrite). A knob outside
    knobs.py cannot be written.
12. Ephemeral means ephemeral: mirilla copies, alignment video and the frame store live in the
    api's RAM only; the rope is mandatory and capped (video_align.max_s), never "never".
13. The one-way lock: an outgoing plate that looks eaten falls to review flagged; doubt about
    direction goes to the merma lane WITH direction_dudosa; the reverse correction does not exist.
14. The browser gatekeeper is real: a function-off direction is discarded on the phone; the server
    refuses a stray one too.
15. Whitelists are the security model: the knob registry, the butler's three verbs, the keyed-model
    selector. Every dangerous action: double confirm with the visible diff. Every admin action: a
    bitácora row. The admin's name never appears in a public message.
16. The three temporal .env keys (TELEGRAM_CHAT_ID, ADMIN_PASSWORD_HASH, DEMO_PASSWORD_HASH) die at
    the ceremony in the SPEC's exact order; config.yaml carries ZERO temporal keys.
17. Reference originals are sacred: never read, never overwritten; a hand-placed original is NEVER
    deleted; an uploader original+diet pair shares one stem and dies together, only when no row
    references it.
18. ONE poller per token: the laptop's bot stays stopped while the server's runs; local bot tests
    bracket a listed stop/start of the server's bot.
19. config.yaml and textos.yaml are OPERATOR STATE from Fase 3: they never travel in a deploy; the
    repo copies are the factory template, and laptop test edits are reverted before each commit.
20. Kit rule 8: one-liners with `$?`, escaped quotes or regex run from BASH; `bash -s` over ssh
    breaks after `docker compose exec` (pass scripts as the ssh argument); `pg_restore` reads stdin
    with NO file argument.

## Environment variables (.env: values typed only by the human)
ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB, DATABASE_URL
(+ BIND_IP in the SERVER's .env only). Until the ceremony also: TELEGRAM_CHAT_ID,
ADMIN_PASSWORD_HASH, DEMO_PASSWORD_HASH. Committed only as .env.example.

## My workflow
Windows 11 (Acer Predator) · PowerShell (bash for the rule-20 one-liners) · VS Code + Claude Code ·
python 3.11.5 on host · project folder C:\Users\danie\Downloads\LPQ_VISION · server
ssh root@lpq-brain · local http://localhost:8000/{api/health,panel,review,galeria,camara,captura,admin,maquinas} ·
VPS via the club: https://lpq-brain.taild4ddb0.ts.net/... (phone) or http://lpq-brain:8000/... (laptop) ·
GitHub repo: lpq-vision (private). Claude Code commits; the human pushes from PowerShell.

## Session-logging model thresholds
Warn at 7 / 12 / 15 exchanges (update when the model changes).
