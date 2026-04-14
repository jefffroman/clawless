FROM node:22-slim

ARG AWSCLI_VERSION=2.17.0

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl unzip git python3 python3-minimal python3-venv tini \
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

RUN npm install -g openclaw@latest

# node:22-slim ships with a `node` user at UID 1000 — reclaim the UID for
# openclaw so it matches S3 Files access point defaults (UID/GID 1000).
RUN userdel --remove node 2>/dev/null || true \
    && useradd --create-home --home-dir /home/openclaw --uid 1000 --shell /bin/bash openclaw

# Memory system venv: chromadb + sentence-transformers + networkx + rank-bm25.
# Baked into the image at a fixed path so it doesn't have to round-trip through
# the S3 workspace sync. Scripts (indexer/search/auto_retrieve) live alongside.
RUN mkdir -p /opt/clawless/memory /opt/clawless/bin \
    && python3 -m venv /opt/clawless/memory/venv \
    && /opt/clawless/memory/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/clawless/memory/venv/bin/pip install --no-cache-dir \
         --index-url https://download.pytorch.org/whl/cpu \
         torch \
    && /opt/clawless/memory/venv/bin/pip install --no-cache-dir \
         chromadb sentence-transformers networkx rank-bm25

COPY ansible/roles/memory/files/indexer.py /opt/clawless/bin/indexer.py
COPY ansible/roles/memory/files/search.py /opt/clawless/bin/search.py
COPY ansible/roles/memory/files/auto_retrieve.py /opt/clawless/bin/auto_retrieve.py
COPY ansible/roles/memory/files/configure_openclaw.py /usr/local/bin/configure-openclaw
RUN chmod +x /usr/local/bin/configure-openclaw /opt/clawless/bin/*.py \
    && chown -R openclaw:openclaw /opt/clawless

# Warm the sentence-transformers model cache as openclaw so it's owned correctly
# and available without a cold download at runtime.
RUN su openclaw -c '/opt/clawless/memory/venv/bin/python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer(\"all-MiniLM-L6-v2\")"'

COPY docker/gateway/entrypoint.sh /usr/local/bin/clawless-entrypoint
RUN chmod +x /usr/local/bin/clawless-entrypoint

USER openclaw
ENV HOME=/home/openclaw
WORKDIR /home/openclaw

ENV OPENCLAW_CONFIG_PATH=/home/openclaw/.openclaw/openclaw.json \
    OPENCLAW_SANDBOX_MODE=off \
    WORKSPACE_DIR=/home/openclaw \
    AWS_SDK_LOAD_CONFIG=1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/clawless-entrypoint"]
