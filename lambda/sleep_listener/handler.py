"""
Clawless sleep-listener Lambda.

Replaces the Lightsail-era clawless-sleep-listener.py aiohttp service. Runs
behind a Lambda Function URL; the gateway POSTs to it when an agent decides
to sleep. Authenticated by a shared secret header (matched against SSM).

Event shape (Function URL, buffered mode):
    {
        "requestContext": {"http": {"method": "POST", "path": "/sleep"}},
        "headers": {"x-clawless-auth": "..."},
        "body": '{"slug": "client/agent"}' (may be base64-encoded),
        "isBase64Encoded": false
    }
"""

import base64
import json
import os

import boto3
from botocore.exceptions import ClientError

ecs = boto3.client("ecs")
ssm = boto3.client("ssm")

ECS_CLUSTER = os.environ.get("ECS_CLUSTER", "clawless")
AUTH_SSM_PARAM = os.environ.get("AUTH_SSM_PARAM", "/clawless/sleep-listener/token")

_CACHED_TOKEN = None


def _auth_token():
    global _CACHED_TOKEN
    if _CACHED_TOKEN is None:
        resp = ssm.get_parameter(Name=AUTH_SSM_PARAM, WithDecryption=True)
        _CACHED_TOKEN = resp["Parameter"]["Value"]
    return _CACHED_TOKEN


def _resp(status, body):
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }


def _service_name(slug):
    return "clawless-" + slug.replace("/", "-")


def lambda_handler(event, context):
    print(f"Event: {json.dumps({k: v for k, v in event.items() if k != 'body'})}")

    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    supplied = headers.get("x-clawless-auth", "")
    if not supplied or supplied != _auth_token():
        return _resp(401, {"error": "unauthorized"})

    raw_body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw_body = base64.b64decode(raw_body).decode("utf-8")

    try:
        payload = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        return _resp(400, {"error": "invalid json"})

    slug = payload.get("slug", "").strip()
    if not slug or "/" not in slug:
        return _resp(400, {"error": "slug required (format: client/agent)"})

    service = _service_name(slug)
    try:
        ecs.update_service(cluster=ECS_CLUSTER, service=service, desiredCount=0)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "ServiceNotFoundException":
            return _resp(404, {"error": "service not found", "service": service})
        print(f"update_service failed: {e}")
        return _resp(500, {"error": "update_service failed", "detail": str(e)})

    print(f"[sleep:{slug}] desired_count=0")
    return _resp(200, {"status": "sleeping", "slug": slug, "service": service})
