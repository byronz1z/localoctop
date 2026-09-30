# localoctop adapter — standalone container image.
# Self-contained: copies only this directory; no Octop code enters the image.
FROM python:3.12-slim

WORKDIR /app

# Install dependencies first for layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code.
COPY localoctop ./localoctop

# Non-root runtime user; audit log directory must be writable.
RUN useradd --create-home --uid 10001 adapter \
    && mkdir -p /app/logs \
    && chown -R adapter:adapter /app
USER adapter

ENV LOCALOCTOP_HOST=0.0.0.0 \
    LOCALOCTOP_PORT=8080 \
    LOCALOCTOP_AUDIT_LOG=/app/logs/calls.jsonl \
    LOCALOCTOP_LOG_LEVEL=INFO

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status==200 else 1)"

CMD ["python", "-m", "localoctop.main"]
