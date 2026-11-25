FROM python:3.12-slim
WORKDIR /app
COPY oxalpha_server.py turnstile_solver.py proxy_pool.py proxies_seed.json ./
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD python3 oxalpha_server.py --host 0.0.0.0 --port ${PORT:-8000}
