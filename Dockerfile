FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    THREADROUTE_HOME=/data PORT=8080

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN mkdir -p /data && python -m threadroute seed >/dev/null

EXPOSE 8080
# Cloud Run / Docker: the UI listens on $PORT. Set GOOGLE_API_KEY at deploy time (never bake it in).
CMD ["sh", "-c", "python -m threadroute web --host 0.0.0.0 --port ${PORT}"]
