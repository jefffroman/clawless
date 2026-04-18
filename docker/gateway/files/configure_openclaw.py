#!/usr/bin/env python3
import json, os

# Config lives outside the agent workspace tree (and outside the S3-synced
# $HOME) so agents can't read or edit it via their file tools. The entrypoint
# installs a fresh root-owned baseline at this path before each invocation,
# and configure_openclaw rewrites it in place.
CONFIG_PATH = os.environ.get(
    "OPENCLAW_CONFIG_PATH",
    "/var/lib/openclaw/openclaw.json",
)

# Channel to configure (e.g. "telegram") and its provider-specific config
# blob (JSON string shaped by the storefront for each channel type).
# If either is absent the channels block is left untouched.
CHANNEL = os.environ.get("OPENCLAW_CHANNEL", "").strip().lower()
CHANNEL_CONFIG = json.loads(os.environ.get("OPENCLAW_CHANNEL_CONFIG", "null") or "null")
# Full OpenClaw model string (e.g. "bedrock/us.amazon.nova-micro-v1:0").
# If absent the existing model config is left untouched.
MODEL = os.environ.get("OPENCLAW_MODEL", "").strip()


MEMORY_SEARCH_BLOCK = {
    "memorySearch": {
        "enabled": True,
        "sources": ["memory", "sessions"],
        "extraPaths": [
            "SOUL.md", "AGENTS.md", "HEARTBEAT.md",
            "PROJECTS.md", "TOOLS.md", "IDENTITY.md",
            "USER.md", "reference/", "ARCHITECTURE.md"
        ],
        "experimental": {"sessionMemory": True}
    }
}

# MCP servers — OpenClaw uses its plugin system for MCP, not a config key.
# InboxAPI is installed globally via npm; register it as a plugin separately.
MCP_SERVERS = {}

# Enable full tool access. Without this the agent may boot with no shell/file
# access (the "messaging" profile trap — see openclaw issue #33225).
TOOLS_BLOCK = {"profile": "full"}

# SearXNG URL — injected by the task definition, points at the shared SearXNG
# Lambda Function URL.
SEARXNG_URL = os.environ.get("SEARXNG_URL", "").strip()

# Per-peer session isolation: each person who DMs the bot gets their own
# conversation thread. Safe default given dmPolicy: "open" on the Telegram channel.
SESSION_BLOCK = {"dmScope": "per-peer"}

# Sandbox: tools run in a Docker container as the gateway user (ubuntu).
# Ideally the container would use a separate UID for isolation, but OpenClaw's
# file tools bridge hardcodes 0600 perms (openclaw/openclaw#17941), so any file
# written by the gateway is unreadable by a different container UID. Until that's
# fixed, we leave docker.user unset and let OpenClaw auto-detect the UID from
# the workspace owner.
# Valid modes: "off", "non-main", "all".
#
# On Fargate there is no host Docker daemon available to the gateway, so
# OPENCLAW_SANDBOX_MODE=off disables the docker block entirely and runs tools
# in-process. The Fargate task boundary is the isolation.
SANDBOX_MODE = os.environ.get("OPENCLAW_SANDBOX_MODE", "all").strip().lower()

if SANDBOX_MODE == "off":
    SANDBOX_BLOCK = {"mode": "off"}
else:
    SANDBOX_BLOCK = {
        "mode": SANDBOX_MODE,
        "scope": "agent",
        "workspaceAccess": "rw",
        "docker": {
            "image": "openclaw-sandbox-common:bookworm-slim",
            "network": "bridge",
            "env": {
                "SEARXNG_URL": SEARXNG_URL,
            },
            "binds": [
                "/usr/local/lib/node_modules/openclaw/skills:/usr/local/lib/node_modules/openclaw/skills:ro",
            ],
            "extraHosts": [
                "host.docker.internal:host-gateway",
            ],
            "dangerouslyAllowExternalBindSources": True,
        },
    }


def patch_config():
    with open(CONFIG_PATH) as f:
        config = json.load(f)

    defaults = config.setdefault("agents", {}).setdefault("defaults", {})

    # Clean up stale keys from previous configure_openclaw.py runs.
    for stale_key in ("workspaceDir", "mcpServers", "sandbox", "provider"):
        config.pop(stale_key, None)
    config.get("agents", {}).pop("main", None)
    defaults.pop("mcpServers", None)
    defaults.pop("workspaceDir", None)
    defaults.pop("workspace", None)

    defaults.update(MEMORY_SEARCH_BLOCK)

    if MODEL:
        defaults.setdefault("model", {})["primary"] = MODEL
        print(f"agents.defaults.model.primary patched to {MODEL}")

    # tools, session, channels are valid at root level.
    existing_tools = config.get("tools", {})
    config["tools"] = {**existing_tools, **TOOLS_BLOCK}
    # Clean up stale web.search config from previous runs (now handled by skill).
    config["tools"].pop("web", None)
    print(f"tools patched: profile={TOOLS_BLOCK['profile']}")

    existing_session = config.get("session", {})
    config["session"] = {**existing_session, **SESSION_BLOCK}
    print(f"session.dmScope patched to {SESSION_BLOCK['dmScope']}")

    # Clean up stale provider block from previous runs.
    config.pop("provider", None)

    existing_sandbox = defaults.get("sandbox", {})
    defaults["sandbox"] = {**existing_sandbox, **SANDBOX_BLOCK}
    # Remove stale docker.user from previous runs (now auto-detected from workspace owner).
    defaults["sandbox"].get("docker", {}).pop("user", None)
    print("sandbox patched: mode=all, user=auto")

    # SearXNG skill: enable and set URL (shared Lambda Function URL).
    skills = config.setdefault("skills", {}).setdefault("entries", {})
    skills["searxng"] = {"enabled": True, "env": {"SEARXNG_URL": SEARXNG_URL}}
    print(f"skills.entries.searxng patched: SEARXNG_URL={SEARXNG_URL}")

    skills["sleep"] = {"enabled": True}
    print("skills.entries.sleep patched: enabled")

    skills["memory-curation"] = {"enabled": True}
    print("skills.entries.memory-curation patched: enabled")

    if CHANNEL and CHANNEL_CONFIG:
        # Merge into any existing channel block so manually-added fields
        # (e.g. allowFrom entries added via pairing) survive re-runs.
        existing = config.setdefault("channels", {}).get(CHANNEL, {})
        config["channels"][CHANNEL] = {**existing, **CHANNEL_CONFIG}
        print(f"channels.{CHANNEL} patched")

    # Context-engine plugin: baked into the image at /opt/openclaw/extensions.
    # Listed via plugins.load.paths so OpenClaw auto-loads it on boot. No
    # plugins.allow entry needed as of 2026.3.28 — bundled/explicit path
    # plugins auto-load. plugins.slots.contextEngine intentionally absent
    # (removed upstream; caused hard "not registered" errors).
    plugins = config.setdefault("plugins", {})
    load    = plugins.setdefault("load", {})
    paths   = load.setdefault("paths", [])
    ext_dir = "/opt/openclaw/extensions/clawless-memory"
    if ext_dir not in paths:
        paths.append(ext_dir)
    # Strip any stale keys from earlier configure_openclaw.py runs.
    plugins.pop("allow", None)
    plugins.pop("slots", None)
    print(f"plugins.load.paths patched: {ext_dir}")

    # Disable built-in plugins we don't use on Fargate. Each one adds boot time
    # (permission probes, device discovery, voice init, browser CDP bootstrap)
    # for features that are impossible or meaningless in this container.
    #   - phone-control / talk-voice: no phone, no speaker
    #   - acpx: we don't orchestrate agent-to-agent over ACP; channels are
    #     Telegram/Discord/Slack
    #   - browser: searxng skill uses stdlib HTTP via uv, not a Chrome session
    #   - device-pair: wake-greet uses `openclaw agent --channel`, not pairing;
    #     allowlists are managed via SSM by add-agent.sh
    entries = plugins.setdefault("entries", {})
    for disabled in ("phone-control", "talk-voice", "acpx", "browser", "device-pair"):
        entries[disabled] = {"enabled": False}
    print("plugins.entries: phone-control, talk-voice, acpx, browser, device-pair disabled")

    # mDNS/Bonjour discovery scans the local network on boot. Fargate tasks
    # are single-container with no LAN peers — the scan just burns ~1-2s and
    # logs noisy warnings about missing avahi.
    discovery = config.setdefault("discovery", {})
    discovery.setdefault("mdns", {})["mode"] = "off"
    print("discovery.mdns.mode patched: off")

    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
    print("openclaw.json patched — restart OpenClaw to apply")


if __name__ == "__main__":
    patch_config()
