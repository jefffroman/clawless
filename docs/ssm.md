# SSM Parameters

All operational state for Clawless lives in AWS Systems Manager Parameter Store under two prefixes: `/clawless/version` (global) and `/clawless/clients/*` (per-agent). This page is the authoritative reference.

## Namespace

```
/clawless/
├── version                                               — git ref the lifecycle Lambda clones
└── clients/
    └── {client_slug}/
        └── {agent_slug}                                  — agent config record (SecureString JSON)
            ├── /active                                   — "true" | "false" sleep toggle
            ├── /error                                    — lifecycle taint marker (present only on failure)
            └── /verbose                                  — "true" to enable verbose gateway logging (optional)
```

`{client_slug}` and `{agent_slug}` are both lowercase slugs produced by `scripts/add-agent.sh` (`[a-z0-9-]+`). The composite `{client_slug}/{agent_slug}` is the globally-unique agent identity used throughout the codebase.

## Parameters

### `/clawless/version`

| | |
|---|---|
| Type | String |
| Purpose | Git ref (tag, branch, or SHA) the lifecycle Lambda clones when running `tofu apply` |
| Writers | `scripts/bootstrap.sh` (initial), operator (manual updates) |
| Readers | `lambda/handler.py` (at every lifecycle invocation) |
| IAM | Lambda role: `ssm:GetParameter` |

See [versioning.md](versioning.md) for the full versioning policy.

### `/clawless/clients/{client_slug}/{agent_slug}`

The agent config record. Stored as a **SecureString** because it embeds channel credentials.

| | |
|---|---|
| Type | SecureString (JSON blob) |
| Purpose | Per-agent configuration consumed by tofu to build the task def |
| Writers | Lifecycle SFN `WriteSSM` step (driven by `add-agent.sh` and storefront provisioning) |
| Readers | `tofu/clients.tf` (at every apply), `lambda/handler.py`, `lambda/wake_listener.py` |
| IAM | Lambda roles: `ssm:GetParameter` on `/clawless/clients/*` |

**JSON schema** (keys consumed by `tofu/main.tf`):

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `client_name` | string | yes | — | Human-readable customer name |
| `agent_name` | string | yes | — | Human-readable agent name |
| `agent_channel` | string | yes | — | `telegram`, `discord`, or `slack` |
| `channel_config` | object | yes | `null` | Channel-specific config; contains bot token(s), `dmPolicy`, `allowFrom` |
| `agent_style` | string | no | `""` | Reserved for per-agent persona tuning (not yet consumed) |
| `bedrock_model` | string | no | `bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0` | Bedrock model string, passed as `CLAWLESS_MODEL` env var |

Any key not listed here is ignored silently — `tofu/main.tf` reads only the keys it knows about via `try(each.value.<key>, <default>)`.

### `/clawless/clients/{client_slug}/{agent_slug}/active`

| | |
|---|---|
| Type | String (`"true"` / `"false"`) |
| Purpose | Sleep/wake toggle — drives the ECS service `desired_count` via tofu (0 when `"false"`, 1 otherwise) |
| Writers | `scripts/sleep-agent.sh`, `scripts/wake-agent.sh`, `wake_listener.py` (on channel message), the agent itself via the sleep skill |
| Readers | `tofu/clients.tf` (via the `aws_ssm_parameters_by_path` data source) |
| IAM | Lambda role: `ssm:PutParameter`. Task role: `ssm:PutParameter` on its own `/active` only (self-sleep). |

Stored as a separate parameter (rather than a key in the agent blob) so the task role's put-permission can be scoped to this exact path.

### `/clawless/clients/{client_slug}/{agent_slug}/error`

| | |
|---|---|
| Type | String |
| Purpose | Lifecycle taint marker — written when a `tofu apply` for the agent fails, so the next lifecycle invocation skips it and surfaces the failure to the operator |
| Writers | `lambda/handler.py` (on tofu failure) |
| Readers | Operator (`aws ssm get-parameter` when debugging); `lambda/handler.py` (to detect tainted state) |
| IAM | Lambda role: `ssm:PutParameter` |

Present only after a failed apply. Clear with `aws ssm delete-parameter` once the underlying issue is fixed. See [troubleshooting.md](troubleshooting.md).

### `/clawless/clients/{client_slug}/{agent_slug}/verbose` *(optional)*

| | |
|---|---|
| Type | String (`"true"` to enable; absent or any other value disables) |
| Purpose | Debug toggle — when present, the gateway entrypoint exports `CLAWLESS_VERBOSE=1`, raising the gateway logger to DEBUG (visible in CloudWatch) |
| Writers | Operator, manually |
| Readers | `docker/gateway/entrypoint.sh` at container boot |
| IAM | Task role: `ssm:GetParameter` on its own `/verbose` only |

Present only when debugging. Because the entrypoint reads it at boot, flipping the flag requires a sleep/wake cycle (or `aws ecs update-service --force-new-deployment`) to take effect. Remove the parameter (or set it to anything other than `"true"`) to return to default logging.

Enable:

```bash
aws ssm put-parameter \
  --name /clawless/clients/{client}/{agent}/verbose \
  --type String --value true --overwrite --region us-east-1
```

Disable:

```bash
aws ssm delete-parameter \
  --name /clawless/clients/{client}/{agent}/verbose --region us-east-1
```

## IAM scope summary

| Principal | Action | Resource |
|---|---|---|
| Lifecycle Lambda | `ssm:GetParameter` | `/clawless/version` |
| Lifecycle Lambda | `ssm:GetParameter`, `ssm:GetParametersByPath`, `ssm:PutParameter` | `/clawless/clients/*` |
| Wake listener Lambda | `ssm:GetParameter` | `/clawless/clients/*` |
| Wake listener Lambda | `ssm:PutParameter` | `/clawless/clients/*/active` |
| Task role (per agent) | `ssm:PutParameter` | `/clawless/clients/{slug}/active` |
| Task role (per agent) | `ssm:GetParameter` | `/clawless/clients/{slug}/verbose` |

Task roles are scoped to the agent's own paths only — one agent cannot read or modify another's state.
