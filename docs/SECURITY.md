# Security and credential model

## What claude-hr protects, and what it cannot

| Asset | Control | Limit |
|---|---|---|
| `.env`, ssh/cloud/gh credentials via Claude's **file tools** | `permissions.deny` baseline (`credentials.block_secret_reads`) | Deny rules are not a boundary for Bash or for scripts that open files themselves |
| Secrets printed by **Bash / PowerShell** into the transcript | `PreToolUse` guard in `hr-core` (`claude-hr-hook bash-guard`, ~0.15 s per call; blocks printing or exfiltrating `.env`, private keys, cloud/gh credential files, `printenv`/`env` dumps, `echo $TOKEN`, `gh auth token`, `op read`, `/proc/*/environ`; allows `cp .env.example .env`, `process.env`, public keys, `ssh/config`) + deny `Bash(printenv *)`, `Bash(env)` | Pattern-based; a determined indirect read (`python -c 'print(open(".env").read())'`) is not caught. Keep the OS-level sandbox on where it exists |
| Secrets already in plaintext transcripts (`~/.claude/projects/**/*.jsonl`) | `creds scan` (weekly job too): token-shaped strings, reported masked, never printed | Detects well-known token shapes only. Rotate anything found, then delete the file |
| Auth shadowing | `creds doctor` warns when `ANTHROPIC_API_KEY` overrides the subscription login | — |
| Plaintext secret stores | `creds doctor`: `settings.json` env, literal MCP env values, `gh` token in `hosts.yml`, loose permissions on `.credentials.json` (POSIX) | Windows file ACLs are inherited from the profile; not inspected |
| A repository's own commands (`.claude/hr-verify.toml`, run unattended at the end of every turn) | Never run until a person trusted the file's exact bytes (`verify trust`, SHA-256 per repository, any edit revokes it). `verify trust` has no `--yes` and refuses a shell that belongs to a Claude Code session or has no terminal; `verify run` of an untrusted recipe is refused the same way; the bash guard denies an agent that types `verify trust` / `untrust` or touches the trust record; the baseline asks before Claude edits the recipe or `.claude/settings*.json` and denies edits of `~/.claude-hr/state`. Checks run without a shell, with a deny-list-scrubbed environment (tokens, `ANTHROPIC_*`, `SSH_AUTH_SOCK`), a per-check and a total time limit, the process tree killed on timeout, executables looked up only on the scrubbed PATH (never in the repo) | Pattern checks, not a boundary: an agent that allocates a pty, obfuscates the command or runs with permission prompts off can still get a recipe trusted. The permission prompts, your own review of `.claude/hr-verify.toml`, and running untrusted repos in a VM are the real controls |
| Untrusted repository agent surface (hooks, `enableAllProjectMcpServers`, `apiKeyHelper`, hidden Unicode, injection text in CLAUDE.md and skills, risky workflows, committed secrets) | `claude-hr-ctl audit` and the preflight shown by `verify trust`: read-only, never executes repository code, findings never echo repository text, every external scanner runs with a scrubbed environment and a time limit | Pattern checks, not proof; working tree only, not git history; obfuscation beyond base64 / pipe-to-shell is not covered; scanners reach their own services (npm registry, OSV) and read repo-supplied config |
| Destructive git commands | The bash guard asks before `--no-verify`, force-push of a protected branch, `reset --hard`, `clean -f`, `checkout .` (`security.profile = "strict"`: refuses); `gh auth token` and `gh auth status --show-token` are blocked | A regex is not a boundary (`git -C . push`, `sh -c ...` get past it). Server-side branch protection is the control |
| LLM reviews | `security-guidance`'s per-turn Opus review is off by default (`security.stop_review`); `/security-review`, `/code-review` and the `hr-review-changes` workflow run on request | Advisory only and prompt-injectable: never run an LLM review with write access on untrusted pull requests; deterministic checks and CI are the merge gate |
| Headroom CCR store | Holds **plaintext originals of compressed tool output** (`~/.claude-hr/headroom`); doctor checks mode 700 on POSIX | Treat the directory like the transcripts |
| Third-party plugin code | Plugins run with your privileges. Plugins come from Anthropic's SHA-pinned catalog and this repo (third-party entries such as Ponytail are vendored there pinned to a 40-char commit); the one exception is `headroom-snip` from Headroom's own repository. `plugins sync` never installs unlisted plugins. Unattended updates never pass `-y`, so a marketplace-declared command is never auto-accepted | Updates follow Anthropic's pins; review `plugins audit` after updates |
| Update supply chain | 5-day cooldown on Headroom releases; Claude Code releases publish per-platform SHA-256 checksums and a detached signature (the installer verifies them); `uv` resolves from PyPI | A compromised release older than the cooldown still installs. Pin versions in regulated environments |
| Network exposure | The proxy binds `127.0.0.1` by default; any other `headroom.host` is your explicit choice and needs Headroom's `HEADROOM_PROXY_TOKEN`. Health checks ignore `HTTP(S)_PROXY` for loopback | — |
| Telemetry | claude-hr sends nothing. Headroom beacon off; Claude error reporting off; `claude.privacy = "strict"` also disables telemetry and non-essential traffic | `strict` removes flag-gated features |

## The end-of-turn gate and trust
`claude-hr-hook stop` (plugin `hr-gates`) formats the files Claude changed with the repo's own configured formatter and runs the
repo's trusted fast checks. It adds nothing to the context unless a trusted check failed, never blocks on its own errors
(every failure exits 0), skips subagents and plan mode, and keeps a metadata-only trail in `~/.claude-hr/audit` (month files,
owner-only, no code, no output, 90 days). The formatter step only uses a tool the repo configures (ruff, black, prettier, biome,
gofmt, rustfmt), refuses a formatter binary that is committed in the repo, and rewrites a tracked file only when its committed
version was already clean. The test step is the trust decision described in the table above: **the recipe is the repository's
text, so approving it is approving code execution on every turn.** Read it before `verify trust`; `claude-hr-ctl audit` shows
what else in that repository touches the agent surface.

## Where credentials come from (recommended order)

1. **Claude Code**: interactive `/login` once per VM (subscription), or on a headless VM `claude setup-token` and supply the
   token as `CLAUDE_CODE_OAUTH_TOKEN` from your secret manager. Do not share one person's token across a team. Do not leave
   `ANTHROPIC_API_KEY` exported unless you intend API billing. Never put tokens in `settings.json` `env`.
2. **Injecting secrets**: set `credentials.op_env_file` and claude-hr launches `op run --env-file=<file> -- claude …`, so the
   secrets exist only in the child's environment. Optionally add `claude`'s `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` via
   `security.profile = "strict"` (strips credentials from Bash/hook/MCP subprocesses; it deliberately keeps `GITHUB_TOKEN`).
3. **GitHub**: `gh auth login` (OS keyring) + `gh auth setup-git`; headless: a fine-grained PAT scoped to the repositories needed,
   read-only by default, 30–90 day expiry; or a per-VM SSH deploy key. Classic and org-wide PATs are not used.
4. **MCP**: prefer OAuth; otherwise `${VAR}` expansion; never `claude mcp add --env TOKEN=literal`.

## Private forks: the clone PAT
For a private fork only, `CLAUDE_HR_GIT_TOKEN` (installer clone only) is passed as a per-command `http.extraheader`; it is not written to `.git/config`,
shell history (when exported from a secret manager), or any claude-hr file. The scheduled self-update uses whatever git
credentials the VM already has (SSH deploy key recommended). If it cannot fetch, it logs `no release tag yet`/fetch failure
and leaves the installed version running.

## Running `--dangerously-skip-permissions`
It stays available (it is an intended workflow). Deny rules and the Bash guard still apply in that mode. Use it in a VM or
container, not as root, not on a machine holding production credentials. `security.profile = "strict"` disables the flag
fleet-wide through `disableBypassPermissionsMode`.

## Reporting
Report vulnerabilities privately through GitHub: the repository's **Security** tab, then **Report a vulnerability**
(https://github.com/timchrist49/claude-hr/security/advisories/new). If that is unavailable, open an issue that says only that
you need a private channel and leave out the details. Do not paste transcripts or `creds scan` file lists into issues
without redacting.
