"""
Clawless wake listener Lambda.

Receives Telegram webhook POSTs for paused agents.  When a user messages a
sleeping agent, this Lambda queues the message in DynamoDB, sets the agent's
/active flag to true, and triggers the lifecycle state machine to resume the
instance.

Deployed as a zip-packaged Lambda with a public Function URL (AUTH_TYPE=NONE).
Telegram identifies the target agent via the X-Telegram-Bot-Api-Secret-Token
header, which encodes the resource slug (set during setWebhook by the
lifecycle Lambda).
"""

import datetime
import json
import os
import urllib.parse
import urllib.request

import boto3
from botocore.exceptions import ClientError

dynamodb = boto3.client("dynamodb")
ssm = boto3.client("ssm")
sfn = boto3.client("stepfunctions")

WAKE_TABLE = os.environ["WAKE_MESSAGES_TABLE"]
SFN_ARN = os.environ["LIFECYCLE_SFN_ARN"]
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

TTL_DAYS = 7


def lambda_handler(event, context):
    # ── Extract slug from secret_token header ────────────────────────────
    headers = event.get("headers", {})
    resource_slug = headers.get("x-telegram-bot-api-secret-token", "")
    if not resource_slug:
        print("Missing secret_token header — rejecting")
        return {"statusCode": 401, "body": "Unauthorized"}

    # secret_token is the resource slug "client-agent"; agent path is "client/agent"
    parts = resource_slug.split("-", 1)
    if len(parts) != 2:
        print(f"Invalid slug format: {resource_slug}")
        return {"statusCode": 400, "body": "Bad slug"}
    agent_path = f"{parts[0]}/{parts[1]}"
    print(f"Wake request for {agent_path}")

    # ── Parse Telegram Update ────────────────────────────────────────────
    body = event.get("body", "{}")
    if event.get("isBase64Encoded"):
        import base64
        body = base64.b64decode(body).decode("utf-8")

    try:
        update = json.loads(body)
    except json.JSONDecodeError:
        print(f"Invalid JSON body")
        return {"statusCode": 200, "body": "ok"}

    message = update.get("message", {})
    text = message.get("text", "")
    sender = message.get("from", {})
    sender_name = sender.get("first_name", "User")

    if not text:
        print("No message text — ignoring (might be a media message or service update)")
        return {"statusCode": 200, "body": "ok"}

    print(f"Message from {sender_name}: {text[:100]}")

    now = datetime.datetime.now(datetime.timezone.utc)
    now_iso = now.isoformat()
    ttl = int(now.timestamp()) + (TTL_DAYS * 86400)

    msg_entry = {
        "M": {
            "text": {"S": text},
            "sender_name": {"S": sender_name},
            "timestamp": {"S": now_iso},
        }
    }

    # ── Check if agent is already waking ─────────────────────────────────
    try:
        existing = dynamodb.get_item(
            TableName=WAKE_TABLE,
            Key={"slug": {"S": agent_path}},
            ConsistentRead=True,
        )
    except ClientError as e:
        print(f"DynamoDB GetItem failed: {e}")
        existing = {}

    if "Item" in existing:
        # Agent already waking — append message, skip SFN
        print(f"Agent {agent_path} already waking — appending message")
        try:
            dynamodb.update_item(
                TableName=WAKE_TABLE,
                Key={"slug": {"S": agent_path}},
                UpdateExpression="SET messages = list_append(messages, :new_msg), #ttl = :ttl",
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={
                    ":new_msg": {"L": [msg_entry]},
                    ":ttl": {"N": str(ttl)},
                },
            )
        except ClientError as e:
            print(f"DynamoDB append failed: {e}")
        _send_telegram_reply(agent_path, sender.get("id"), "_(Got it — still waking up!)_")
        return {"statusCode": 200, "body": "ok"}

    # ── First wake message — write to DynamoDB ───────────────────────────
    print(f"First wake message for {agent_path} — triggering resume")
    try:
        dynamodb.put_item(
            TableName=WAKE_TABLE,
            Item={
                "slug": {"S": agent_path},
                "messages": {"L": [msg_entry]},
                "ttl": {"N": str(ttl)},
            },
        )
    except ClientError as e:
        print(f"DynamoDB PutItem failed: {e}")

    # ── Set /active = true ───────────────────────────────────────────────
    ssm_name = f"/clawless/clients/{agent_path}/active"
    try:
        ssm.put_parameter(Name=ssm_name, Value="true", Type="String", Overwrite=True)
        print(f"Set {ssm_name} = true")
    except ClientError as e:
        print(f"SSM PutParameter failed: {e}")

    # ── Invoke lifecycle SFN ─────────────────────────────────────────────
    sfn_input = {
        "name": f"/clawless/clients/{agent_path}",
        "operation": "Update",
        "time": now_iso,
    }
    try:
        sfn.start_execution(
            stateMachineArn=SFN_ARN,
            input=json.dumps(sfn_input),
        )
        print(f"SFN execution started for {agent_path}")
    except ClientError as e:
        print(f"SFN invocation failed: {e}")
        _alert(f"Wake listener failed to invoke SFN for {agent_path}: {e}")

    # ── Reply to user on Telegram ────────────────────────────────────────
    _send_telegram_reply(agent_path, sender.get("id"), "_(Waking up — give me a few minutes)_")

    return {"statusCode": 200, "body": "ok"}


def _send_telegram_reply(agent_path, chat_id, text):
    """Send a reply via Telegram Bot API using the bot token from SSM."""
    if not chat_id:
        return

    try:
        param = ssm.get_parameter(
            Name=f"/clawless/clients/{agent_path}",
            WithDecryption=True,
        )
        config = json.loads(param["Parameter"]["Value"])
        bot_token = (config.get("channel_config") or {}).get("botToken")
    except (ClientError, json.JSONDecodeError, KeyError) as e:
        print(f"Could not read bot token for {agent_path}: {e}")
        return

    if not bot_token:
        print(f"No bot token for {agent_path} — skipping reply")
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}).encode()

    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"Telegram reply sent: {resp.status}")
    except Exception as e:
        print(f"Telegram sendMessage failed: {e}")


def _alert(message):
    """Publish to SNS alerts topic."""
    if not SNS_TOPIC_ARN:
        return
    try:
        import boto3
        sns = boto3.client("sns")
        sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject="Clawless Wake Listener Alert",
            Message=message,
        )
    except Exception as e:
        print(f"SNS alert failed: {e}")
