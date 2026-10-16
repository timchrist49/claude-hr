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
| Run without Headroom routing | use `claude` (claude-hr never edits its binary; only the optional `shim` shadows it: `claude-hr-ctl shim remove`). Plain `claude` still reads the managed `settings.json`, the CLAUDE.md block, the plugins and the MCP servers: only the Headroom routing is skipped. To remove those too, run `claude-hr-ctl uninstall`. |
| Stop scheduled jobs | `claude-hr-ctl schedule remove` |
| Undo all managed config | `claude-hr-ctl uninstall` (your own settings/plugins/CLAUDE.md content stay) |
| Also delete Headroom and `~/.claude-hr` | `claude-hr-ctl uninstall --purge` |

## The development loop (`hr-gates`, `verify`, `audit`, `init-repo`, `footprint`)
```
claude-hr-ctl verify status            # is this repository's recipe trusted? what would the gate run?
claude-hr-ctl verify init --write      # propose .claude/hr-verify.toml from what the repo declares; then read it
claude-hr-ctl verify trust             # in a normal terminal, never inside Claude Code
claude-hr-ctl trail --tail 20          # what the end-of-turn gate did (metadata only)
claude-hr-ctl audit [--strict]         # read-only repository audit, zero tokens
claude-hr-ctl init-repo [--apply]      # scaffold CI, Dependabot, issue/PR templates, docs/plans, REVIEW.md
claude-hr-ctl footprint                # always-on token cost of the plugins, against footprint.budget_standard_tokens
```
The gate runs once per turn, only for a git repository, and not at all in plan mode or for subagents. It skips a working
tree that did not change since it last passed. A failing trusted check blocks the turn's end at most `verify.gate_max_blocks`
(2) times for the same tree, then lets Claude stop and names the failure (Claude Code itself gives up after 8 consecutive
blocks). Turn it off: `CLAUDE_HR_GATES=off` for one session, `gates.enabled = false` everywhere, `format.enabled` or
`verify.gate` for one step, `claude-hr-ctl verify untrust` for one repository.

`hr-plan-execute` runs the independent steps of an approved plan in parallel git worktrees. Commit the plan first (worktrees
start from the last commit; the run stops when tracked files are dirty). Run `claude-hr-ctl workflows install`, then ask Claude to
run the `hr-plan-execute` workflow with args `{plan: 'docs/plans/<file>.md', dryRun: true}` to see the groups, and again
without `dryRun`. It uses up to about 25 agents (`maxParallel` 4 by default, 8 at most). Passing groups are merged one by
one on the current branch; failed or conflicting groups are reported and left on their `hr/<planslug>-<group>` branches
(clean up with `claude-hr-ctl wt rm` or `git branch -d`). Its LLM review findings are advisory; CI is the gate.

`init-repo` writes the CI job `verify` (use that name in a branch-protection rule). Its actions are referenced by tag with a
comment to pin them to a commit SHA; the Dependabot file it also writes keeps them updated. Review it before committing.
A token that lacks the `workflow` scope cannot push `.github/workflows/*`.

`doctor` reports: the recipe state of the current repository, a missing `claude-hr-hook` on PATH, a foreign proxy or Headroom
Desktop, and `security-guidance` reviews that were skipped for lack of API credentials.

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

**A self-update that did not finish:** claude-hr's own reinstall runs in the background after `update` exits and logs to
`~/.claude-hr/logs/self-update.log` (Windows: a one-shot Task Scheduler task `selfupdate` in the `claude-hr` folder that deletes
itself and its script; Linux: a transient `claude-hr-selfupdate-*` systemd user unit). If `doctor` warns that the installed
version is behind the checkout, run `claude-hr-ctl update --only self` to retry (the next daily update retries too), or re-run
the installer.

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
| Claude shows a non-blocking hook notice every turn | `claude-hr-hook` is not on PATH in the shell Claude runs in: `uv tool update-shell`, open a new terminal (`doctor` says so). |
| The gate never runs the tests | `verify status`: no recipe, not trusted, or changed since trusted. `verify init --write`, read it, `verify trust` in a terminal. `trail` shows what the last turns did. |
| The gate blocks again and again | After `verify.gate_max_blocks` blocks for the same tree it stops and names the failure; fix it or `verify untrust`. A check that is flaky or slow belongs in the `full` tier, not `fast`. |
| A file was not formatted | `fmt` lists the formatter the repo configures and marks one as `NOT USED` with the reason. A formatter inside the repo (`.venv`, `node_modules/.bin`) or prettier (its configuration can load code) is used only after `verify trust` for that repository (or `format.repo_tools = always`). A tracked file whose committed version was never formatted is left alone on purpose (`format.clean_gate = false` to change that); `trail` records skips. |
| Hook errors after uninstalling claude-hr | None expected: hook commands exit 0 when `claude-hr-hook` is absent. |
| Windows: long-path clone failures in plugin install | `git config --system core.longpaths true`, or shorten `CLAUDE_CONFIG_DIR`. |

## Moving to a new VM
Run the installer; copy `~/.claude-hr/config.toml` if you have local overrides. Nothing else is machine-specific.
For a fleet, put shared overrides in `/etc/claude-hr/config.toml` (Windows `%ProgramData%\claude-hr\config.toml`).
