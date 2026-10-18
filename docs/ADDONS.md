# What was added, what is optional, what was rejected

Researched October 2026; stars/dates from the GitHub API at the time. Verdicts reflect *evidence about cost and
quality*, not popularity.

## Included (default)
| Component | Why |
|---|---|
| **Headroom** `headroom-ai[all]` — proxy, SmartCrusher/CodeCompressor/Kompress, CCR, memory and output shaping (both off by default since 0.2.1), `learn`, MCP server, `headroom-snip` plugin | The requested core. All extras that matter for Claude Code are in `[all]`. Framework adapters (`langchain`, `agno`, `strands`, `bedrock`, `anyllm`, `autogen`), `vector` (needs a C++ toolchain), `memory-stack` (Qdrant + Neo4j), `voice-train`, `pytorch-mps` are **not** installed: they serve other stacks or add infrastructure. Add via `headroom.extras` |
| **Serena** (via Headroom's registration, pinned with `headroom.serena_version`) | Symbol-level navigation; one stdio instance per session, started only when a session calls it |
| **Native statusLine** | Model, context %, cost, Headroom savings, 5-hour plan usage; no third-party package |
| **Native usage ledger** (`usage`) | Replaces `ccusage` without an npm dependency; daily snapshot survives Claude's transcript pruning |
| **Anthropic catalog plugins** | `standard`: `claude-md-management`, `security-guidance`, `context7`. `process`: + `superpowers`, `feature-dev`. `audit`: + `claude-security`. `full`: all of those + `skill-creator`. Measured and trimmed 2026-10-16 (see Admission rule) |
| **hr-gates** (plugin, one Stop hook) | Format the files git reports as changed with the repo's own formatter, run the repo's trusted fast checks, keep a metadata-only audit trail. One process per turn, no context cost, fails open |
| **hr-core skills** | `hr-plan`, `hr-done`, `hr-gh`, `hr-audit`, `hr-status`, `hr-parallel-worktrees` are manual-only (`disable-model-invocation`: not in the skill listing, 0 always-on tokens); `hr-claudemd-hygiene` is the only listed skill |
| **Git-safety asks** | The bash guard asks before `--no-verify` (also `git commit -n` in any short-flag cluster, abbreviations such as `--no-veri`, and `git -c core.hooksPath=`), force-push of a protected branch, `reset --hard`, `clean -f`; `gh auth status --show-token` is blocked. The settings baseline asks before Claude edits `.claude/hr-verify.toml` or `.claude/settings*.json`, and before `verify trust` / `verify enable` |
| **Git worktree helper** | Built-in isolation is `--worktree`; the helper adds a predictable path and exclude rule for scripted parallel runs |

## Optional (off by default)
| Component | Notes |
|---|---|
| **RTK** (rtk-ai/rtk, 83k★) | Filters Bash output via a hook. Independent benchmarks (JetBrains: +7.6% cost at low effort; Quesma: +1% / +17%; PointFive: −2.7%, not significant) found no reliable net saving, truncation can cause re-fetch loops, and `rtk init` can write its own `ANTHROPIC_BASE_URL`, which collides with Headroom. If you try it: answer **N** to its proxy prompt, set `RTK_TELEMETRY_DISABLED=1`, A/B it |
| **Ponytail** (DietrichGebert/ponytail, MIT, 164k★) | Headroom's recommended companion for terser output: a "laziest solution that works" behaviour layer (YAGNI, stdlib first). Reported gains are contested (a later agentic benchmark found ~4% less code, not the advertised 54%) and it changes *what the model writes*, so it is **not** default. Available as profile `lean-output` (`plugins.profile = "lean-output"`), vendored in this repo's marketplace pinned to commit `84740e3`; it ships hooks, so re-review the diff before bumping the SHA |
| **Headroom `--code-graph`** | A per-project file watcher: it only works when the proxy runs from a project root, i.e. with `--hr-wrap` / `headroom.mode = "wrap"`. Serena covers semantic navigation in the default shared-proxy mode |
| **CodeGraph** (colbymchenry/codegraph) | Alternative to Serena; vendor-run benchmarks only. Do not run both |
| **ccstatusline** | Pretty, but its usage widgets call the Anthropic usage API with your credentials |
| **LSP code-intelligence plugins** (Anthropic catalog) | Add the one for your main language: post-edit type errors improve first-try quality |
| **Superpowers** | Process-heavy skills (brainstorm -> plan -> TDD -> review). Moved out of `standard` on 2026-10-16: its SessionStart hook injects a whole skill (roughly 0.8-1k tokens, estimated) on every start, clear and compact, which `claude plugin details` does not count, and its text uses the forceful 'ABSOLUTELY MUST' style that Anthropic's Claude 5 prompting notes say over-triggers. Overlaps `hr-plan` / `hr-done` and the bundled `/code-review`. Opt in with profile `process`. Measure with `claude-hr-ctl footprint` |
| **feature-dev**, **code-simplifier** | `feature-dev` (profile `process`) overlaps plan mode and `hr-plan`; `code-simplifier` is redundant with the bundled `/simplify` (listed under [redundant]) |
| **claude-security** (profile `audit`) | Official on-demand deep scan (9 agents, patches, SARIF). Heavy by design, pre-1.0: run it deliberately before a release, never per turn |
| **Language-server plugins** (`pyright-lsp`, `typescript-lsp`, `gopls-lsp`, `rust-analyzer-lsp`) | Per-project: diagnostics after every edit make the model's first try better. Suggested by `plugins suggest` when the server binary is installed; never installed globally |
| **gitleaks** | `creds doctor` detects it; install it as a pre-commit hook in each repo |
| **1Password CLI** | `credentials.op_env_file` |

## Rejected
| Component | Reason |
|---|---|
| claude-mem | Duplicates Claude auto-memory (and Headroom's optional memory); sign-in/cloud sync prompts, paid tier after trial |
| Caveman suite | Overlaps Headroom/RTK; measured output-token reduction ~8.5% vs 65% advertised; its own README shows ~3% beyond "answer concisely" |
| Claude-Code-Usage-Monitor | Stale; native statusLine `rate_limits` + ledger cover it |
| claude-squad / ccmanager / Conductor | `claude agents`, `--bg`, `--worktree` now cover the use case; claude-squad is AGPL and tmux-only; Conductor is a GUI |
| Headroom `--mode token` / `agent-90` profile | Raised the uncached share ~4× and cache busts 9× in a published test; no spend benefit |
| `NODE_OPTIONS=--max-old-space-size` | No effect on the native binary |
| Headroom `headroom` hooks plugin | Spawns Python on every Bash tool call to manage a deployment claude-hr already manages |
| Headroom Enterprise features | Paid; not needed |
| **Headroom Desktop** (gglucass/headroom-desktop, Garm Tech B.V.) | Paid tray app (account + plan required, ~$3-4/month after a trial) that runs its own proxy on :6767 and writes `ANTHROPIC_BASE_URL`, hooks and a status line into the global `~/.claude/settings.json`, re-applying them when the file changes. A settings.json env value replaces the one claude-hr sets per launch, so the two are mutually exclusive; it cannot be pointed at another Headroom install. Independent of the open-source Headroom project (the vendor says it has the creator's endorsement; not confirmed upstream). Reviewed 2026-10-16 from its README, source and site, nothing run. `doctor` warns when it is detected. Revisit if upstream ships a first-party desktop app or the vendor documents coexistence |
| **Everything Claude Code** (affaan-m/ECC, MIT, v2.2.3, commit `c05b2d66`) | Reviewed 2026-10-18 from source; nothing was run. Not malicious, but the wrong shape. The plugin cannot be subset: 293 skills, 68 agents, 94 commands and 24 hook entries load together, and `skillOverrides` does not apply to plugin skills. `footprint` reports 30.9k description tokens, which is an uncapped upper bound; Claude Code caps the skill and command listing, so the real always-on cost is roughly 7-9k tokens at a 200k window and 12-16k at 1M (inferred from the Claude Code binary and docs, not measured), against the 1,500 budget, and the agent list is not capped. Every hook entry starts 2-3 node processes even with hooks switched off in the plugin options (4-18 per tool call or turn). Default hooks keep tool input and output and every Bash command in clear text under the home folder (not under `CLAUDE_CONFIG_DIR`), make `claude -p` calls with transcript text, re-read the whole transcript every turn and run the open repository's formatter and `npx tsc` at Stop; the Bash pre-dispatcher exits 2 on its own errors. The root `.mcp.json` starts an npx-fetched browser server whatever `mcpServers: {}` says. GateGuard's reported gain rests on two self-scored tasks. No skill, agent or command was shown to beat `hr-core`, `hr-gates` or the bundled commands. Taken: its `--no-verify` test cases (the bash guard now covers them). `main` (76aaa77) is newer and was not reviewed. Revisit if upstream ships a hook-free, subsettable plugin |
| **Bun / `bun fmt` as a dependency** | `bun fmt` merged 2026-10-12 and is canary-only (the latest stable Bun predates it); its Prettier-parity figures (99.1%) come from Bun itself; it does not format Python, Go or Rust. claude-hr formats with the formatter each repo already configures and never imposes one |
| **Spec-driven frameworks** (spec-kit, OpenSpec, BMAD, Kiro, Tessl) | Heavy per-feature artifacts, no independent evidence of fewer defects, the best independent review found them verbose for small tasks. `docs/plans/` + `hr-plan` covers the loop |
| **GitHub MCP server by default** | ~52 tools / ~30k tokens when loaded (GitHub's figures); Anthropic says CLI tools such as `gh` are the most context-efficient. `hr-gh` uses `gh` with a read-only allow list |
| **claude-code-security-review Action**, **LLM review of untrusted PRs in CI** | Documented as not hardened against prompt injection and exploited in 2026 (CVE-2026-47751 and others). Deterministic CI checks are the merge gate; LLM review stays local and advisory |
| **Per-edit or per-tool hooks** (format after every edit, protected-files PreToolUse guard) | Each is a Python spawn (0.11-0.16 s on Windows) per tool call. Everything runs once per turn in `hr-gates`; tamper protection uses permission `ask` rules instead (0 spawns) |
| **Model-judged Stop hooks** (prompt / agent hooks) | Add a model call or up to 50 subagent turns per turn; `/goal` already covers per-task model-judged completion |
| **Unattended LLM routines** (scheduled `claude -p` maintenance) | Deferred, not rejected: needs a stored credential, shares the subscription window with interactive work, and no public per-run cost exists. Deterministic maintenance (Dependabot through `init-repo`, the existing weekly `maintain` job) ships; revisit after a measured pilot |

## Workflows (opt-in)
`claude-hr-ctl workflows install` copies `hr-review-changes` and `hr-plan-execute` into the project's `.claude/workflows/`. `hr-review-changes` reviews the diff on up to five dimensions, then has skeptics try to refute every finding (majority-refute drops it) and reports what survives; anything it could not check is logged, never silently dropped. Cost is typically 10-40 agents per run and up to about 85 with the defaults (`hr-plan-execute` is capped at about 25), so neither is part of any default flow. Plugins cannot ship workflows, hence the separate installer.

## Enabling an optional piece
Most are one-liners in `~/.claude-hr/config.toml`, e.g.
```toml
[plugins]
profile = "full"
[headroom]
extras = "all"               # or "proxy,code,ml,memory,mcp,vector" etc.
code_memory = "none"         # disable Serena
```
then `claude-hr-ctl setup`.

## Admission rule for a new component (checked by tests/test_repo_hygiene.py)
1. Not already bundled in Claude Code (`/code-review`, `/simplify`, `/security-review`, `/verify`, `/batch`, `/doctor`, `/skill-doctor`, ultrareview). If it is, document the command instead of adding a plugin.
2. A CLI or a silent hook cannot do the job at zero model context (`gh`, `git`, a formatter, a scanner).
3. Skills that have side effects are `disable-model-invocation: true`; descriptions stay at or under 300 characters and avoid ALWAYS / MUST / CRITICAL.
4. Hooks never add context unless something failed, run once per turn (not per tool call), exit 0 on every error, and are measured with `claude-hr-ctl footprint`.
5. It fits the profile's always-on budget (`footprint.budget_standard_tokens`), counting what hooks inject and not only what `claude plugin details` reports.
6. Third-party code is read before it is admitted, not trusted from its description. Check: every `hooks.json` entry and what it starts (inline `node -e` bootstraps, `claude -p`, formatters, `npx`); whether an off or minimal switch avoids the process start or only the work; paths written outside `CLAUDE_CONFIG_DIR`; any handler that exits 2 or fails closed on its own error; unpinned `npx` / `uvx` in skills, agents, commands and MCP configs; work done per turn against transcript size (some hooks re-read the whole transcript); what the plugin itself discloses about its hooks; `claude-hr-ctl footprint` and `claude plugin details` for the description cost. `claude-hr-ctl audit` does not read a plugin layout (root `skills/`, `commands/`, `agents/`, `hooks/hooks.json`): copying a candidate under `.claude/skills/<name>/` only lets it check the skill and command markdown for injection wording and a nested `.mcp.json` for unpinned packages, not the hook definitions, so read `hooks/hooks.json` and every script it names yourself. Pin by 40-character commit: a manifest `version` that does not change hides a new pin from `claude plugin update`. A manual-only skill is not reachable by the model or by workflow subagents, so "0 listed tokens" also means 0 autonomous use.
