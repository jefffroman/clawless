---
name: memory-curation
description: Curate your long-term memory in MEMORY.md and dated archives under memory/. Use at the end of a session, before going to sleep, or whenever the user asks you to remember, save, archive, or reorganize what you've learned so future-you can pick up where you left off.
---

# Memory Curation

Your memory lives in Markdown files. MEMORY.md is the working set — what you need for day-to-day context. `memory/YYYY-MM-DD.md` files are the archive — older knowledge that's still recoverable but not cluttering your active context. The vector index (L2/L3) is rebuilt automatically from MEMORY.md every 5 minutes; you don't manage it directly.

## When to Curate

**After every session:** Before the conversation ends, review what happened and write anything worth keeping to MEMORY.md. This is not optional. Knowledge that isn't written down is lost — you have no implicit memory between sessions.

**When MEMORY.md gets large:** If MEMORY.md exceeds roughly 300 lines (`wc -l MEMORY.md`), archive older or less-active sections to a daily log. A bloated MEMORY.md wastes context window and dilutes search relevance.

**When the user asks:** "Remember this", "save this", "don't forget" — write it immediately.

## Writing to MEMORY.md

1. Read MEMORY.md first — understand the current structure before editing
2. Place new knowledge in the appropriate section (Projects, Key Lessons, Architecture, Blockers)
3. Write concise bullets, not prose — every token costs context window
4. Update the **Keyword Index** at the top with new terms and the section they appear in
5. Don't run `indexer.py` manually — the systemd timer picks up changes within 5 minutes

### What to Save

In priority order:

1. **User corrections and preferences** — how they want things done, what they dislike, explicit "remember this" requests
2. **Project state changes** — decisions made, blockers discovered, milestones hit
3. **Technical architecture** — system relationships, gotchas, non-obvious dependencies
4. **Lessons learned** — what went wrong and why, what worked unexpectedly

Do NOT save: routine greetings, transient debugging output, things already obvious from the codebase or files on disk.

## Archiving Protocol

When MEMORY.md needs pruning, **never delete without archiving first**.

1. Create or append to `memory/YYYY-MM-DD.md` (today's date)
2. If the file is new, add a header: `# Memory Archive — YYYY-MM-DD`
3. Copy the sections you're archiving into the daily log — full content, not summaries
4. Verify the daily log write succeeded before touching MEMORY.md
5. Remove the archived sections from MEMORY.md
6. Add a cross-reference to the Keyword Index:
   ```
   - deployment-migration: archived to memory/2026-03-28.md
   ```

### What to Archive vs. Keep

**Keep** in MEMORY.md:
- Identity, mission, user preferences — these are always relevant
- Active projects and current blockers
- Recent lessons (last 2-3 weeks)

**Archive** to daily logs:
- Completed projects (keep a one-line summary in MEMORY.md, details to archive)
- Resolved blockers
- Older lessons that are unlikely to come up again soon

## Keyword Index Format

The Keyword Index at the top of MEMORY.md is a lookup table — it tells you (and the vector search) where to find things, including archived content.

```markdown
## Keyword Index
- deployment: ## Architecture
- user-timezone: ## Identity & Mission
- terraform-migration: ## Projects > Infrastructure
- old-auth-bug: archived to memory/2026-03-15.md
```

Keep entries alphabetical. Remove entries when their section is deleted and no archive exists.

## Rules

- Never edit files in `vector_memory/` — that directory is managed by the indexer
- Never delete from MEMORY.md without archiving first
- Never summarize when archiving — copy the full content so nothing is lost
- Always update the Keyword Index when you add or archive a section
