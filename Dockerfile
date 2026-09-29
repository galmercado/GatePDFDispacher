FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

COPY requirements.txt .
# pytest is only needed for development; keep it out of the runtime image
RUN grep -v '^pytest' requirements.txt > runtime.txt && pip install -r runtime.txt

COPY app ./app
RUN useradd --system --uid 10001 gate && mkdir -p /srv/data /srv/storage && chown -R gate /srv
USER gate

ENV DATABASE_URL=sqlite:////srv/data/app.db STORAGE_DIR=/srv/storage
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"
# Single worker: SQLite (WAL) is happiest with one writer process at this scale.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
