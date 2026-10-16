"""Apply claude-hr's managed configuration to Claude Code: settings, CLAUDE.md, MCP servers, plugins."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import baseline, paths, procutil, settings_merge
from .config import Config
from .headroom import Headroom
from .jsonio import JsonFileError, atomic_write_text, backup_file, read_json, write_json

BEGIN = "<!-- CLAUDE-HR:BEGIN (managed by claude-hr-ctl; do not edit inside this block) -->"
END = "<!-- CLAUDE-HR:END -->"

# `setup` writes the version whose managed baseline (settings.json, CLAUDE.md block, plugins) it last applied here. A release
# that changes the baseline reaches a machine through the unattended self-update only as code: nothing re-applies it, so
# doctor and the daily update job compare this file with the running version.
APPLIED_VERSION_FILE = "applied-version.json"
BASELINE_STEPS = ("settings", "claude-md", "plugins")


def applied_version() -> str | None:
    """The claude-hr version whose baseline was last applied, or None (never recorded: set up by a release before 0.2.0)."""
    try:
        data = read_json(paths.state_dir() / APPLIED_VERSION_FILE, {}) or {}
        value = data.get("version") if isinstance(data, dict) else None
    except (JsonFileError, OSError):
        return None
    return str(value) if value else None


def running_version() -> str:
    # read at call time: the package attribute is what a reinstall (and a test simulating one) changes
    from . import __version__

    return __version__


def record_applied_version() -> None:
    write_json(
        paths.state_dir() / APPLIED_VERSION_FILE,
        {"version": running_version(), "ts": time.time()},
    )


def was_set_up() -> bool:
    """True once `setup` has run on this machine (it leaves install.json and the settings ownership record)."""
    d = paths.state_dir()
    return (d / "install.json").exists() or (d / "settings-merge.json").exists()


def baseline_is_stale() -> tuple[bool, str | None]:
    """(True, version last applied or None) when this machine was set up but not by the claude-hr that is running now."""
    if not was_set_up():
        return False, None
    applied = applied_version()
    return applied != running_version(), applied


@dataclass
class Step:
    name: str
    ok: bool = True
    changed: bool = False
    lines: list[str] = field(default_factory=list)

    def add(self, msg: str) -> None:
        self.lines.append(msg)


class ClaudeSetup:
    def __init__(self, cfg: Config, hr: Headroom, claude: Path | None, dry: bool = False):
        self.cfg, self.hr, self.claude, self.dry = cfg, hr, claude, dry

    # ---- helpers --------------------------------------------------------------------------
    def _claude(self, *args: str, timeout: float = 120):
        if not self.claude:
            return subprocess.CompletedProcess([], 127, "", "claude not found")
        return procutil.run([str(self.claude), *args], timeout=timeout)

    def statusline_command(self) -> str:
        """Absolute path to the tiny claude-hr-hook entry point (forward slashes work in cmd, PowerShell and Git Bash)."""
        name = "claude-hr-hook.exe" if paths.IS_WINDOWS else "claude-hr-hook"
        candidates = [Path(sys.executable).with_name(name), *procutil.which_all("claude-hr-hook")]
        exe = next((c for c in candidates if c.is_file()), None)
        base = str(exe).replace("\\", "/") if exe else "claude-hr-hook"
        return f'"{base}" statusline' if " " in base else f"{base} statusline"

    def install_process_wrapper(self) -> str | None:
        """Write the cgroup wrapper used for Claude's own background spawns (Linux). Returns its path."""
        from . import resources

        script = resources.render_wrapper(self.cfg)
        dest = paths.bin_dir() / "hr-wrap.sh"
        if not script:
            if dest.exists() and not self.dry:
                dest.unlink()
            return None
        if not self.dry:
            paths.bin_dir().mkdir(parents=True, exist_ok=True)
            atomic_write_text(dest, script)
            dest.chmod(0o755)
        return str(dest)

    # ---- settings.json --------------------------------------------------------------------
    def plan_settings(self, force: bool = False) -> tuple[dict, dict, dict, settings_merge.Report]:
        """(current settings.json, merged document, new ownership state, report). Raises JsonFileError when settings.json or
        the ownership state is unparsable. Writes nothing (except the process-wrapper script on a real, non-dry run), so
        doctor can ask what `setup` would change."""
        current = read_json(paths.claude_settings(), {}) or {}
        state = read_json(paths.state_dir() / "settings-merge.json", None)
        wrapper = self.install_process_wrapper()
        want, atomic = baseline.build(self.cfg, self.statusline_command(), wrapper)
        new_doc, new_state, rep = settings_merge.apply_baseline(
            current, want, state, atomic=atomic, force=force
        )
        return current, new_doc, new_state, rep

    def apply_settings(self, force: bool = False) -> Step:
        step = Step("settings")
        target = paths.claude_settings()
        state_file = paths.state_dir() / "settings-merge.json"
        try:
            current, new_doc, new_state, rep = self.plan_settings(force)
        except JsonFileError as exc:
            step.ok = False
            step.add(str(exc))
            step.add(
                "fix the JSON by hand (claude-hr never overwrites an unparsable file), then re-run setup"
            )
            return step
        step.changed = new_doc != current
        step.add(f"{target}: {rep.summary()}")
        for item in rep.kept:
            step.add(f"  kept your value: {item}")
        if self.dry:
            for item in rep.set:
                step.add(f"  would set: {item}")
            return step
        if step.changed:
            write_json(target, new_doc, backup_dir=paths.backups_dir() / "settings")
        write_json(state_file, new_state)
        return step

    def revert_settings(self) -> Step:
        step = Step("settings-revert")
        target = paths.claude_settings()
        state_file = paths.state_dir() / "settings-merge.json"
        try:
            state = read_json(state_file, None)
            if not state:
                step.add("no managed settings recorded")
                return step
            current = read_json(target, {}) or {}
        except JsonFileError as exc:
            # never abort the uninstall over this: report it, leave both files exactly as they are, let the rest run
            step.ok = False
            step.add(str(exc))
            step.add(
                "the managed settings were NOT reverted and the ownership record was kept: fix the JSON by hand, "
                "then run `claude-hr-ctl uninstall` again (or remove the keys claude-hr added yourself)"
            )
            return step
        new_doc, rep = settings_merge.revert(current, state)
        step.changed = new_doc != current
        step.add(f"reverted: {rep.summary()}")
        if not self.dry:
            if step.changed:
                write_json(target, new_doc, backup_dir=paths.backups_dir() / "settings")
            state_file.unlink(missing_ok=True)
            (paths.state_dir() / APPLIED_VERSION_FILE).unlink(missing_ok=True)
        return step

    # ---- CLAUDE.md ------------------------------------------------------------------------
    def sync_claude_md(self) -> Step:
        step = Step("claude-md")
        src = paths.package_data_dir() / "claude-md" / "baseline.md"
        dest_dir = paths.claude_home() / "claude-hr"
        dest = dest_dir / "baseline.md"
        md = paths.claude_user_md()
        block = f"{BEGIN}\n@claude-hr/baseline.md\n{END}"
        text, nl = self._read_md(md)
        if BEGIN in text and END in text and text.index(BEGIN) < text.index(END):
            new = re.sub(
                re.escape(BEGIN) + r".*?" + re.escape(END), lambda _m: block, text, flags=re.S
            )
        else:
            base = self._strip_managed(
                text
            ).rstrip()  # also repairs a half-deleted block without touching user text
            new = (base + "\n\n" if base else "") + block + "\n"
        content = src.read_text(encoding="utf-8")
        changed = (not dest.exists() or dest.read_text(encoding="utf-8") != content) or new != text
        step.changed = changed
        step.add(
            f"{md}: {'update' if changed else 'up to date'} (import of claude-hr/baseline.md, {len(content.splitlines())} lines)"
        )
        if self.dry or not changed:
            return step
        dest_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(dest, content)
        if md.exists():
            backup_file(md, paths.backups_dir() / "claude-md")
        atomic_write_text(md, new.replace("\n", nl))
        return step

    @staticmethod
    def _strip_managed(norm: str) -> str:
        """Remove the managed block; if its markers are damaged, remove only marker/import lines (never user text)."""
        if BEGIN in norm and END in norm and norm.index(BEGIN) < norm.index(END):
            return re.sub(
                r"\n*" + re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n*",
                "\n\n",
                norm,
                flags=re.S,
            )
        keep = [
            ln
            for ln in norm.split("\n")
            if not ln.startswith("<!-- CLAUDE-HR:BEGIN")
            and not ln.startswith("<!-- CLAUDE-HR:END")
            and ln.strip() != "@claude-hr/baseline.md"
        ]
        return "\n".join(keep)

    @staticmethod
    def _read_md(md: Path) -> tuple[str, str]:
        """(text normalised to LF, the file's own newline style): read_text() would hide CRLF."""
        raw = md.read_bytes().decode("utf-8-sig") if md.exists() else ""
        return raw.replace("\r\n", "\n"), ("\r\n" if "\r\n" in raw else "\n")

    def remove_claude_md(self) -> Step:
        step = Step("claude-md-remove")
        md = paths.claude_user_md()
        if md.exists():
            text, nl = self._read_md(md)
            new = self._strip_managed(text).strip()
            step.changed = new != text.strip()
            if step.changed and not self.dry:
                backup_file(md, paths.backups_dir() / "claude-md")
                if new:
                    atomic_write_text(md, (new + "\n").replace("\n", nl))
                else:
                    md.unlink()
        if not self.dry:
            shutil.rmtree(paths.claude_home() / "claude-hr", ignore_errors=True)
        step.add("managed block and ~/.claude/claude-hr removed")
        return step

    # ---- MCP servers ----------------------------------------------------------------------
    def _registered_mcp(self) -> dict:
        data = read_json(paths.claude_global_json(), {}) or {}
        return data.get("mcpServers", {}) or {}

    def _mcp_records(self, step: Step) -> tuple[dict, dict, Path] | None:
        """(servers Claude has, servers claude-hr registered, path of the latter), or None after marking `step` failed
        because ~/.claude.json or our own record is unparsable (neither is ever overwritten)."""
        managed_file = paths.state_dir() / "mcp-managed.json"
        try:
            have = self._registered_mcp()
            managed = read_json(managed_file, {}) or {}
        except JsonFileError as exc:
            step.ok = False
            step.add(str(exc))
            step.add(
                "fix the JSON by hand (claude-hr never overwrites an unparsable file), then re-run"
            )
            return None
        return have, managed, managed_file

    def register_mcp(self) -> Step:
        step = Step("mcp")
        records = self._mcp_records(step)
        if records is None:
            return step
        have, managed, managed_file = records
        h = self.hr.h
        wanted: dict[str, dict] = {}
        if bool(h.get("enabled", True)) and bool(h.get("mcp", True)) and self.hr.binary():
            wanted["headroom"] = self.hr.mcp_server_spec()
        if (
            bool(h.get("enabled", True))
            and str(h.get("code_memory", "serena")) == "serena"
            and str(h.get("code_memory_scope", "user")) == "user"
        ):
            if not self.dry and not self.hr.serena_pin():
                pin = self.hr.resolve_serena_pin(float(self.cfg.get("updates.cooldown_days", 5)))
                step.add(
                    f"serena pinned to {pin}"
                    if pin
                    else "serena left unpinned (PyPI unreachable): set headroom.serena_version"
                )
            wanted["serena"] = self.hr.serena_spec()
        for name, spec in wanted.items():
            if have.get(name) == spec:
                step.add(f"{name}: already registered")
                managed[name] = spec
                continue
            if name in have and name not in managed:
                step.add(
                    f"{name}: a server with this name exists and was not registered by claude-hr; leaving it alone"
                )
                continue
            step.changed = True
            step.add(f"{name}: {'would register' if self.dry else 'register'} (user scope)")
            if self.dry:
                continue
            if name in have:
                self._claude("mcp", "remove", "--scope", "user", name)
            r = self._claude("mcp", "add-json", "--scope", "user", name, json.dumps(spec))
            if r.returncode != 0:
                step.ok = False
                step.add(f"  failed: {(r.stderr or r.stdout).strip()[:200]}")
            else:
                managed[name] = spec
        if not self.dry:
            write_json(managed_file, managed)
        return step

    def unregister_mcp(self) -> Step:
        """Remove only servers claude-hr registered itself; a same-named server the user added is left alone."""
        step = Step("mcp-remove")
        records = self._mcp_records(step)
        if records is None:
            return step
        have, managed, managed_file = records
        for name in list(managed):
            if name in have:
                step.changed = True
                if not self.dry:
                    self._claude("mcp", "remove", "--scope", "user", name)
                step.add(f"{name}: removed")
        if not self.dry:
            managed_file.unlink(missing_ok=True)
        return step

    # ---- plugins --------------------------------------------------------------------------
    def plugin_plan(self) -> dict:
        with (paths.package_data_dir() / "plugins.toml").open("rb") as fh:
            return tomllib.load(fh)

    def profile_plugins(self, plan: dict, profile: str, _seen: set[str] | None = None) -> list[str]:
        _seen = _seen or set()
        if profile in _seen:
            return []
        _seen.add(profile)
        prof = plan.get("profiles", {}).get(profile)
        if prof is None:
            raise KeyError(
                f"unknown plugin profile '{profile}' (have: {', '.join(plan.get('profiles', {}))})"
            )
        out = self.profile_plugins(plan, prof["extends"], _seen) if prof.get("extends") else []
        for p in prof.get("plugins", []):
            if p not in out:
                out.append(p)
        return out

    def effective_plugins(self, plan: dict, profile: str) -> list[str]:
        """The profile's plugins that `plugins sync` really installs: those from a marketplace whose `requires` flag is off
        (headroom-snip with headroom.install_plugins = false) are left out. doctor uses the same list, so it never asks
        for something sync will not do."""
        gates = {n: m.get("requires") for n, m in plan.get("marketplaces", {}).items()}
        out = []
        for spec in self.profile_plugins(plan, profile):
            req = gates.get(spec.partition("@")[2])
            if req and not self.cfg.flag(req):
                continue
            out.append(spec)
        return out

    def _installed_plugins(self) -> list[dict]:
        r = self._claude("plugin", "list", "--json")
        try:
            data = json.loads(r.stdout) if r.returncode == 0 else []
        except json.JSONDecodeError:
            return []
        if isinstance(data, dict):  # some builds wrap the list: {"plugins": [...]}
            data = data.get("plugins", [])
        return (
            [p for p in data if isinstance(p, dict) and isinstance(p.get("id"), str)]
            if isinstance(data, list)
            else []
        )

    def _known_marketplaces(self) -> set[str]:
        r = self._claude("plugin", "marketplace", "list", "--json")
        try:
            data = json.loads(r.stdout) if r.returncode == 0 else []
        except json.JSONDecodeError:
            return set()
        names = set()
        for item in data if isinstance(data, list) else data.get("marketplaces", []):
            if isinstance(item, dict) and item.get("name"):
                names.add(item["name"])
            elif isinstance(item, str):
                names.add(item)
        return names

    def sync_plugins(self, profile: str | None = None, prune: bool = False) -> Step:
        step = Step("plugins")
        if not self.claude:
            step.ok = False
            step.add("claude not found")
            return step
        plan = self.plugin_plan()
        profile = profile or str(self.cfg.get("plugins.profile", "standard"))
        try:
            wanted = self.profile_plugins(plan, profile)
            effective = self.effective_plugins(plan, profile)
        except KeyError as exc:
            step.ok = False
            step.add(str(exc))
            return step
        gates = {n: m.get("requires") for n, m in plan.get("marketplaces", {}).items()}
        known = self._known_marketplaces()
        repo = paths.repo_dir()
        for name, mp in plan.get("marketplaces", {}).items():
            req = gates.get(name)
            if req and not self.cfg.flag(req):
                continue
            src = str(mp["source"]).replace("@repo", str(repo))
            if name in known:
                continue
            step.changed = True
            step.add(f"marketplace {name}: {'would add' if self.dry else 'add'} {src}")
            if self.dry:
                continue
            r = self._claude("plugin", "marketplace", "add", "--scope", "user", src, timeout=300)
            if r.returncode != 0:
                step.ok = False
                step.add(f"  failed: {(r.stderr or r.stdout).strip()[:200]}")
        if "claude-hr" in known and not self.dry:
            # our own marketplace is a local checkout: re-read it so a newer repo version is installable
            self._claude("plugin", "marketplace", "update", "claude-hr", timeout=120)
        installed = self._installed_plugins()
        have_names = {p["id"].split("@")[0] for p in installed if p.get("scope") == "user"}
        for spec in effective:
            pname = spec.partition("@")[0]
            if pname in have_names:
                step.add(f"{pname}: installed")
                continue
            step.changed = True
            step.add(f"{spec}: {'would install' if self.dry else 'install'}")
            if self.dry:
                continue
            r = self._claude("plugin", "install", spec, "--scope", "user", "-y", timeout=600)
            if r.returncode != 0:
                step.ok = False
                step.add(f"  failed: {(r.stderr or r.stdout).strip()[:300]}")
        if prune:
            keep = {w.split("@")[0] for w in wanted}
            for p in installed:
                if p.get("scope") == "user" and p["id"].split("@")[0] not in keep:
                    step.changed = True
                    step.add(
                        f"{p['id']}: {'would uninstall' if self.dry else 'uninstall'} (not in profile)"
                    )
                    if not self.dry:
                        self._claude("plugin", "uninstall", p["id"], "--scope", "user", "-y")
        return step

    def update_marketplaces(self) -> Step:
        step = Step("plugin-update")
        r = self._claude("plugin", "marketplace", "update", timeout=600)
        step.add((r.stdout or r.stderr).strip()[-300:] or "marketplaces refreshed")
        for p in self._installed_plugins():
            if p.get("scope") == "user" and not self.dry:
                # no -y: an unattended job must never auto-accept a marketplace-declared command
                u = self._claude("plugin", "update", p["id"], "--scope", "user", timeout=300)
                out = (u.stdout or u.stderr).strip().splitlines()
                if out and "up to date" not in out[-1].lower() and "already" not in out[-1].lower():
                    step.changed = True
                    step.add(f"{p['id']}: {out[-1][:140]}")
        return step
