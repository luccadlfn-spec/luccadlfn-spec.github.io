# Sandbox: the only place where code written by the agent runs.
# Python with a few common libraries; no compilers, no network tools.
# The isolation itself is set in docker-compose.yml (internal network,
# read-only root filesystem, no capabilities, pid/memory/CPU limits).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

COPY requirements.txt /opt/sandbox/requirements.txt
RUN pip install -r /opt/sandbox/requirements.txt \
 && python -c "import bs4, dateutil, lxml, matplotlib, numpy, pandas, requests, yaml"

# uid/gid 10001 is shared with the agent container: both use the same workspace files.
# The home directory is /tmp because that is the only writable place besides /workspace.
RUN groupadd --gid 10001 sandbox \
 && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /tmp --shell /bin/bash sandbox \
 && mkdir /workspace \
 && chown 10001:10001 /workspace

COPY server.py /opt/sandbox/server.py

USER 10001:10001
WORKDIR /workspace
EXPOSE 8070

# server.py must stay PID 1 (do not add an init): as PID 1 it cannot be killed
# by the code it runs, and it relies on that to clean up stray processes.
CMD ["python3", "/opt/sandbox/server.py"]
