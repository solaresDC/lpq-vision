# LPQ_VISION — Project Context

## What this is
Plate-waste computer-vision system for a restaurant chain (~21 cafés, CDMX).
Cameras photograph plates (returns at the bar; outgoing per camera role); a worker sends each photo + a
per-site menu reference to Claude (Sonnet 5); the validated result (dish,
% leftover per component, confidence) lands as one row per plate in Postgres.
Current phase (Fase 1 — The Living Pipeline): repo skeleton → API + worker +
queue → deploy to the live VPS → photo-to-JSON smoke in <60s.
Authoritative spec: the LPQ-FASE1-SPEC pasted in the build conversation.

## Sealed stack (LAW — fix implementations, never substitute pieces)
| Piece | Choice | Notes |
|---|---|---|
| Language | Python | containers on python:3.12.x-slim, exact patch pinned at build (rule 1); host has 3.11.5 |
| API | FastAPI + uvicorn | `api/main.py`; /api/health, /api/upload, /api/plates/{id} |
| Validation | pydantic | every boundary |
| LLM adapter | LiteLLM, LIBRARY mode, PINNED | model from config: `claude-sonnet-5`; cascade OFF; prompt caching ON (`cache: "on"`): ephemeral 5m mark on the stable prefix ([system+menu reference]) for Anthropic calls, photo after the mark; discount never a dependency; prompts never padded to chase the cache minimum (performance shapes the prompt, not savings) |
| DB + queue | Postgres 16 (pinned minor); queue = `jobs` table, FOR UPDATE SKIP LOCKED | NO Redis/broker, ever; all SQL in `brain/db/queries.py` |
| Bot | python-telegram-bot, PINNED | Fase 1 = heartbeat placeholder only |
| Deploy | Docker Compose, TWO files | `compose.mac.yaml` runs on BOTH laptop and VPS this era; `compose.universal.yaml` carries the future ollama block, never invoked now; bare `docker compose up` MUST fail (no compose.yaml exists) |
| Frontend | `frontend/` deletable by design | EMPTY in Fase 1 — building it is a scope violation |

## The live infrastructure (exists — touch only via listed commands)
- VPS `lpq-brain`: Hetzner CX23, Ubuntu 26.04.1, Docker 29.8.0+compose v2,
  Tailscale; UFW deny-all + tailscale0 only; public port 22 DELETED.
  Access: `ssh root@lpq-brain` (key auth over Tailscale). Deploy dir: /opt/lpq_vision.
- Tailscale club: lpq-brain (100.126.179.123) + this laptop + iphone-14.
- Anthropic: prepaid account, auto-reload OFF (spending hard-lock). Key in .env only.
- Telegram: bot @LPQ_vision_bot + group "LPQ demo-alertas" exist; code is Fase 2.

## HARD RULES (never violate)
1. Never trust remembered prices/versions/tags — verify at the source, then PIN.
2. Secrets never in chat/repo/logs. `.env` git-ignored from commit zero;
   REPLACE_WITH_ placeholders everywhere; never echo env values.
3. Sealed architecture is law: broken? fix the implementation, or STOP and flag.
4. Two-compose design exact; no compose.yaml ever; `-f compose.mac.yaml` always.
5. Absent-when-off: no null/disabled placeholder keys in configs or row JSON.
   YAML orthography: string values colliding with YAML magic words are ALWAYS
   quoted (`cascade: "off"`, `fallback: "none"`) — bare off/on/yes/no parse as
   booleans and none as null; bare values are reserved for real booleans.
6. Verify freshness before judging (docker ps, git status, the actual file).
7. Server: only the commands a section lists. UFW/Tailscale/sshd/Hetzner: never.
8. Job status + plate result commit in ONE transaction. Queue claim uses the
   verbatim SKIP LOCKED statement from the SPEC (with its run_after <= now()
   eligibility). A failed attempt re-enters 'pending' IMMEDIATELY with
   run_after = now() + min(2s/8s/30s by attempts, 30s ceiling + assert
   BACKOFF_CEILING >= max(BACKOFF)): the ROW waits,
   a worker NEVER sleeps. Every loop pass starts by rescuing 'working' rows
   with started_at older than 10 min back to 'pending'.
9. Container exposure is controlled by the compose BIND, never by UFW: Docker
   publishes ports through its own iptables chains, bypassing UFW's deny. The
   API publishes as ${BIND_IP:-127.0.0.1}:8000:8000; the server's .env sets
   BIND_IP to its Tailscale IP (100.126.179.123); no BIND_IP = 127.0.0.1.
   Boot order on the server is guaranteed by the tailscale-ready gate
   (two systemd unit files installed at deploy; SPEC section 1.7).

## Environment variables (.env — values typed only by the human)
ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN (reserved), POSTGRES_USER,
POSTGRES_PASSWORD, POSTGRES_DB, DATABASE_URL (+ BIND_IP in the SERVER's .env
only: Tailscale IP; absent on the laptop). Committed only as .env.example.

## My workflow
Windows 11 (Acer Predator) · PowerShell (bash exists; never require bash-only
for human steps) · VS Code + Claude Code · pip · python 3.11.5 on host ·
project folder C:\Users\danie\Downloads\LPQ_VISION · server ssh root@lpq-brain ·
local API http://localhost:8000/api/health · VPS API http://lpq-brain:8000/api/health
(club members only) · GitHub repo: lpq-vision (private).

## Session-logging model thresholds
Warn at 7 / 12 / 15 exchanges (update when the model changes).
