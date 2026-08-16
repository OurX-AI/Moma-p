---
name: cron
description: Schedule reminders and recurring agent tasks via the cron tool.
---

Use the `cron` tool to schedule reminders or recurring tasks.

```
cron(action="add", message="Time to take a break!", every_seconds=1200)
cron(action="add", message="Check CI", every_seconds=600, kind="agent", durable=true)
cron(action="add", message="Meeting", at="2026-07-26T15:00:00")
cron(action="add", message="Standup", cron_expr="0 9 * * 1-5", tz="America/Vancouver", recurring=false)
cron(action="list")
cron(action="remove", job_id="...")
cron(action="run_now", job_id="...")
```

- `durable=false` (default): in-memory, dies when process exits.
- `durable=true`: persist to `data/cron/cron.json`.
