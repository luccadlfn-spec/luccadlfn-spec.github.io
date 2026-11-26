# Agent service: FastAPI app on port 8090, run as an unprivileged user.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# Dependencies first, so editing the code does not reinstall them.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# uid/gid 10001 matches the owner of the bind-mounted data and workspace directories.
RUN groupadd --gid 10001 agent \
 && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin agent \
 && mkdir -p /data /workspace \
 && chown 10001:10001 /data /workspace \
 && python -m compileall -q app

USER 10001:10001
EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8090/agent/healthz', timeout=4).status == 200 else 1)"]

# One process, one event loop: the task queue, scheduler and locks live in memory.
# Open chat streams are given 5 s on shutdown, so the service stops cleanly inside
# Docker's default 10 s stop timeout.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090", "--workers", "1", \
     "--no-access-log", "--no-server-header", "--timeout-graceful-shutdown", "5"]
