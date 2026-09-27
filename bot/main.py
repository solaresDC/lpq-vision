"""The bot container, Fase-1 PLACEHOLDER: a heartbeat every 60 seconds, nothing else.

The real Telegram bot (python-telegram-bot, already pinned and installed) is Fase 2,
etapa D. Resting between heartbeats is deliberate: the never-sleeps law belongs to the
QUEUE WORKER, which has plates waiting; this placeholder has nothing to do yet.
"""

from __future__ import annotations

import logging
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("lpq.bot")

HEARTBEAT_SECONDS = 60


def main() -> None:
    log.info("bot placeholder starting: real bot logic arrives in Fase 2 (etapa D)")
    while True:
        log.info("bot placeholder: Fase 2 pending")
        time.sleep(HEARTBEAT_SECONDS)


if __name__ == "__main__":
    main()
