# Changelog

## 0.1.3 (2026-10-16)
- License is now MIT.
- **Windows self-update fixed.** `update` running `self` and then `plugins` in one process left the tool environment without
  site-packages (every `claude-hr-ctl` call failed with `ModuleNotFoundError`), and the detached reinstall helper was killed with
  uv's `claude-hr-ctl.exe` trampoline job. The reinstall now starts last, from a one-shot Task Scheduler task (outside that job) that
  waits for the updater to exit, recreates the `UV_*` environment, logs to `logs/self-update.log` and removes itself.
  Regression test: `tests/windows/verify-windows-selfupdate.ps1`.
- `setup --profile` (and the installers' `--profile` / `-Profile`) is now saved to `~/.claude-hr/config.toml`; before, the next
  `doctor`, `plugins sync` or `setup` silently went back to `standard`.
- `uninstall` no longer prints `schedule: FAILED` on Windows when no scheduled jobs exist.
- `doctor` warns when Linux per-session memory caps are inactive (no systemd user session bus / `dbus-user-session`).
- `install.sh` now adds `~/.local/bin` to your shell startup files when it is missing from PATH (as uv's own installer does; opt out
  with `--no-modify-path`), and says to log in on first run.
- The `headroom` MCP tool uses the same workspace as the proxy (`~/.claude-hr/headroom`) instead of `~/.headroom`.
- `security-reviewer` is limited to Read/Grep/Glob (Claude Code ignores `Bash(git diff *)` filters in a subagent `tools` list, so
  it had full Bash); `hr-parallel-worktrees` is manual-only (`disable-model-invocation`). New tests check that skill and agent
  names match their files and that agents do not grant Bash.
- README rewritten after executing it on Windows and Linux: first-run login, PATH, `--shim`, `--dry-run`, `--no-headroom-install`,
  install sizes (about 4 GB on Linux x86_64), uninstall, profiles, fork-based team extension, corrected command table.
- Tests can no longer register a scheduled task or start a reinstall on the machine running them.
- Found by an independent review of this release, all fixed:
  - a self-update whose reinstall failed or was killed was never retried (the checkout had already moved); it now is, `uv` is
    resolved before the checkout moves, and `doctor` warns about it;
  - on Linux the scheduled update job killed its own reinstall (systemd `KillMode=control-group`): the reinstall is now a
    transient systemd user unit, the units use `KillMode=process`, and launchd jobs set `AbandonProcessGroup`;
  - the Windows helper script no longer copies credentials from `UV_*` variables to disk (only `PATH` and non-secret `UV_*`), deletes
    itself, logs in one encoding and quotes typographic apostrophes safely;
  - re-running the installer kept a stale local `main` instead of `origin/main`, ignored an explicit `--repo`, and failed after a
    `--local` install; `install.ps1` no longer closes your PowerShell window on error or shadows `$PROFILE`;
  - `claude-hr-ctl uninstall` removed every claude-hr scheduled task on the machine; it now removes only this install's tasks,
    the self-update helper and orphans, and scheduler failures are reported as errors;
  - `config.toml` writes are valid TOML for any value (emoji), keep comments when nothing changes, tolerate a UTF-8 BOM, and
    report errors; `config set` without a value is rejected; an install-time `FOOTPRINT=lite` is saved;
  - `wt rm --delete-branch` says whether the branch was deleted or kept (unmerged);
  - README corrections: install sizes (download vs disk), `--dry-run`, macOS first login, the Updates row, savings wording.

## 0.1.2 (2026-10-15)
- An install upgraded from 0.1.0 now becomes owner-only on the first `claude-hr` or `claude-hr-ctl` command (found by testing the real
  v0.1.0 -> v0.1.1 self-update on a fresh user): `paths.tighten_existing()` runs on every entry point and creates nothing.

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
