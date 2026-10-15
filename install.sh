#!/usr/bin/env bash
# claude-hr bootstrap installer for Linux, macOS, WSL and Git-Bash.
#
#   curl -fsSL https://raw.githubusercontent.com/timchrist49/claude-hr/main/install.sh | bash
#   ./install.sh --local .            # install from a checkout (development / air-gapped)
#
# Everything real happens in `claude-hr-ctl setup` (idempotent, re-runnable); this script only gets the
# prerequisites in place: uv, Claude Code, the claude-hr source tree, and the two commands.
#
# Options:
#   --repo URL        git URL of the claude-hr repo            (env CLAUDE_HR_REPO_URL)
#   --ref REF         branch or tag to install            (env CLAUDE_HR_REF, default main)
#   --local PATH      use an existing checkout instead of cloning
#   --profile NAME    plugin profile: minimal | standard | full | lean-output
#   --no-headroom-install   skip the (large) Headroom download; run `claude-hr-ctl headroom install` later
#   --skip-schedule   do not register scheduled jobs (warm/monitor/update/maintain)
#   --shim            make plain `claude` run claude-hr
#   --dry-run         show what setup would change, install nothing beyond prerequisites
# Private fork: export CLAUDE_HR_GIT_TOKEN (a fine-grained PAT with Contents:read). It is used only for the
# clone via a one-shot HTTP header and is never written to disk. For unattended updates prefer an
# SSH deploy key URL (git@github.com:you/claude-hr.git) or `gh auth login`.
set -euo pipefail

REPO_URL="${CLAUDE_HR_REPO_URL:-https://github.com/timchrist49/claude-hr.git}"
REF="${CLAUDE_HR_REF:-main}"
LOCAL=""
SETUP_ARGS=()
SHIM=0
CLAUDE_HR_HOME="${CLAUDE_HR_HOME:-$HOME/.claude-hr}"

log() { printf '\033[1;36m[claude-hr-install]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[claude-hr-install] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO_URL="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --local) LOCAL="$2"; shift 2 ;;
    --profile) SETUP_ARGS+=(--profile "$2"); shift 2 ;;
    --no-headroom-install) SETUP_ARGS+=(--no-headroom-install); shift ;;
    --skip-schedule) SETUP_ARGS+=(--skip schedule); shift ;;
    --dry-run) SETUP_ARGS+=(--dry-run); shift ;;
    --shim) SHIM=1; shift ;;
    -h|--help) sed -n '2,/^set -/{/^set -/!p;}' "$0"; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

[ "$(id -u 2>/dev/null || echo 1)" = "0" ] && log "warning: running as root. Claude Code refuses --dangerously-skip-permissions as root; use a normal user."

command -v curl >/dev/null || die "curl is required"
command -v git  >/dev/null || die "git is required (Claude Code also needs it on Windows/WSL)"

export PATH="$HOME/.local/bin:$PATH"

# ---- uv ------------------------------------------------------------------------------------
if ! command -v uv >/dev/null; then
  log "installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  command -v uv >/dev/null || die "uv install failed"
fi

# ---- Claude Code ---------------------------------------------------------------------------
if ! command -v claude >/dev/null; then
  log "installing Claude Code (native build)"
  curl -fsSL https://claude.ai/install.sh | bash
  export PATH="$HOME/.local/bin:$PATH"
  command -v claude >/dev/null || die "Claude Code install failed; see https://code.claude.com/docs/en/setup"
fi
log "claude $(claude --version 2>/dev/null | head -1)"

# ---- claude-hr source ---------------------------------------------------------------------------
mkdir -p "$CLAUDE_HR_HOME"
# state, logs and settings backups live here: owner-only from the first moment (never the user's real home dir itself)
if [ "$(cd "$CLAUDE_HR_HOME" && pwd -P)" != "$(cd "$HOME" && pwd -P)" ]; then chmod 700 "$CLAUDE_HR_HOME" 2>/dev/null || true; fi
SRC="$CLAUDE_HR_HOME/repo"
if [ -n "$LOCAL" ]; then
  LOCAL="$(cd "$LOCAL" && pwd)"
  if [ "$LOCAL" != "$SRC" ]; then
    log "using checkout $LOCAL"
    rm -rf "$SRC"
    mkdir -p "$SRC"
    # copy the working tree as it is on disk (committed or not); tar is portable to BSD/macOS
    (cd "$LOCAL" && tar --exclude=.git --exclude=__pycache__ --exclude=.venv --exclude=.pytest_cache --exclude=.ruff_cache -cf - .) | (cd "$SRC" && tar -xf -)
  fi
else
  [ -n "$REPO_URL" ] || die "no repo URL: pass --repo <url>, set CLAUDE_HR_REPO_URL, or use --local <path>"
  # The token travels in git's ENVIRONMENT (GIT_CONFIG_*), never in argv, so it is not visible in `ps` output.
  git_auth() {
    if [ -n "${CLAUDE_HR_GIT_TOKEN:-}" ]; then
      GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.extraheader \
        GIT_CONFIG_VALUE_0="Authorization: Basic $(printf 'x-access-token:%s' "$CLAUDE_HR_GIT_TOKEN" | base64 | tr -d '\n')" \
        git "$@"
    else
      git "$@"
    fi
  }
  if [ -d "$SRC/.git" ]; then
    log "updating $SRC"
    git_auth -C "$SRC" fetch --quiet --tags --prune origin
    git -C "$SRC" checkout --quiet --detach "$REF" 2>/dev/null || git -C "$SRC" checkout --quiet --detach "origin/$REF"
  else
    log "cloning $REPO_URL ($REF)"
    git_auth clone --quiet "$REPO_URL" "$SRC"
    git -C "$SRC" checkout --quiet --detach "$REF" 2>/dev/null || git -C "$SRC" checkout --quiet --detach "origin/$REF"
  fi
fi

# ---- commands ------------------------------------------------------------------------------
log "installing claude-hr and claude-hr-ctl"
uv tool install --python 3.13 --force --reinstall "$SRC" >/dev/null
export PATH="$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin"):$PATH"
command -v claude-hr-ctl >/dev/null || die "claude-hr-ctl not on PATH after install (try: uv tool update-shell)"

log "running claude-hr-ctl setup ${SETUP_ARGS[*]:-}"
CLAUDE_HR_REPO="$SRC" claude-hr-ctl setup ${SETUP_ARGS[@]+"${SETUP_ARGS[@]}"}

if [ "$SHIM" = "1" ]; then claude-hr-ctl shim install; fi

cat <<EOF

claude-hr is installed.
  Open a NEW terminal, then:   claude-hr            (interactive)
                               claude-hr agents --dangerously-skip-permissions
  Health:                      claude-hr-ctl doctor
  If 'claude-hr' is not found: run  uv tool update-shell  and restart the shell.
EOF
