FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SOLVER_LOW_RAM=1

# Runtime deps for Camoufox (Firefox fork) on a slim base.
# playwright install-deps resolves the exact apt list for the bundled browser.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl xvfb fonts-liberation fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY deploy/requirements.txt /app/deploy/requirements.txt
RUN pip install --no-cache-dir -r /app/deploy/requirements.txt \
    # Firefox system libraries (exact list maintained by playwright)
    && python -m playwright install-deps firefox \
    # Bake the Camoufox browser into the image (no download at boot)
    && python -m camoufox fetch

COPY oxalpha-proxy/ /app/oxalpha-proxy/
COPY boterdrop-solver/ /app/boterdrop-solver/
COPY deploy/start.sh /app/deploy/start.sh
RUN chmod +x /app/deploy/start.sh

# In-container wiring: proxy (public) -> solver (loopback)
ENV SOLVER_BASE=http://127.0.0.1:8001 \
    SOLVER_TIMEOUT=120 \
    PROXY_API_KEY=sk-oxalpha-oreo-dummy-key

EXPOSE 10000

CMD ["bash", "/app/deploy/start.sh"]
