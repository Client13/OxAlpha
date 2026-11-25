# Clean stdlib-only rebuild stack (oxalpha_server.py + proxy pool) — verified
# live on Render on 2026-11-25. The default ./Dockerfile builds the UI-bridge
# stack instead; to run THIS one on Render, set the service's
# "Dockerfile Path" to render-server.Dockerfile (dashboard → Settings).
FROM python:3.12-slim
WORKDIR /app
COPY oxalpha_server.py turnstile_solver.py proxy_pool.py proxies_seed.json ./
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD python3 oxalpha_server.py --host 0.0.0.0 --port ${PORT:-8000}
