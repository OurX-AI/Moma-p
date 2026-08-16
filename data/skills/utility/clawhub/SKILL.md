---
name: clawhub
description: Search and install agent skills from ClawHub via Skills Hub (global install).
homepage: https://clawhub.ai
metadata: {"momacoder":{"emoji":"🦞"}}
---

# ClawHub

Public skill registry for AI agents. On MomaCoder, ClawHub skills install to the **global** skills directory (`RUNTIME_DATA_DIR/skills`, e.g. `data/skills/`) through **Skills Hub** — not into session workspace.

## When to use

Use when the user asks to find, search, install, update, or list ClawHub community skills (e.g. A-share stocks, web scraping, etc.).

## Preferred: `skill_hub` tool (global install)

Always use `skill_hub` instead of raw `npx clawhub` or filesystem copies.

### Search

```
skill_hub(action="search", query="<keywords>", source="clawhub")
```

Examples: `A股`, `股票`, `web scraping`, `pdf`

### Preview

```
skill_hub(action="inspect", identifier="clawhub:<slug>", include_content=true)
```

### Install

```
skill_hub(action="install", identifier="clawhub:<slug>", force=true)
```

Use `force=true` for community skills when the user confirms. Replace `<slug>` with the slug from search results (e.g. `china-stock-analysis`).

### List Hub-installed skills

```
skill_hub(action="list_installed")
```

### Check / update Hub-installed skills

```
skill_hub(action="check")
skill_hub(action="update", name="<skill_name>", force=true)
```

## Web UI (optional)

Settings → Skills Hub → source **ClawHub** → search → install, or install by identifier `clawhub:<slug>`.

## After install

1. Tell the user to enable the skill in **Agent config → Skill permissions**.
2. Tell the user to **start a new session** so the agent loads the new skill.

## Do not

- Do **not** run `npx clawhub install ... --workdir <workspace>/skills` unless the user explicitly asks for a **session-only** workspace install.
- Do **not** use `skill_hub` with `source=github` when the user means ClawHub — use `source=clawhub`.
- Do **not** claim "no skills found" without calling `skill_hub` with `source=clawhub` first.

## Notes

- Hub ClawHub integration uses the public HTTP API on the **server**; the agent calls `skill_hub`, not shell `npx` directly.
- Identifier format: `clawhub:<slug>` (e.g. `clawhub:china-stock-analysis`).
- Publishing to ClawHub still uses `npx clawhub login` / `publish` if needed.
