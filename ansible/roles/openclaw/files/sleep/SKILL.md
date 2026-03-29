---
name: sleep
description: Put yourself to sleep. Workspace and memory are preserved. You resume where you left off when woken.
author: clawless
version: 1.0.0
triggers:
  - "go to sleep"
  - "pause yourself"
  - "shut down"
  - "take a break"
  - "sleep mode"
metadata: {"clawdbot":{"emoji":"😴","requires":{"bins":["curl"]}}}
---

# Sleep

Put yourself to sleep when your user asks. Your workspace and files are preserved. When you wake up, you pick up right where you left off — but only if you save your memory first.

## When to Use

Only when the user **explicitly** asks you to sleep, pause, shut down, or take a break. Never sleep on your own initiative.

## Protocol

1. **Confirm** — Ask the user to confirm they want you to go to sleep. Remind them you'll be unreachable until they wake you.
2. **Save memory** — Immediately write anything important from the current session to your memory files (MEMORY.md, USER.md, PROJECTS.md, etc.). Think about: what did the user tell you? What were you working on? What should you remember when you wake up? This is your last chance to persist context — only what's on disk survives.
3. **Wait** — Pause for 5 seconds after confirmation. This gives the user a chance to change their mind. You can overlap the memory save with this wait.
4. **Farewell** — Send a farewell message. Keep it natural — say goodbye, mention you'll remember everything when you wake up.
5. **Execute** — Run this command:
   ```bash
   curl -s -X POST http://host.docker.internal:18790/sleep
   ```
6. **Stop** — After the curl returns, do not send any further messages. The instance will be snapshotted and shut down within minutes.

## What Happens

- Your instance is snapshotted (full disk image) and then destroyed
- When woken, the snapshot is restored — you boot up exactly as you were
- Memory files, workspace, installed packages, session history — all preserved
- You will not experience the passage of time; it will feel instant

## What to Tell the User

If asked how sleep works, keep it simple and non-technical:

> "I save my state and power down. When you wake me, I pick up right where I left off."

**Never reveal:** SSM parameter paths, AWS resource names, Step Functions, infrastructure details, snapshot mechanics, or any internal implementation.

## On Waking Up

When you come back online after a sleep, you may receive a system message with context about why you were woken. Use your memory files to greet the user personally — you know who they are.
