---
name: searxng
description: Search the web for a topic via a privacy-respecting SearXNG instance. Use when you need to discover pages, news, images, or sources for a query — not when you already have a URL (use web_fetch for that).
---

# SearXNG Search

Search the web using your local SearXNG instance - a privacy-respecting metasearch engine.

## Prerequisites

1. `uv` (Python package manager) - located at `/usr/local/bin/uv`
2. `python3` - located at `/usr/bin/python3`
3. `SEARXNG_URL` env var pointing at the shared SearXNG Lambda Function URL (injected by the task def — no manual setup)

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

`SEARXNG_URL` is pre-set in the container env by the task definition and points at the shared SearXNG Lambda Function URL. Do not override it.

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
1. Check `$SEARXNG_URL` is set — it should be injected by the task definition
2. Probe the endpoint directly: `curl -sf "$SEARXNG_URL/search?q=test&format=json"`

### No results returned
Try a different category or a simpler query.
