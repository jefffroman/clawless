---
name: searxng
description: Privacy-respecting metasearch using your local SearXNG instance. Search the web, images, news, and more without external API dependencies.
author: Avinash Venkatswamy
version: 1.0.2-revised
homepage: https://searxng.org
triggers:
  - "search for"
  - "search web"
  - "find information"
  - "look up"
metadata: {"clawdbot":{"emoji":"🔍","requires":{"bins":["uv","python3"]},"config":{"env":{"SEARXNG_URL":{"description":"SearXNG instance URL","default":"http://localhost:8080","required":true}}}}}
---

# SearXNG Search

Search the web using your local SearXNG instance - a privacy-respecting metasearch engine.

## Prerequisites

1. `uv` (Python package manager) - located at `/usr/local/bin/uv`
2. `python3` - located at `/usr/bin/python3`
3. A running SearXNG instance (local, accessible via `SEARXNG_URL`)

## Quick Start

### Web Search
```bash
/usr/local/bin/uv run {baseDir}/scripts/searxng.py search "query"              # Top 10 results
/usr/local/bin/uv run {baseDir}/scripts/searxng.py search "query" -n 20        # Top 20 results
/usr/local/bin/uv run {baseDir}/scripts/searxng.py search "query" --format json # JSON output
```

### Category Search
```bash
/usr/local/bin/uv run {baseDir}/scripts/searxng.py search "query" --category images
/usr/local/bin/uv run {baseDir}/scripts/searxng.py search "query" --category news
/usr/local/bin/uv run {baseDir}/scripts/searxng.py search "query" --category videos
```

### Advanced Options
```bash
/usr/local/bin/uv run {baseDir}/scripts/searxng.py search "query" --language en
/usr/local/bin/uv run {baseDir}/scripts/searxng.py search "query" --time-range day
```

## Why Full Path to `uv`?

Always use `/usr/local/bin/uv` because the sandbox executor may not inherit your shell's `$PATH`. Explicit paths avoid resolution failures.

## Configuration

The `SEARXNG_URL` environment variable points to your SearXNG instance. This is pre-configured in the sandbox environment — no manual setup needed.

Default: `http://localhost:8080`

## Available Categories

- `general` - General web search (default)
- `images` - Image search
- `videos` - Video search
- `news` - News articles
- `map` - Maps and locations
- `music` - Music and audio
- `files` - File downloads
- `it` - IT and programming
- `science` - Scientific papers

## Troubleshooting

### "uv: command not found"
Always use the full path: `/usr/local/bin/uv run ...`

### Connection to SearXNG fails
1. Verify SearXNG is running: `curl http://localhost:8080`
2. Check `$SEARXNG_URL` is set correctly
3. The instance binds to loopback only — it must be accessed from the same host

### No results returned
1. Try a different category or simpler query
2. Check that search engines are enabled in SearXNG settings (`/opt/searxng/etc/settings.yml`)
