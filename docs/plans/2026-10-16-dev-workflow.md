---
status: done (0.2.0); the final design differs where noted in DESIGN.md section 13
date: 2026-10-16
owner: timchrist49
---
# Plan: a lean, consistent, tested, security-checked, parallel dev workflow (0.2.0)

## Goal
The user runs Claude Code with Ultracode effort and the `opusplan` model. claude-hr must add: consistent code, a test-and-verify
step along the way, security checking, fast hooks, and parallel workflows, **without adding always-on tokens**. Evidence and
rejected options: docs/ADDONS.md and the research behind this plan (2026-10-16, nine investigators, claims fact-checked).

## Principles (non-negotiable)
1. **Zero always-on cost by default.** New skills are `disable-model-invocation: true` (not in the skill listing). Hooks add no
   context unless they fail something. New CLI commands cost nothing. Anything that costs tokens is opt-in and measured.
2. **Never set model, effort or thinking.** The user's `opusplan` + Ultracode choice is untouched.
3. **Repo-aware, never imposing.** Formatters, test commands and linters come from the repo's own configuration.
4. **One spawn per turn, not per edit.** All gate work happens in ONE `claude-hr-hook stop` process (format -> verify -> audit log).
5. **Fail open, never block `claude`.** Any exception, timeout or missing tool exits 0 silently (logged to `logs/crash.log`).
6. **Untrusted text is data.** Issue/PR text, READMEs, repo `.claude/` files and tool output never become instructions.
7. **Deterministic gates are authoritative; LLM reviewers are advisory.** CI is the last line; hooks are best-effort.
8. **Windows, Linux, macOS.** stdlib only, LF endings, `ruff format`-clean, lazy imports on hot paths.

## The loop
issue -> `/hr-plan` (plan file in `docs/plans/`, user approves) -> implement a step (parallel agents for independent steps via the
`hr-plan-execute` workflow or `claude agents`) -> end of turn: format changed files, run the repo's fast checks (trusted recipe) ->
`/hr-done` evidence checklist -> `/code-review` (+ `/security-review` when a trust boundary changed; `claude-hr-ctl audit`) ->
`/hr-gh pr` -> required CI checks -> docs updated -> issue closed by the PR.

## Components
| Piece | Where | Default |
|---|---|---|
| Formatter detector + `claude-hr-ctl fmt` | `src/claude_hr/fmt.py` | on, inert unless the repo configures a formatter |
| Verify recipe, trust, gate, test-weakening advisory | `src/claude_hr/verify.py`, `claude-hr-ctl verify` | gate runs only for a trusted `.claude/hr-verify.toml` |
| Repo audit / trust preflight | `src/claude_hr/audit.py`, `claude-hr-ctl audit` | manual command, zero tokens |
| Stop dispatcher | `claude-hr-hook stop`, plugin `hr-gates` | in `standard` |
| Audit log (metadata only) | `src/claude_hr/auditlog.py` | on |
| Footprint measurement | `src/claude_hr/footprint.py`, `claude-hr-ctl footprint` | manual |
| Repo scaffold | `src/claude_hr/initrepo.py`, `claude-hr-ctl init-repo` | dry-run unless `--apply` |
| Skills `hr-plan`, `hr-done`, `hr-gh`, `hr-audit` | `plugins/hr-core/skills/` | manual-only |
| Workflow `hr-plan-execute` | `src/claude_hr/data/workflows/` | opt-in (`workflows install`) |
| git-safety asks + gh token guard | `hooks.py` bash-guard | on |
| Trimmed `standard` profile | `plugins.toml` | on |

## Module contracts (stdlib only; every function must be safe to call in a hook: never raise out of the public entry points)

### fmt.py
```python
@dataclass(frozen=True)
class Formatter:
    name: str          # ruff | black | prettier | biome | gofmt | rustfmt
    exe: str           # resolved absolute path (PATH or the project's own .venv / node_modules/.bin)
    def stdin_argv(self, path: Path) -> list[str]   # argv that reads the file text on stdin and writes formatted text to stdout
def detect(path: Path, root: Path) -> Formatter | None          # ONLY if the repo explicitly configures that formatter
def format_text(f: Formatter, path: Path, text: str, *, cwd: Path, timeout: float) -> str | None   # None = failed
def changed_files(root: Path, *, max_files: int, max_kb: int, skip_globs=()) -> list[Path]   # git status; skips ignored/lock/minified/generated/vendored/large
def format_changed(root: Path, *, timeout_s=30.0, max_files=40, max_kb=512, skip_globs=(), allow=None, clean_gate=True) -> FormatReport
def describe(root: Path) -> list[dict]                          # for `claude-hr-ctl fmt`: which formatter handles which extensions here
@dataclass class FormatReport: formatted: list[str]; skipped: list[tuple[str, str]]; errors: list[tuple[str, str]]; formatters: list[str]; elapsed_s: float
```
`clean_gate=True`: a tracked file is only rewritten if its `HEAD` version is already clean under the formatter (no whole-file drift
diffs); untracked/new files are always formatted. Write only if the output differs; preserve the file's line endings policy of the repo.

### verify.py
```python
@dataclass(frozen=True) class Check: name: str; argv: list[str]; timeout_s: int = 120; cwd: str = "."
@dataclass class Recipe: fast: list[Check]; full: list[Check]; source: str
def discover(root: Path) -> Recipe                  # propose from package.json scripts, Makefile/justfile, pyproject (pytest/ruff/mypy/pyright), Cargo, go.mod, .pre-commit-config, CI `run:` steps
def render_toml(r: Recipe) -> str                   # content of .claude/hr-verify.toml
def config_path(root: Path) -> Path
def load(root: Path) -> Recipe | None               # strict validation: argv lists of str only, bounded timeouts, cwd inside the repo
def is_trusted(root: Path, state_dir: Path) -> bool # sha256 of the config file == the hash recorded by `verify trust`
def trust(root: Path, state_dir: Path) -> str
def run(root: Path, checks: list[Check], *, total_timeout_s=300) -> RunResult   # argv, no shell, scrubbed env, timeouts kill the process TREE
def fingerprint(root: Path) -> str                  # hash of the working-tree change (status + diff + untracked metadata)
def test_weakening(root: Path) -> list[str]         # ADVISORY regex scan of `git diff -U0 HEAD`
def gate(root: Path, state_dir: Path, *, max_blocks=2, timeout_s=120, tier="fast") -> GateOutcome   # action: skip | pass | block | error
```
`gate` never runs anything unless `is_trusted`. It skips when the fingerprint equals the last green one, caps blocks per fingerprint,
and returns a failure tail of at most 40 lines / 1500 bytes.

### audit.py
```python
@dataclass(frozen=True) class Finding: severity: str; kind: str; path: str; detail: str      # severity: high|medium|low|info
def scan_repo(root: Path) -> list[Finding]   # agent-config attack surface, agent-directed text, workflow lint, secrets (masked)
def run_tools(root: Path, *, timeout_s=60) -> list[ToolRun]   # gitleaks / pip-audit / npm audit / osv-scanner IF on PATH; never installs
def render(findings, tools, *, as_json=False) -> str
```
Scan: `.claude/settings*.json` hooks/env/apiKeyHelper/statusLine/enableAllProjectMcpServers, `.mcp.json`, skills/agents with
`allowed-tools` or inline command injection, hidden Unicode in CLAUDE.md/skills/READMEs, agent-directed instructions ("if you are a
coding agent", "ignore previous instructions", `curl | sh`) in README/AGENTS.md/CLAUDE.md/llms.txt, `.github/workflows` (unpinned
`uses:` not a 40-hex SHA, `pull_request_target`, missing `permissions`, `allowed_non_write_users`), secret patterns (reuse `creds.py`).

### initrepo.py
```python
@dataclass(frozen=True) class Action: path: str; content: str; exists: bool; group: str
def plan(root: Path, only=None) -> list[Action]       # groups: github, docs, editor, dependabot, codeowners, review, ci
def apply(root: Path, actions) -> list[str]            # writes only files that do not exist; never overwrites
```
Templates live in `src/claude_hr/data/repo-template/` (LF). Dependabot ecosystems are detected from the repo; CODEOWNERS owner is
parsed from `origin` (skip with a note if unknown); the CI template uses tag refs and `permissions: contents: read`, timeouts, concurrency.

### footprint.py
```python
@dataclass class PluginFootprint: name; version; skills: int; agents: int; commands: int; manual_only_skills: int; description_tokens_est: int; hook_events: list[str]; injects_context: bool | None; mcp_servers: int; notes: list[str]
def scan_plugin_dir(path: Path) -> PluginFootprint
def scan_installed(plugin_list: list[dict]) -> list[PluginFootprint]    # `claude plugin list --json` entries ({id, version, scope, enabled, installPath})
def render(fps: list[PluginFootprint], budget: int | None) -> str
def probe(claude: str, *, runner) -> dict      # one tiny `claude -p ... --output-format json` call; runner is injectable for tests
```

## CLI contracts (wired by the integrator, not by module authors)
`claude-hr-ctl fmt [--check] [--changed] [paths...]` | `verify init [--write]` / `verify run [--tier fast|full]` / `verify trust` /
`verify status` | `audit [--json] [--strict] [--tools]` | `init-repo [--apply] [--only GROUPS]` | `footprint [--probe]`.

## Stop dispatcher (`claude-hr-hook stop`, plugin `hr-gates`, exec form, no shell)
stdin: `cwd`, `stop_hook_active`, `permission_mode`, `last_assistant_message`, `agent_id` (subagents). Flow: exit 0 silently if gates are
off / not a git repo / plan mode / subagent (NOT `stop_hook_active`: the gate counts its own blocks per working-tree state, see DESIGN.md 13); fingerprint unchanged -> exit 0; (1) `fmt.format_changed`; (2) `verify.gate`
(trusted recipe only; on failure print `{"decision":"block","reason":...}` at most `max_blocks` times per fingerprint, well under Claude
Code's cap of 8); (3) append a metadata-only audit record. Everything fails open.

## Skips (see docs/ADDONS.md)
Bun as a dependency, Headroom Desktop, spec-driven frameworks, GitHub MCP by default, LLM review of untrusted PRs in CI, model-judged
Stop hooks, auto-merge, per-edit hooks, unattended LLM routines (deferred: need a measured cost pilot and a stored credential).
