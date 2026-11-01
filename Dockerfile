FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AUTOVAULT_HOME=/data \
    AUTOVAULT_CONTAINER=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system autovault \
    && useradd --system --gid autovault --home-dir /data --create-home autovault

WORKDIR /app
COPY --chown=autovault:autovault autovault/ /app/autovault/
RUN mkdir -p /data && chown -R autovault:autovault /data
USER autovault
EXPOSE 8765
HEALTHCHECK --interval=20s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=2)"
CMD ["python", "autovault/app.py", "--port", "8765"]
