FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    THREADROUTE_HOME=/data PORT=8080

# Unprivileged user: the app never needs root
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY --chown=app:app . .
RUN mkdir -p /data && chown app:app /data && su -s /bin/sh app -c "python -m threadroute seed >/dev/null"

USER app
EXPOSE 8080
VOLUME ["/data"]

# Liveness probe hits the public /healthz route (no token needed)
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8080'),timeout=4)" || exit 1

# The UI listens on $PORT. Provide GOOGLE_API_KEY and THREADROUTE_AUTH_TOKEN at run time (never bake them in).
CMD ["sh", "-c", "python -m threadroute web --host 0.0.0.0 --port ${PORT}"]
