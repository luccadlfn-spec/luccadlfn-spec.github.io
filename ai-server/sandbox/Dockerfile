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

# startup/ holds the sitecustomize.py every snippet loads; it must stay in the
# image (read-only), never in a place snippets can write to.
COPY server.py /opt/sandbox/server.py
COPY startup/ /opt/sandbox/startup/

USER 10001:10001
WORKDIR /workspace
EXPOSE 8070

# server.py must stay PID 1 (do not add an init). As PID 1 it inherits every
# orphan (and reaps them), kill(-1) reaches exactly the other processes of the
# container, and the code it runs cannot stop it: the kernel drops signals
# without a handler that come from inside the container, and server.py only
# honours SIGTERM and SIGINT sent from outside it (docker stop).
CMD ["python3", "/opt/sandbox/server.py"]
