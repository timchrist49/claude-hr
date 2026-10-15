# Operations runbook

## Daily checks
```
claude-hr-ctl status            # proxy, savings, plan usage, sessions, RAM, last-24h tokens, CLAUDE.md size
claude-hr-ctl doctor            # every check with the exact fix; exit 1 if anything is FAIL
claude-hr-ctl doctor --deep     # also measures ~/.claude size (time-boxed)
```

## Turning things off (in order of blast radius)
| Need | Do |
|---|---|
| One session without Headroom | `claude-hr --hr-direct …` (or `CLAUDE_HR_DIRECT=1`) |
| Everything without Headroom | `claude-hr-ctl config set headroom.enabled false` |
| Reinstall while sessions are open (Windows) | blocked on purpose: replacing the tool env under a running launcher breaks it. Close sessions, or `$env:CLAUDE_HR_FORCE_INSTALL=1` |
| Run exactly like stock `claude` | use `claude` (claude-hr never edits its binary; only the optional `shim` shadows it: `claude-hr-ctl shim remove`) |
| Stop scheduled jobs | `claude-hr-ctl schedule remove` |
| Undo all managed config | `claude-hr-ctl uninstall` (your own settings/plugins/CLAUDE.md content stay) |
| Also delete Headroom and `~/.claude-hr` | `claude-hr-ctl uninstall --purge` |

## Updates
```
claude-hr-ctl update --check                 # what is available, changes nothing
claude-hr-ctl update                         # apply now (defers while sessions run; --force to override)
claude-hr-ctl update --only headroom         # one component
```
Daily job: 03:17 local (`updates.at`). Log: `~/.claude-hr/logs/update.jsonl`. Pin instead of tracking:
`claude-hr-ctl config set claude.pin 2.1.292` / `headroom.version 0.40.0`. Release channel for Claude: `claude.channel`
(`stable` ≈ a week behind `latest`; the updater never downgrades you off a newer build unless `--allow-downgrade`).
claude-hr itself updates to the newest `vX.Y.Z` tag (`self.update_ref`); cut a release with `git tag vX.Y.Z && git push --tags`.

**Rollback by hand:** `claude install <version> --force`; `uv tool install --python 3.13 "headroom-ai[all]==<ver>"`;
`git -C ~/.claude-hr/repo checkout <tag> && uv tool install --force --reinstall ~/.claude-hr/repo`.

## Monitoring and alerts
`monitor tick` (every 15 min) writes `state/health.json`, appends `logs/monitor.jsonl`, restarts a dead proxy, and alerts
(once per 6 h per condition) on: low disk, high RAM, large `~/.claude`, proxy down. The weekly `maintain` job also runs
`creds scan` over the last 7 days of transcripts and sends a one-off `transcript-secrets` alert through the same channels.
Channels (set any): `monitor.alert_ntfy_url` (phone push), `monitor.alert_webhook_url`, `monitor.alert_command`
(argv, no shell; text arrives in `CLAUDE_HR_ALERT_TITLE` / `CLAUDE_HR_ALERT_BODY`). Claude's own phone push needs Remote Control,
which is disabled behind a proxy, hence ntfy.

## Scheduler specifics
- **Windows**: Task Scheduler folder `\claude-hr\`, runs `pythonw.exe` (no console window), only while you are logged on.
- **Linux**: `systemd --user` timers. On a headless VM run once as root: `loginctl enable-linger <user>`. Without
  systemd (containers) it falls back to a marked cron block. `systemd-run --user` caps also need `dbus-user-session`.
- **macOS**: LaunchAgents `io.github.timchrist49.claude-hr.*`.

## Resource governor tuning (`~/.claude-hr/config.toml`)
```toml
[resources]
max_sessions = 4            # or "auto"
memory_max_gb = 8           # per session tree; 0 disables hard caps
low_priority = true         # below-normal CPU priority
[headroom]
footprint = "lite"          # skip ML/memory extras (≈ 600 MB less RAM, far smaller download)
```
CPU quotas on Linux need the `cpu` controller delegated to user managers (one-time, root):
`/etc/systemd/system/user@.service.d/delegate.conf` → `[Service]\nDelegate=cpu cpuset io memory pids`, then
`systemctl daemon-reload` and re-login; then set `resources.cpu_quota_pct`.

## Troubleshooting
| Symptom | Cause / fix |
|---|---|
| `[claude-hr] … continuing WITHOUT compression` | Proxy not healthy. `claude-hr-ctl headroom status`, `claude-hr-ctl headroom logs`, `claude-hr-ctl headroom start --wait 180`. First start after install downloads models. |
| Claude says it cannot reach the API | A stale `ANTHROPIC_BASE_URL` in your shell or settings. `claude-hr-ctl doctor` names it. |
| Background sessions not compressed | The Claude daemon was started by a plain `claude`. `claude-hr-ctl daemon status`; `claude-hr-ctl daemon restart` when idle. |
| Subscription limit burning faster than expected | `ANTHROPIC_API_KEY` exported (overrides login — `creds doctor`), or many parallel sessions: lower `resources.max_sessions`. `claude-hr-ctl usage --by project`. |
| `/rc` (Remote Control) unavailable | Disabled by Claude Code behind any custom base URL. `claude-hr --hr-direct`. |
| High RAM | Proxy ~1.2 GB + ~0.3–0.7 GB per session. `footprint = "lite"`, fewer sessions. |
| `~/.claude` huge | `claude-hr-ctl cleanup` (dry-run) then `--apply`. |
| Plugin token cost too high | `claude-hr-ctl plugins audit`; move big ones to project scope (`plugins suggest`). |
| Hook errors after uninstalling claude-hr | None expected: hook commands exit 0 when `claude-hr-hook` is absent. |
| Windows: long-path clone failures in plugin install | `git config --system core.longpaths true`, or shorten `CLAUDE_CONFIG_DIR`. |

## Moving to a new VM
Run the installer; copy `~/.claude-hr/config.toml` if you have local overrides. Nothing else is machine-specific.
For a fleet, put shared overrides in `/etc/claude-hr/config.toml` (Windows `%ProgramData%\claude-hr\config.toml`).
