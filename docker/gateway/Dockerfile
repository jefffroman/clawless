FROM node:22-slim

ARG AWSCLI_VERSION=2.17.0

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl unzip git python3 python3-minimal python3-venv tini gosu \
      build-essential python3-dev \
    && rm -rf /var/lib/apt/lists/*

RUN arch=$(dpkg --print-architecture) \
    && case "$arch" in \
         arm64) awscli_arch=aarch64 ;; \
         amd64) awscli_arch=x86_64 ;; \
         *) echo "unsupported arch: $arch" >&2; exit 1 ;; \
       esac \
    && curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${awscli_arch}-${AWSCLI_VERSION}.zip" -o /tmp/awscli.zip \
    && unzip -q /tmp/awscli.zip -d /tmp \
    && /tmp/aws/install \
    && rm -rf /tmp/awscli.zip /tmp/aws

RUN curl -LsSf https://astral.sh/uv/install.sh | \
      env UV_INSTALL_DIR=/usr/local/bin UV_UNMANAGED_INSTALL=1 sh

RUN npm install -g openclaw@latest

# node:22-slim ships with a `node` user at UID 1000 — reclaim the UID for
# openclaw so it matches S3 Files access point defaults (UID/GID 1000).
RUN userdel --remove node 2>/dev/null || true \
    && useradd --create-home --home-dir /home/openclaw --uid 1000 --shell /bin/bash openclaw

# Memory system venv: chromadb + sentence-transformers + networkx + rank-bm25
# for the retrieval pipeline, plus aiohttp + pyyaml for the long-running memory
# server. Baked into the image at a fixed path so it doesn't have to
# round-trip through the S3 workspace sync. Scripts (memory_server / indexer /
# search / auto_retrieve) live alongside.
RUN mkdir -p /opt/clawless/memory /opt/clawless/bin \
    && python3 -m venv /opt/clawless/memory/venv \
    && /opt/clawless/memory/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/clawless/memory/venv/bin/pip install --no-cache-dir \
         --index-url https://download.pytorch.org/whl/cpu \
         torch \
    && /opt/clawless/memory/venv/bin/pip install --no-cache-dir \
         chromadb sentence-transformers networkx rank-bm25 aiohttp pyyaml

COPY docker/gateway/files/indexer.py /opt/clawless/bin/indexer.py
COPY docker/gateway/files/search.py /opt/clawless/bin/search.py
COPY docker/gateway/files/auto_retrieve.py /opt/clawless/bin/auto_retrieve.py
COPY docker/gateway/files/memory_server.py /opt/clawless/bin/memory_server.py
COPY docker/gateway/files/configure_openclaw.py /usr/local/bin/configure-openclaw
RUN chmod +x /usr/local/bin/configure-openclaw /opt/clawless/bin/*.py \
    && chown -R openclaw:openclaw /opt/clawless

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

# Warm the sentence-transformers model cache as openclaw so it's owned correctly
# and available without a cold download at runtime.
RUN su openclaw -c '/opt/clawless/memory/venv/bin/python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer(\"all-MiniLM-L6-v2\")"'

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
