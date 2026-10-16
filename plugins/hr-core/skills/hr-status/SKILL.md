---
name: hr-status
description: Show claude-hr health (Headroom proxy, token savings, plugin cost, CLAUDE.md size, running sessions and RAM). Run it to check token usage, cost, or whether Headroom is working.
disable-model-invocation: true
allowed-tools: Bash(claude-hr-ctl status *) Bash(claude-hr-ctl doctor *)
---
Run `claude-hr-ctl status` and summarize it in a few lines. If anything is marked WARN or FAIL, run `claude-hr-ctl doctor` and report the first suggested fix. Do not change any configuration unless the user asks.
