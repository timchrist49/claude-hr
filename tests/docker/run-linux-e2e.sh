#!/usr/bin/env bash
# Runs INSIDE the test container as a non-root user. /src is the repo (read-only mount).
# Simulates a brand-new VM: installs everything from scratch via install.sh, then verifies behaviour.
set -uo pipefail
pass=0; fail=0
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; fail=$((fail+1)); }
check() { local name="$1"; shift; if "$@" >/tmp/check.out 2>&1; then ok "$name"; else bad "$name"; sed 's/^/        /' /tmp/check.out | tail -15; fi; }

export HOME="${HOME:-/home/hr}"
export PATH="$HOME/.local/bin:$PATH"
SRC_DIR="${SRC_DIR:-/src}"
# A user systemd manager needs these when the shell was not started by a PAM session (wsl.exe, docker exec).
if [ -d "/run/user/$(id -u)" ]; then
  export XDG_RUNTIME_DIR="/run/user/$(id -u)"
  export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
fi
HAVE_SYSTEMD=0; systemctl --user show-environment >/dev/null 2>&1 && HAVE_SYSTEMD=1
export CLAUDE_HR_HEADROOM__FOOTPRINT=lite          # keep the container test small; full extras are covered on Windows
export CLAUDE_HR_HEADROOM__PORT=8787
rm -rf /tmp/src && cp -a "$SRC_DIR" /tmp/src && rm -rf /tmp/src/.git

echo "== 1. fresh install (curl, git only) =="
check "install.sh completes"        bash /tmp/src/install.sh --local /tmp/src --skip-schedule
check "claude-hr on PATH"         command -v claude-hr
check "claude-hr-ctl on PATH"            command -v claude-hr-ctl
check "claude installed"            claude --version
check "headroom installed"          headroom --version

echo "== 2. idempotent re-run =="
check "second install.sh run"       bash /tmp/src/install.sh --local /tmp/src --skip-schedule --no-headroom-install
check "settings unchanged on rerun" bash -c "claude-hr-ctl setup --only settings | grep -q '0 set'"

echo "== 3. managed config landed =="
check "settings.json has baseline"  grep -q DISABLE_AUTOUPDATER "$HOME/.claude/settings.json"
check "CLAUDE.md import block"      grep -q "CLAUDE-HR:BEGIN" "$HOME/.claude/CLAUDE.md"
check "baseline.md present"         test -s "$HOME/.claude/claude-hr/baseline.md"
check "headroom MCP registered"     grep -q '"headroom"' "$HOME/.claude.json"
check "hr-core plugin installed"  bash -c "claude plugin list --json | grep -q hr-core"
check "process wrapper generated"   test -x "$HOME/.claude-hr/bin/hr-wrap.sh"
check "wrapper is valid sh"         sh -n "$HOME/.claude-hr/bin/hr-wrap.sh"
check "wrapper set in settings env" grep -q CLAUDE_CODE_PROCESS_WRAPPER "$HOME/.claude/settings.json"

echo "== 4. Headroom proxy =="
check "proxy starts and is healthy" claude-hr-ctl headroom start --wait 240
check "compression smoke test"      claude-hr-ctl headroom bench --wait 60

echo "== 5. launcher =="
check "passthrough --version"       bash -c "claude-hr --version | grep -q 'Claude Code'"
check "no login yet -> direct (so login works)" bash -c "claude-hr --hr-dry-run hi 2>&1 | grep -q 'no login yet'"
export CLAUDE_CODE_OAUTH_TOKEN=dummy-for-routing-plan-only   # stands in for a completed login; never sent anywhere
check "dry-run routes via headroom" bash -c "claude-hr --hr-dry-run agents --dangerously-skip-permissions 2>&1 | grep -q 'route=headroom'"
check "dry-run mcp is direct"       bash -c "claude-hr --hr-dry-run mcp list 2>&1 | grep -q 'route=direct'"
check "--hr-direct bypasses"      bash -c "claude-hr --hr-dry-run --hr-direct hi 2>&1 | grep -q 'route=direct'"
check "exec keeps exit code"        bash -c "claude-hr --version >/dev/null; test \$? -eq 0"
unset CLAUDE_CODE_OAUTH_TOKEN

echo "== 6. management commands =="
check "doctor has no FAIL"          bash -c "claude-hr-ctl doctor | grep -v '^\[FAIL\]' >/dev/null && ! claude-hr-ctl doctor | grep -q '^\[FAIL\]'"
check "status"                      claude-hr-ctl status
check "plugins audit"               claude-hr-ctl plugins audit
check "claudemd lint"               claude-hr-ctl claudemd lint
check "cleanup dry-run"             claude-hr-ctl cleanup
check "usage"                       claude-hr-ctl usage --days 1
check "update --check"              claude-hr-ctl update --check --only claude headroom
check "bash guard blocks .env"      bash -c "echo '{\"tool_input\":{\"command\":\"cat .env\"}}' | claude-hr-ctl hook bash-guard; test \$? -eq 2"

echo "== 7. scheduler ($([ $HAVE_SYSTEMD = 1 ] && echo systemd || echo cron fallback)) =="
check "schedule install"            claude-hr-ctl schedule install
if [ "$HAVE_SYSTEMD" = 1 ]; then
  check "4 timers active"           bash -c "test \$(systemctl --user list-timers 'claude-hr-*' --all --no-pager | grep -c 'claude-hr-.*timer') -ge 4"
  check "units pass systemd-analyze" bash -c "systemd-analyze --user verify $HOME/.config/systemd/user/claude-hr-*.service $HOME/.config/systemd/user/claude-hr-*.timer"
  check "service runs the job"      bash -c "systemctl --user start claude-hr-monitor.service && sleep 3 && test -s $HOME/.claude-hr/state/health.json"
else
  check "crontab has managed block" bash -c "crontab -l | grep -q 'claude-hr managed block'"
fi
check "job runs: monitor"           claude-hr-ctl job monitor
check "job runs: warm"              claude-hr-ctl job warm
check "schedule remove"             claude-hr-ctl schedule remove
if [ "$HAVE_SYSTEMD" = 1 ]; then
  check "timers removed"            bash -c "! systemctl --user list-timers 'claude-hr-*' --all --no-pager | grep -q 'claude-hr-'"
else
  check "crontab block removed"     bash -c "! crontab -l 2>/dev/null | grep -q 'claude-hr managed block'"
fi

if [ "$HAVE_SYSTEMD" = 1 ]; then
  echo "== 7b. resource governor (real cgroups) =="
  check "wrapper fails open without a user bus" bash -c "XDG_RUNTIME_DIR=/nonexistent $HOME/.claude-hr/bin/hr-wrap.sh echo ran | grep -q ran"
  if [ -S "$XDG_RUNTIME_DIR/bus" ]; then
  check "wrapper puts child in a claude-hr scope" bash -c "$HOME/.claude-hr/bin/hr-wrap.sh cat /proc/self/cgroup | grep -q 'claude-hr-'"
  check "nested wrapper does not stack"       bash -c "$HOME/.claude-hr/bin/hr-wrap.sh $HOME/.claude-hr/bin/hr-wrap.sh cat /proc/self/cgroup | grep -c 'claude-hr-' | grep -q '^1$'"
  check "memory cap kills a runaway" bash -c "
    cd /tmp/src && CLAUDE_HR_RESOURCES__MEMORY_MAX_GB=0.25 CLAUDE_HR_RESOURCES__MEMORY_HIGH_GB=0.2 uv run --python 3.13 --no-project python - <<'PY'
import subprocess, sys
sys.path.insert(0, 'src')
from claude_hr import config, resources
cfg = config.load()
cmd = resources.wrap_command_posix(cfg, ['python3', '-c', 'x = bytearray(900 * 1024 * 1024); print(\"survived\")'])
assert cmd[0] == 'systemd-run', cmd
r = subprocess.run(cmd, capture_output=True, text=True)
print('rc', r.returncode, r.stdout.strip())
sys.exit(0 if r.returncode != 0 and 'survived' not in r.stdout else 1)
PY"
  else
    echo "  SKIP cgroup checks: no user D-Bus session here (apt install dbus-user-session)"
  fi
fi

echo "== 8. unit tests inside the container =="
check "pytest"                      bash -c "cd /tmp/src && uv run --python 3.13 --with pytest pytest -q -p no:cacheprovider tests"

echo "== 9. uninstall restores user config =="
check "uninstall"                   claude-hr-ctl uninstall
check "baseline removed"            bash -c "! grep -q DISABLE_AUTOUPDATER $HOME/.claude/settings.json"
check "CLAUDE.md block removed"     bash -c "! grep -q 'CLAUDE-HR:BEGIN' $HOME/.claude/CLAUDE.md 2>/dev/null"

echo
echo "RESULT: $pass passed, $fail failed"
[ "$fail" = 0 ]
