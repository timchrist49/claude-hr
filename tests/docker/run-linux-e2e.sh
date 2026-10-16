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

echo "== 2b. PATH persistence (new terminal finds claude-hr) =="
rc_lines() { cat "$HOME/.bashrc" "$HOME/.profile" "$HOME/.bash_profile" 2>/dev/null | grep -c '\.local/bin' || true; }
clean_install() { env -i HOME="$HOME" PATH=/usr/local/bin:/usr/bin:/bin SHELL=/bin/bash bash /tmp/src/install.sh --local /tmp/src --skip-schedule --no-headroom-install "$@"; }
rc0="$(rc_lines)"
check "install.sh --no-modify-path leaves the startup files alone"  clean_install --no-modify-path
check "  ... and wrote no PATH line"                                 test "$(rc_lines)" = "$rc0"
check "install.sh with ~/.local/bin missing from PATH fixes it"     clean_install
rc1="$(rc_lines)"
check "  ... a startup file gained a PATH line"                     test "$rc1" -gt "$rc0"
check "  ... a fresh login shell finds claude-hr"                   env -i HOME="$HOME" PATH=/usr/bin:/bin bash -lc 'command -v claude-hr'
check "  ... re-running does not add a second line"                 bash -c "$(declare -f rc_lines clean_install); clean_install >/dev/null 2>&1; test \"\$(rc_lines)\" = \"$rc1\""

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
check "claude-hr home is owner-only"   bash -c "test \"\$(stat -c %a \$HOME/.claude-hr)\" = 700 && test \"\$(stat -c %a \$HOME/.claude-hr/state)\" = 700 && test \"\$(stat -c %a \$HOME/.claude-hr/logs)\" = 700 && test \"\$(stat -c %a \$HOME/.claude-hr/backups)\" = 700"
echo "  (info) lite headroom-ai tool environment: $(du -sm "$HOME/.local/share/uv/tools/headroom-ai" 2>/dev/null | cut -f1) MB on disk"
check "proxy log is owner-only"        bash -c "test \"\$(stat -c %a \$HOME/.claude-hr/logs/headroom-proxy.log)\" = 600"

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

echo "== 6b. development loop: end-of-turn gate, trust, audit, scaffolding, footprint =="
PY_TOOL="$HOME/.local/share/uv/tools/claude-hr/bin/python"
check "hr-gates plugin installed (standard profile)" bash -c "claude plugin list --json | grep -q hr-gates"
check "footprint is within the standard budget"      bash -c "claude-hr-ctl footprint | grep -q ': OK'"
GP=/tmp/gateproj; rm -rf "$GP"; mkdir -p "$GP/.claude"; git -C "$GP" init -q
git -C "$GP" config user.email t@example.com; git -C "$GP" config user.name t
cat > "$GP/.claude/hr-verify.toml" <<'TOML'
[[fast]]
name = "needs-done-file"
argv = ["sh", "-c", "test -e done.txt || { echo 'done.txt is missing'; exit 1; }"]
timeout_s = 30
TOML
stop_json() { printf '{"cwd":"%s","hook_event_name":"Stop"}' "$GP" | claude-hr-hook stop; }
check "verify trust refuses a Claude Code shell"      bash -c "cd $GP && CLAUDECODE=1 claude-hr-ctl verify trust; test \$? -eq 1"
check "verify trust refuses without a terminal"       bash -c "cd $GP && env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT claude-hr-ctl verify trust </dev/null; test \$? -eq 1"
check "bash guard denies an agent running verify trust" bash -c "echo '{\"tool_input\":{\"command\":\"claude-hr-ctl verify trust\"}}' | claude-hr-hook bash-guard; test \$? -eq 2"
check "untrusted recipe: the gate prints nothing"     bash -c "$(declare -f stop_json); GP=$GP; test -z \"\$(stop_json)\""
check "recipe trusted (what a person does in a terminal)" "$PY_TOOL" -c "from pathlib import Path; from claude_hr import verify, paths; verify.trust(Path('$GP'), paths.state_dir())"
check "trusted failing recipe: the gate blocks with the reason" bash -c "$(declare -f stop_json); GP=$GP; stop_json | grep -q '\"decision\": \"block\"' && stop_json | grep -q 'needs-done-file failed'"
check "after the fix the gate passes silently"        bash -c "$(declare -f stop_json); GP=$GP; touch $GP/done.txt; test -z \"\$(stop_json)\""
check "audit trail has the block and the pass"        bash -c "claude-hr-ctl trail --tail 10 | grep -q 'verify=block' && claude-hr-ctl trail --tail 10 | grep -q 'verify=pass'"
check "fmt reports no formatter in a bare repo"       bash -c "cd $GP && claude-hr-ctl fmt | grep -q 'no formatter'"
check "audit runs"                                    bash -c "cd $GP && claude-hr-ctl audit"
check "init-repo is a dry run"                        bash -c "cd $GP && claude-hr-ctl init-repo --only docs,editor | grep -q 'dry run' && test ! -e docs"
check "both workflows installable"                    bash -c "claude-hr-ctl workflows install --dir $GP && test -e $GP/.claude/workflows/hr-plan-execute.js && test -e $GP/.claude/workflows/hr-review-changes.js"

echo "== 7. scheduler ($([ $HAVE_SYSTEMD = 1 ] && echo systemd || echo cron fallback)) =="
check "schedule install"            claude-hr-ctl schedule install
if [ "$HAVE_SYSTEMD" = 1 ]; then
  check "4 timers active"           bash -c "test \$(systemctl --user list-timers 'claude-hr-*' --all --no-pager | grep -c 'claude-hr-.*timer') -ge 4"
  check "units ask systemd not to kill leftovers" grep -q '^KillMode=process' "$HOME/.config/systemd/user/claude-hr-update.service"
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

if [ "$HAVE_SYSTEMD" = 1 ] && [ -S "${XDG_RUNTIME_DIR:-/nonexistent}/bus" ]; then
  echo "== 7a. self-update helper survives the systemd job that starts it =="
  helper_survives() {
    local py="$HOME/.local/share/uv/tools/claude-hr/bin/python"
    rm -f /tmp/hr-survived
    # a oneshot service with the default KillMode=control-group, like the timer jobs: it kills every process left in its cgroup
    systemd-run --user --wait --collect --quiet -p Type=oneshot "$py" -c '
from claude_hr import config, updater
u = updater.Updater(config.load())
u._pending_reinstall = ["sh", "-c", "sleep 5; touch /tmp/hr-survived"]
u._launch_reinstall()
' || return 1
    [ ! -e /tmp/hr-survived ] || return 1          # the job is over; the helper must still be running
    for _ in $(seq 1 30); do [ -e /tmp/hr-survived ] && return 0; sleep 1; done
    return 1
  }
  check "reinstall started by a systemd job still completes"  helper_survives
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
