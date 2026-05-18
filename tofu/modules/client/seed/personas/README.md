# Personas

A persona is a pre-formed character an agent ships as. The persona is an
**explicit, required parameter** (`persona`), fully decoupled from the agent
name — one client may run several agents of the same persona under different
names. There is no generic fallback and no agent-name fallback.

## How selection works

`add-agent.sh` prompts for the persona separately from the agent name and
writes it into the SSM record. `seed.tf` normalizes `var.persona` into a
`persona_key`:

```
persona_key = replace(lower(trimspace(persona)), "/[^a-z0-9_-]/", "-")
```

Examples: `"gamer"` → `gamer`, `"Life Coach"` → `life-coach`, `"Coach_01"` →
`coach_01`.

The persona directory is `seed/personas/<persona_key>/`.

## Fail-early contract

`SOUL.md.tftpl` in the matched persona directory is **mandatory**. If
`persona` is empty or no directory matches it, `tofu plan` fails *before any
apply* with:

```
Invalid persona '<key>' (var.persona=<value>) for agent '<name>'. A valid
persona is required — there is no agent-name fallback. Expected
seed/personas/<key>/SOUL.md.tftpl. Available personas: <list>
```

There is no default/generic SOUL. Choose an existing persona, or add the
persona first.

## A persona directory

| File | Required | Effect |
|---|---|---|
| `SOUL.md.tftpl` | **yes** | The collapsed identity + character (name, nature, vibe, signature, values, boundaries). |
| `MEMORY.md.tftpl` | no | Overrides the generic `../MEMORY.md.tftpl` — use to seed persona-specific durable knowledge (it is indexed on first boot). |
| `USER.md.tftpl` | no | Overrides the generic `../USER.md.tftpl`. |

Any file a persona does **not** ship falls back to the generic scaffold in
`seed/`.

Template variables available (rendered via `templatefile`): `${agent_name}`,
`${client_name}`, `${agent_style}`, `${agent_channel}`.

## Content rule (hard requirement)

Persona and seed files are **client-reachable**: they are indexed into the
agent's long-term memory and surfaced back into its context. They must be
**pure character** — describe who the agent *is* and how it behaves.

Never include, in any seed/persona file:

- infrastructure, hosting, models, providers, storage, scaling, sleep/wake
- tooling, the retrieval/memory mechanism, or how anything works internally
- "you wake up fresh", "these files are your memory", "update this file when
  done", or any continuity/onboarding framing

The client must never be exposed to architectural detail. Keep it in-character.

## Write-once caveat

Persona resolves **at agent creation only**. The seed S3 objects use
`ignore_changes`, so renaming an agent, editing a persona, or adding a new
persona does **not** rewrite an existing agent's already-seeded workspace.

## Adding a persona

1. `mkdir seed/personas/<persona_key>/`
2. Add `SOUL.md.tftpl` (start from an existing persona; keep to the content
   rule above).
3. Optionally add `MEMORY.md.tftpl` / `USER.md.tftpl` overrides.
4. Agents named `<persona_key>` (after normalization) now provision as this
   persona.
