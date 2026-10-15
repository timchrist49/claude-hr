# claude-hr (this repo)

Distribution repo for `claude-hr` / `claude-hr-ctl`. Python package in `src/claude_hr` (stdlib only, Python >= 3.11);
the Claude plugin in `plugins/hr-core`; bootstrap installers `install.sh` / `install.ps1`.

## Commands
- Tests: `uv run --with pytest pytest -q`  (always run before finishing; they must pass on Windows and Linux)
- Lint: `uv run --with ruff ruff check src`
- Linux fresh-VM test: `tests/docker/run-linux-e2e.sh` (inside `tests/docker/Dockerfile.linux`)
- Try changes without touching your real profile: set `CLAUDE_HR_HOME`, `CLAUDE_HR_USER_HOME`, `CLAUDE_CONFIG_DIR` to a temp dir
  (keep the path short on Windows: git clones under `CLAUDE_CONFIG_DIR` fail past 260 characters).

## Rules
- All files use LF line endings (`tests/test_repo_hygiene.py` enforces it). Shell scripts must run under `bash` on Linux.
- The launcher must stay fast (measured 0.15-0.25 s over plain `claude` on Windows, of which Python start-up is 0.11 s; keep heavy imports lazy, see the note in launcher.py) and stdlib-only; anything that can fail must fail open and never block `claude`.
- Never write secrets, `ANTHROPIC_BASE_URL`, or model/effort settings into the managed `settings.json` baseline.
- New config key: add it to `src/claude_hr/data/defaults.toml` with a comment, then to the README/OPERATIONS docs.
- Version bumps: `__init__.py`, `pyproject.toml`, `plugins/hr-core/.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json` together.
