# clawless-gateway — single-process Python aiohttp gateway. boto3 (Bedrock) +
# aiogram (telegram) + onnxruntime/tokenizers (memory embedder).
#
# python:3.12-slim is debian-bookworm-based, which means manylinux_2_17 wheels
# work for onnxruntime/tokenizers on ARM64 — no build-essential needed. The
# image holds:
#
#   /opt/clawless/venv      one venv with all gateway deps
#   /opt/clawless/app       application code (mirrors docker/gateway/app/)
#   /home/clawless          workspace archived (tar+zstd) to/from S3 by
#                           entrypoint.sh; the memory index persists inside it
#                           at /home/clawless/.index (int8 vectors + bm25 +
#                           graph + sync_state — NOT ephemeral, rides the archive)

FROM python:3.12-slim

# Runtime deps only. AWS CLI v1 from apt is enough for the S3 archive
# get/put (we don't need v2 features, and apt-installed v1 is ~25 MB
# compressed vs ~70 MB for v2). tini is PID 1; gosu drops to the
# unprivileged clawless user before exec'ing the python process. sudo lets
# the gateway hand bash subshells off to a lower-privileged user (see
# clawless-tool below). zstd compresses the single workspace archive
# (tar+zstd-1) on SIGTERM; it is not in python:3.12-slim by default.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         ca-certificates curl tini gosu awscli sudo zstd \
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

# Single venv. chromadb is GONE — the memory index uses our own int8 store
# (numpy) + a vendored MiniLM ONNX embedder (app/embedder.py: onnxruntime +
# tokenizers, the same model artifact chromadb shipped). Dropping chromadb
# removes ~167 MB / 47 transitive packages (grpc, kubernetes, pydantic,
# uvicorn, opentelemetry, …). rank-bm25 / networkx are pure-python; aiogram
# pulls aiohttp transitively. All wheels are prebuilt for ARM64
# manylinux_2_17 — no build-essential.
RUN python -m venv /opt/clawless/venv \
    && /opt/clawless/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/clawless/venv/bin/pip install --no-cache-dir \
         aiogram \
         boto3 \
         networkx \
         numpy \
         onnxruntime \
         rank-bm25 \
         tokenizers \
    && find /opt/clawless/venv -type d -name '__pycache__' -exec rm -rf {} + \
    && find /opt/clawless/venv -type f -name '*.pyc' -delete \
    && find /opt/clawless/venv -type d \( -name 'tests' -o -name 'test' \) -prune -exec rm -rf {} +

# Application code: root-owned, readable+executable by the clawless group, NOT
# readable by clawless-tool. Mode 0750 means the agent's bash subshell cannot
# inspect /opt/clawless/app/system_prompt.md or modify any *.py.
COPY --chown=root:clawless docker/gateway/app/ /opt/clawless/app/
RUN chmod -R 0750 /opt/clawless/app

# Pre-cache the all-MiniLM-L6-v2 ONNX model so cold starts don't pay the
# ~80 MB download. Same call the gateway makes on boot, via the vendored
# embedder (hence after the app COPY). The model is baked at
# /opt/clawless/models — OUTSIDE $WORKSPACE_DIR, so the workspace restore
# (which clears WORKSPACE_DIR children) can never wipe it.
RUN mkdir -p /opt/clawless/models \
    && chown clawless:clawless /opt/clawless/models \
    && su clawless -c 'PYTHONPATH=/opt/clawless CLAWLESS_MODEL_DIR=/opt/clawless/models/all-MiniLM-L6-v2 /opt/clawless/venv/bin/python -c "from app.embedder import MiniLMEmbedder; MiniLMEmbedder()([\"warm\"])"'

# Slim entrypoint: S3 sync-down on boot, exec gateway, SIGTERM trap that
# webhook-flips and sync-ups.
COPY docker/gateway/entrypoint.sh /usr/local/bin/clawless-entrypoint
RUN chmod +x /usr/local/bin/clawless-entrypoint

ENV HOME=/home/clawless \
    WORKSPACE_DIR=/home/clawless \
    PYTHONPATH=/opt/clawless \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AWS_SDK_LOAD_CONFIG=1 \
    CLAWLESS_MODEL_DIR=/opt/clawless/models/all-MiniLM-L6-v2

WORKDIR /home/clawless

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/clawless-entrypoint"]
