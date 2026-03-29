"""HTTP listener for agent self-sleep.

Binds to 0.0.0.0:18790 and exposes a single endpoint:

    POST /sleep  — runs /usr/local/bin/clawless-sleep on the host

The sandbox container reaches this via host.docker.internal:18790
(resolves to the Docker bridge IP, not 127.0.0.1).
No authentication — Lightsail firewall blocks all inbound traffic,
so only localhost and Docker containers can reach this port.
"""

import asyncio
import subprocess

from aiohttp import web

SLEEP_CMD = "/usr/local/bin/clawless-sleep"


async def handle_sleep(request: web.Request) -> web.Response:
    proc = await asyncio.create_subprocess_exec(
        SLEEP_CMD,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        return web.json_response(
            {"ok": False, "error": stderr.decode().strip()},
            status=500,
        )
    return web.json_response({"ok": True, "message": stdout.decode().strip()})


app = web.Application()
app.router.add_post("/sleep", handle_sleep)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=18790)
