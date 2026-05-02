# clawless-gateway — single-process Python aiohttp gateway. boto3 (Bedrock) +
# aiogram (telegram) + chromadb (memory). Targets ~340 MB compressed.
#
# python:3.12-slim is debian-bookworm-based, which means manylinux_2_17 wheels
# work for chromadb on ARM64 — no build-essential needed. The image holds:
#
#   /opt/clawless/venv      one venv with all gateway deps
#   /opt/clawless/app       application code (mirrors docker/gateway/app/)
#   /var/lib/clawless-memory  ephemeral chromadb + bm25 + graph
#   /home/clawless          workspace synced to/from S3 by entrypoint.sh

FROM python:3.12-slim

# Runtime deps only. AWS CLI v1 from apt is enough for `aws s3 sync` (we don't
# need v2 features, and apt-installed v1 is ~25 MB compressed vs ~70 MB for v2).
# tini is PID 1; gosu drops to the unprivileged clawless user before exec'ing
# the python process. sudo lets the gateway hand bash subshells off to a
# lower-privileged user (see clawless-tool below).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         ca-certificates curl tini gosu awscli sudo \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /usr/share/doc/* /usr/share/man/* /usr/share/info/* \
    && find /usr/share/locale -mindepth 1 -maxdepth 1 -type d ! -name 'en*' -exec rm -rf {} +

# Two unprivileged users. The gateway python process runs as clawless (UID
# 1000) and owns the workspace. The bash *tool* runs as clawless-tool (UID
# 1001) — a separate UID so the agent cannot signal the gateway, modify its
# code, or read /proc/<gateway>/environ (the kernel blocks cross-UID access
# to /proc/PID/environ regardless of file mode). clawless-tool has no shell,
# no home, and is not a member of any other group.
RUN useradd --create-home --home-dir /home/clawless --uid 1000 --shell /bin/bash clawless \
    && useradd --no-create-home --uid 1001 --shell /usr/sbin/nologin clawless-tool

# Allow clawless to invoke /usr/bin/bash as clawless-tool with no password.
# Restricted to the bash binary; sudoers `NOPASSWD: /usr/bin/bash` does not
# extend to other commands.
RUN echo 'clawless ALL=(clawless-tool) NOPASSWD: /usr/bin/bash' > /etc/sudoers.d/clawless-tool \
    && chmod 0440 /etc/sudoers.d/clawless-tool

# Single venv. chromadb pulls in onnxruntime + numpy + tokenizers; rank-bm25
# is pure-python; networkx is pure-python; aiogram pulls aiohttp transitively.
# All wheels are prebuilt for ARM64 manylinux_2_17 — no build-essential.
RUN python -m venv /opt/clawless/venv \
    && /opt/clawless/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/clawless/venv/bin/pip install --no-cache-dir \
         aiogram \
         boto3 \
         chromadb \
         networkx \
         rank-bm25 \
    && find /opt/clawless/venv -type d -name '__pycache__' -exec rm -rf {} + \
    && find /opt/clawless/venv -type f -name '*.pyc' -delete \
    && find /opt/clawless/venv -type d \( -name 'tests' -o -name 'test' \) -prune -exec rm -rf {} +

# Pre-cache the all-MiniLM-L6-v2 ONNX model so cold starts don't pay the
# ~80 MB download. The warmup is the same call the gateway makes on boot.
RUN mkdir -p /var/lib/clawless-memory \
    && chown clawless:clawless /var/lib/clawless-memory \
    && chmod 0700 /var/lib/clawless-memory \
    && su clawless -c '/opt/clawless/venv/bin/python -c "from chromadb.utils.embedding_functions import DefaultEmbeddingFunction; DefaultEmbeddingFunction()([\"warm\"])"'

# Application code: root-owned, readable+executable by the clawless group, NOT
# readable by clawless-tool. Mode 0750 means the agent's bash subshell cannot
# inspect /opt/clawless/app/system_prompt.md or modify any *.py.
COPY --chown=root:clawless docker/gateway/app/ /opt/clawless/app/
RUN chmod -R 0750 /opt/clawless/app

# Slim entrypoint: S3 sync-down on boot, exec gateway, SIGTERM trap that
# webhook-flips and sync-ups.
COPY docker/gateway/entrypoint.sh /usr/local/bin/clawless-entrypoint
RUN chmod +x /usr/local/bin/clawless-entrypoint

ENV HOME=/home/clawless \
    WORKSPACE_DIR=/home/clawless \
    PYTHONPATH=/opt/clawless \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AWS_SDK_LOAD_CONFIG=1

WORKDIR /home/clawless

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/clawless-entrypoint"]
