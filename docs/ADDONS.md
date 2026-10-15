# What was added, what is optional, what was rejected

Researched October 2026; stars/dates from the GitHub API at the time. Verdicts reflect *evidence about cost and
quality*, not popularity.

## Included (default)
| Component | Why |
|---|---|
| **Headroom** `headroom-ai[all]` — proxy, SmartCrusher/CodeCompressor/Kompress, CCR, memory, `learn`, output shaping, MCP server, `headroom-snip` plugin | The requested core. All extras that matter for Claude Code are in `[all]`. Framework adapters (`langchain`, `agno`, `strands`, `bedrock`, `anyllm`, `autogen`), `vector` (needs a C++ toolchain), `memory-stack` (Qdrant + Neo4j), `voice-train`, `pytorch-mps` are **not** installed: they serve other stacks or add infrastructure. Add via `headroom.extras` |
| **Serena** (via Headroom's registration, pinned with `headroom.serena_version`) | Symbol-level navigation; one stdio instance per session, started only when a session calls it |
| **Native statusLine** | Model, context %, cost, Headroom savings, 5-hour plan usage; no third-party package |
| **Native usage ledger** (`usage`) | Replaces `ccusage` without an npm dependency; daily snapshot survives Claude's transcript pruning |
| **Anthropic catalog plugins** | `claude-md-management`, `feature-dev`, `security-guidance`, `code-simplifier`, `context7`, `superpowers` (judgement call on Claude 5 — see below), `skill-creator` (full profile) |
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
| **Superpowers** | Process-heavy skills (brainstorm → plan → TDD → review). Anthropic's Claude 5 prompting notes warn older scaffolding can over-trigger; it costs ~700 always-on tokens. Kept in `standard` because the team uses it; drop with `plugins.profile = "minimal"` or measure with `claude plugin eval` |
| **gitleaks** | `creds doctor` detects it; install it as a pre-commit hook in each repo |
| **1Password CLI** | `credentials.op_env_file` |

## Rejected
| Component | Reason |
|---|---|
| claude-mem | Duplicates Headroom memory and Claude auto-memory; sign-in/cloud sync prompts, paid tier after trial |
| Caveman suite | Overlaps Headroom/RTK; measured output-token reduction ~8.5% vs 65% advertised; its own README shows ~3% beyond "answer concisely" |
| Claude-Code-Usage-Monitor | Stale; native statusLine `rate_limits` + ledger cover it |
| claude-squad / ccmanager / Conductor | `claude agents`, `--bg`, `--worktree` now cover the use case; claude-squad is AGPL and tmux-only; Conductor is a GUI |
| Headroom `--mode token` / `agent-90` profile | Raised the uncached share ~4× and cache busts 9× in a published test; no spend benefit |
| `NODE_OPTIONS=--max-old-space-size` | No effect on the native binary |
| Headroom `headroom` hooks plugin | Spawns Python on every Bash tool call to manage a deployment claude-hr already manages |
| Headroom Enterprise features | Paid; not needed |

## Workflows (opt-in)
`claude-hr-ctl workflows install` copies `hr-review-changes` into the project's `.claude/workflows/`. It reviews the diff on up to five dimensions, then has skeptics try to refute every finding (majority-refute drops it) and reports what survives; anything it could not check is logged, never silently dropped. Cost is roughly 10-25 agents per run, so it is not part of any default flow. Plugins cannot ship workflows, hence the separate installer.

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
