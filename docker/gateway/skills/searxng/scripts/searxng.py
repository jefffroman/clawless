#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""Thin CLI over a SearXNG JSON endpoint. Stdlib only — `uv run` just uses it
as an isolated interpreter. SEARXNG_URL env must point at the instance root
(the SearXNG Lambda Function URL in Fargate, http://localhost:8080 locally)."""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request


def fetch(base_url: str, params: dict) -> dict:
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{base_url.rstrip('/')}/search?{qs}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def render_text(payload: dict, limit: int) -> str:
    results = payload.get("results", [])[:limit]
    if not results:
        return "(no results)"
    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "(no title)")
        url = r.get("url", "")
        content = (r.get("content") or "").strip().replace("\n", " ")
        lines.append(f"{i}. {title}\n   {url}")
        if content:
            lines.append(f"   {content}")
    return "\n".join(lines)


def cmd_search(args: argparse.Namespace) -> int:
    base = os.environ.get("SEARXNG_URL")
    if not base:
        print("error: SEARXNG_URL not set in environment", file=sys.stderr)
        return 2
    params = {
        "q": args.query,
        "format": "json",
        "categories": args.category,
        "language": args.language,
        "time_range": args.time_range,
    }
    try:
        payload = fetch(base, params)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if args.format == "json":
        trimmed = dict(payload)
        trimmed["results"] = payload.get("results", [])[: args.num]
        print(json.dumps(trimmed, indent=2))
    else:
        print(render_text(payload, args.num))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="searxng")
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search", help="run a web search")
    s.add_argument("query")
    s.add_argument("-n", "--num", type=int, default=10)
    s.add_argument("--format", choices=["text", "json"], default="text")
    s.add_argument("--category", default="general")
    s.add_argument("--language", default=None)
    s.add_argument("--time-range", default=None)
    s.set_defaults(func=cmd_search)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
