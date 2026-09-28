# LPQ_VISION shared image: ONE image for the fastapi, worker and bot services.
# Compose gives each service its own command; this file never decides what runs.
# Base pinned to the exact 3.12 patch verified at build time (hard-learned rule 1).
FROM python:3.12.14-slim

# No .pyc files inside the image, unbuffered stdout so Docker's log river shows
# each line the instant it happens, and a quiet pip that keeps no cache layer.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, code second: Docker caches this layer, so editing code
# never reinstalls packages, and the pins are the only thing that can change it.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# The code packages enter the image: brain/api/bot from Fase 1, frontend/ (static pages
# served as files) and scripts/ (seed, bake-off, backup) from Fase 2. config/ is bind-mounted
# by compose (editable without a rebuild) and .env NEVER enters an image: secrets reach
# containers through env_file only.
COPY brain ./brain
COPY api ./api
COPY bot ./bot
COPY frontend ./frontend
COPY scripts ./scripts

# Runtime data lives on compose volumes mounted here, never inside the image.
RUN mkdir -p /data/photos /data/logs

# Every compose service overrides this; running the bare image just explains itself.
CMD ["python", "-c", "print('LPQ_VISION image: each compose service sets its own command')"]
