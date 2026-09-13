FROM python:3.11-slim
ARG VCS_REF=unknown
LABEL org.opencontainers.image.revision=$VCS_REF
WORKDIR /app
RUN useradd --uid 1000 --create-home sgo && mkdir -p /app/data /app/data/private /app/data/hf && chown -R sgo:sgo /app
COPY requirements-web.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements-web.txt
COPY --chown=sgo:sgo . .
USER sgo
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HF_HOME=/app/data/hf AUTH_DB_PATH=/app/data/private/auth.sqlite3
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log", "--no-proxy-headers", "--limit-concurrency", "32", "--timeout-keep-alive", "5"]
