# Changelog

## 0.1.1 (2026-10-15)
Security hardening from the post-release commit review:
- `~/.claude-hr` and its `state/`, `logs/`, `backups/`, `cache/` folders are now owner-only (0700) on Linux/macOS; they hold the
  proxy log, the transcript-derived `headroom learn` report, usage data and copies of `settings.json`. `install.sh` creates the home
  folder 0700 from the start, and the proxy log is created 0600.
- Files claude-hr owns (learn report, Serena pin, proxy state file, proxy log) are never written or opened through a symlink; a symlink
  planted at such a path is replaced or refused instead of followed. Managed edits to your own `settings.json` still follow a dotfiles symlink.
- The proxy log handle is closed in the launcher after the proxy is spawned.

## 0.1.0 (2026-10-15)
Hardened after an independent review and real-OS testing (Windows 11 incl. PowerShell 5.1 and Task Scheduler, Ubuntu 22.04 with systemd/cgroups):
- Launcher: `claude --help`-derived subcommand/flag classification, best-effort bookkeeping, nesting guard, gateway detection,
  negative cache for a broken proxy, lazy imports (0.78 s -> 0.24 s on Windows), no-login launches direct.
- Windows: real `claude.exe` shim (no `.cmd`), scheduled jobs under `pythonw`, no console flashing, Job Object tests, installer fixes for PS 5.1.
- Updater: forward-only self-update, release tags only, defers while sessions or daemon workers run, rollback restores with the same
  dependency cutoff and is re-verified, 5-day cooldown.
- Guard hook: ~0.16 s per call via `claude-hr-hook`, far fewer false positives, PowerShell tool covered.
- Safety: Headroom stop never signals foreign processes; crontab never overwritten when unreadable; MCP servers only removed if claude-hr added them;
  CLAUDE.md block repair never eats user text; atomic writes follow symlinks and keep modes; cleanup fails closed.

- Initial release: `claude-hr` launcher, `claude-hr-ctl` control CLI, Headroom persistent proxy management, ownership-tracked
  settings merge, CLAUDE.md baseline + linter, plugin profiles + audit, secret-leak guard, resource governor
  (admission, cgroup / Job Object caps), scheduled monitor/update/maintain jobs, staged updater with rollback,
  Windows / Linux (systemd + cron) / macOS (launchd) installers.
