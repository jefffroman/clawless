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


def patch_config():
    with open(CONFIG_PATH) as f:
        config = json.load(f)

    backup = CONFIG_PATH + f".bak.{datetime.now().strftime('%Y%m%d%H%M%S')}"
    shutil.copy(CONFIG_PATH, backup)
    print(f"Backed up to {backup}")

    config.setdefault("agents", {}).setdefault("defaults", {}).update(MEMORY_SEARCH_BLOCK)

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
