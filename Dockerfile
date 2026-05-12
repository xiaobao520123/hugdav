FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/data/hf-cache \
    HUGDAV_HOST=0.0.0.0 \
    HUGDAV_PORT=8080

WORKDIR /app

# Install dependencies first to maximise Docker layer cache reuse.
COPY pyproject.toml ./
COPY src ./src
RUN pip install --root-user-action=ignore .

# HF download cache; mount a volume here in production to avoid re-fetching
# popular blobs on every container restart.
RUN mkdir -p /data/hf-cache
VOLUME ["/data/hf-cache"]

# Drop privileges.
RUN useradd -r -u 10001 -m -d /home/hugdav hugdav \
 && chown -R hugdav:hugdav /data
USER hugdav

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status==200 else 1)"

ENTRYPOINT ["hugdav"]
