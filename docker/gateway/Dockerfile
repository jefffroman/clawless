FROM node:22-slim

ARG AWSCLI_VERSION=2.17.0

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl unzip git python3 python3-minimal tini \
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

COPY ansible/roles/memory/files/configure_openclaw.py /usr/local/bin/configure-openclaw
RUN chmod +x /usr/local/bin/configure-openclaw

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
