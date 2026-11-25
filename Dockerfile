FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# Chromium runtime libs (patchright install --with-deps resolves the rest),
# Xvfb for headed mode (stealth), and base fonts.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl xvfb fonts-liberation fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY deploy/requirements.txt /app/deploy/requirements.txt
RUN pip install --no-cache-dir -r /app/deploy/requirements.txt \
    && python -m patchright install --with-deps chromium

COPY oxalpha-proxy/ /app/oxalpha-proxy/
COPY solver-lite/ /app/solver-lite/
COPY boterdrop-solver/ /app/boterdrop-solver/
COPY deploy/start.sh /app/deploy/start.sh
RUN chmod +x /app/deploy/start.sh

# In-container wiring: proxy (public) -> solver-lite UI bridge (loopback).
# UPSTREAM_MODE=ui: the solver drives the real oxalpha web app (no Turnstile
# tokens needed). SOLVER_IMPL=boterdrop + a fat host restores the og solver.
ENV SOLVER_BASE=http://127.0.0.1:8001 \
    UPSTREAM_MODE=ui \
    SOLVER_IMPL=lite \
    PROXY_API_KEY=sk-oxalpha-oreo-dummy-key

EXPOSE 10000

CMD ["bash", "/app/deploy/start.sh"]
