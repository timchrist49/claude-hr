---
name: hr-parallel-worktrees
description: Run several independent changes in parallel using git worktrees and background sessions. Run it for multiple separate tasks or branches at the same time.
disable-model-invocation: true
---
Only parallelize work that does not touch the same files. If tasks depend on each other, do them in order.

1. For each task, create an isolated worktree: `claude-hr-ctl wt new <branch-name>` (prints the path).
2. Start one background session per worktree: `claude-hr --bg "<self-contained task>"` run from that path. Give each task its own acceptance check.
3. Check progress with `claude-hr agents`; read results with `claude logs <id>`.
4. Merge one branch at a time and run the project's tests after each merge. Remove finished worktrees with `claude-hr-ctl wt rm <branch-name>`.

Respect the session limit: `claude-hr-ctl status` shows how many sessions this machine can carry.
