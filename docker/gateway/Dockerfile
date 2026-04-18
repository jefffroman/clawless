# syntax=docker/dockerfile:1

# ---------- Stage 1: builder ----------
# Everything that needs compilers, pip caches, or npm intermediates is
# built here. Nothing from this stage except explicitly-copied artifacts
# survives into the runtime image, so build-time leftovers (apt metadata,
# wheel extraction tmp, compiler toolchains) can't leak.
FROM node:22-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git python3 python3-venv \
      build-essential python3-dev \
    && rm -rf /var/lib/apt/lists/*

# AWS CLI v1 (pip) instead of v2 (self-contained bundle). Saves ~70 MB
# uncompressed / ~25 MB compressed on pull — meaningful for wake time since
# Fargate re-pulls the image on every cold start. Our in-container usage is
# frozen-stable commands only (s3 sync/ls, ssm get/put-parameter,
# dynamodb get/delete-item, stepfunctions start-execution) that behave
# identically across v1 and v2, so v1's feature freeze costs us nothing.
# Installed in its own venv to avoid polluting the system Python that the
# memory venv and other scripts depend on.
RUN python3 -m venv /opt/awscli \
    && /opt/awscli/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/awscli/bin/pip install --no-cache-dir awscli

# uv + uvx (SearXNG skill invokes `uv run ...` at turn time).
RUN curl -LsSf https://astral.sh/uv/install.sh | \
      env UV_INSTALL_DIR=/usr/local/bin UV_UNMANAGED_INSTALL=1 sh

RUN npm install -g openclaw@latest \
    && npm cache clean --force

# Memory venv: chromadb + networkx + rank-bm25 for the retrieval pipeline,
# plus aiohttp + pyyaml for the long-running memory server. Embeddings come
# from chromadb's bundled DefaultEmbeddingFunction (ONNX runtime + all-
# MiniLM-L6-v2) — dropping sentence-transformers + torch saves ~300 MB
# compressed. Cleanup is in-stage because the whole venv directory gets
# COPY'd to runtime verbatim; what we don't scrub here ships.
RUN python3 -m venv /opt/clawless/memory/venv \
    && /opt/clawless/memory/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/clawless/memory/venv/bin/pip install --no-cache-dir \
         chromadb networkx rank-bm25 aiohttp pyyaml \
    && find /opt/clawless/memory/venv -type d -name '__pycache__' -exec rm -rf {} + \
    && find /opt/clawless/memory/venv -type f -name '*.pyc' -delete \
    && find /opt/clawless/memory/venv -type d \( -name 'tests' -o -name 'test' \) -prune -exec rm -rf {} +


# ---------- Stage 2: runtime ----------
FROM node:22-slim

# Runtime-only apt deps. No build-essential, no python3-dev, no pip.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git python3 python3-minimal python3-venv tini gosu \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /usr/share/doc/* /usr/share/man/* /usr/share/info/* \
    && find /usr/share/locale -mindepth 1 -maxdepth 1 -type d ! -name 'en*' -exec rm -rf {} +

# node:22-slim ships with a `node` user at UID 1000 — reclaim the UID for
# openclaw so it matches S3 Files access point defaults (UID/GID 1000).
RUN userdel --remove node 2>/dev/null || true \
    && useradd --create-home --home-dir /home/openclaw --uid 1000 --shell /bin/bash openclaw

# Pull installed artifacts from the builder. Each COPY lands a self-
# contained tree (venvs, node_modules, single binaries) that works as-is
# because the runtime base image is the same debian release as the builder.
COPY --from=builder /opt/awscli                              /opt/awscli
COPY --from=builder /usr/local/bin/uv                        /usr/local/bin/uv
COPY --from=builder /usr/local/bin/uvx                       /usr/local/bin/uvx
COPY --from=builder /usr/local/lib/node_modules/openclaw     /usr/local/lib/node_modules/openclaw
RUN ln -s /opt/awscli/bin/aws                          /usr/local/bin/aws \
    && ln -s ../lib/node_modules/openclaw/openclaw.mjs /usr/local/bin/openclaw

COPY --from=builder --chown=openclaw:openclaw /opt/clawless  /opt/clawless
RUN mkdir -p /opt/clawless/bin && chown openclaw:openclaw /opt/clawless/bin

COPY --chown=openclaw:openclaw docker/gateway/files/indexer.py        /opt/clawless/bin/indexer.py
COPY --chown=openclaw:openclaw docker/gateway/files/search.py         /opt/clawless/bin/search.py
COPY --chown=openclaw:openclaw docker/gateway/files/auto_retrieve.py  /opt/clawless/bin/auto_retrieve.py
COPY --chown=openclaw:openclaw docker/gateway/files/memory_server.py  /opt/clawless/bin/memory_server.py
COPY docker/gateway/files/configure_openclaw.py                       /usr/local/bin/configure-openclaw
RUN chmod +x /usr/local/bin/configure-openclaw /opt/clawless/bin/*.py

# Per-container memory data (ChromaDB + BM25 corpus + graph). Ephemeral: the
# memory_server rebuilds it at boot from the workspace's MEMORY.md / memory/.
# Owned by openclaw so the server can write without running as root.
RUN mkdir -p /var/lib/clawless-memory \
    && chown openclaw:openclaw /var/lib/clawless-memory \
    && chmod 0700 /var/lib/clawless-memory

# Baseline openclaw.json baked into the image. At boot the entrypoint copies
# this to /var/lib/openclaw/openclaw.json (root-owned, 0644) and configure-openclaw
# patches it with per-client env-driven config. Kept out of the agent workspace
# tree so the agent's file tools don't see or edit it.
# /var/lib/openclaw needs to be openclaw-writable so the gateway's startup
# plugin auto-enable flow can write its atomic rename tmp file. lock_config()
# in the entrypoint flips ownership back to root and chmods the config file
# to 0444 once the gateway reports ready.
RUN mkdir -p /opt/openclaw /var/lib/openclaw \
    && chown openclaw:openclaw /var/lib/openclaw \
    && chmod 0755 /var/lib/openclaw
COPY docker/gateway/files/openclaw.baseline.json /opt/openclaw/openclaw.baseline.json
RUN chown root:root /opt/openclaw/openclaw.baseline.json \
    && chmod 0644 /opt/openclaw/openclaw.baseline.json

# Context engine plugin: baked outside $HOME so the agent's file tools can't
# reach or modify it. configure-openclaw points plugins.load.paths here.
# The plugin POSTs every incoming prompt to the memory server and prepends
# retrieved context to the agent's system prompt.
RUN mkdir -p /opt/openclaw/extensions/clawless-memory
COPY docker/gateway/plugin/ /opt/openclaw/extensions/clawless-memory/
RUN chown -R root:root /opt/openclaw/extensions \
    && chmod -R a+rX /opt/openclaw/extensions

# Clawless-managed skills bundled into openclaw's skill dir.
# - sleep: self-pause via lifecycle SFN (sets /active=false, triggers Lambda)
# - memory-curation: MEMORY.md authoring protocol
# - searxng: web search via shared Lambda (SEARXNG_URL injected by task def)
COPY docker/gateway/skills/ /usr/local/lib/node_modules/openclaw/skills/
RUN chmod -R a+rX /usr/local/lib/node_modules/openclaw/skills/sleep \
                  /usr/local/lib/node_modules/openclaw/skills/memory-curation \
                  /usr/local/lib/node_modules/openclaw/skills/searxng

# Warm ChromaDB's bundled ONNX embedder so the ~80 MB all-MiniLM-L6-v2 tarball
# is cached in the image rather than downloaded on every cold start. Run as
# openclaw so $HOME/.cache/chroma is owned correctly.
RUN su openclaw -c '/opt/clawless/memory/venv/bin/python -c "from chromadb.utils.embedding_functions import DefaultEmbeddingFunction; DefaultEmbeddingFunction()([\"warm\"])"'

COPY docker/gateway/entrypoint.sh /usr/local/bin/clawless-entrypoint
RUN chmod +x /usr/local/bin/clawless-entrypoint

# Entrypoint runs as root so it can write a root-owned config file that the
# openclaw user can read but not modify. It drops to the openclaw user via
# gosu before exec-ing the gateway process.
ENV HOME=/home/openclaw
WORKDIR /home/openclaw

ENV OPENCLAW_CONFIG_PATH=/var/lib/openclaw/openclaw.json \
    OPENCLAW_BASELINE_PATH=/opt/openclaw/openclaw.baseline.json \
    OPENCLAW_SANDBOX_MODE=off \
    WORKSPACE_DIR=/home/openclaw \
    AWS_SDK_LOAD_CONFIG=1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/clawless-entrypoint"]
