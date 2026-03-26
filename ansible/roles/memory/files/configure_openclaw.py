import json, shutil, os
from datetime import datetime

# Path can be overridden via environment variable for server deployments
# where the openclaw user home differs from the provisioning user home.
CONFIG_PATH = os.environ.get(
    "OPENCLAW_CONFIG_PATH",
    os.path.expanduser("~/.openclaw/openclaw.json")
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

# SearXNG connection details — used by the skills.entries.searxng config block.
# Sandbox containers use bridge networking with extraHosts mapping
# host.docker.internal → host-gateway, so the container can reach the host.
SEARXNG_HOST = os.environ.get("SEARXNG_HOST", "host.docker.internal")
SEARXNG_PORT = os.environ.get("SEARXNG_PORT", "8080")

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
SANDBOX_BLOCK = {
    "mode": "all",
    "scope": "agent",
    "workspaceAccess": "rw",
    "docker": {
        "image": "openclaw-sandbox-common:bookworm-slim",
        "network": "bridge",
        "env": {
            "SEARXNG_URL": f"http://{SEARXNG_HOST}:{SEARXNG_PORT}",
        },
        "binds": [
            "/usr/lib/node_modules/openclaw/skills:/usr/lib/node_modules/openclaw/skills:ro",
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

    backup = CONFIG_PATH + f".bak.{datetime.now().strftime('%Y%m%d%H%M%S')}"
    shutil.copy(CONFIG_PATH, backup)
    print(f"Backed up to {backup}")

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

    # SearXNG skill: enable and set URL so the sandbox container can reach the host.
    searxng_url = f"http://{SEARXNG_HOST}:{SEARXNG_PORT}"
    skills = config.setdefault("skills", {}).setdefault("entries", {})
    skills["searxng"] = {"enabled": True, "env": {"SEARXNG_URL": searxng_url}}
    print(f"skills.entries.searxng patched: SEARXNG_URL={searxng_url}")

    if CHANNEL and CHANNEL_CONFIG:
        # Merge into any existing channel block so manually-added fields
        # (e.g. allowFrom entries added via pairing) survive re-runs.
        existing = config.setdefault("channels", {}).get(CHANNEL, {})
        config["channels"][CHANNEL] = {**existing, **CHANNEL_CONFIG}
        print(f"channels.{CHANNEL} patched")

    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
    print("openclaw.json patched — restart OpenClaw to apply")


if __name__ == "__main__":
    patch_config()
