"""The cgroup v2 scopes claude-hr creates (Linux): read what they use and what they are capped at, and raise a cap.

The process wrapper (resources.WRAPPER_TEMPLATE) puts Claude's background service and every session it hosts into ONE scope,
`claude-hr-<pid>.scope`, for as long as the service runs (days). The caps are properties of that running scope, so they
outlive any change to the wrapper script or to the machine: this module shows them as they are now.

Everything here is read-only except `raise_caps`, and everything fails soft (no cgroup v2, a scope that vanished while being
read, no permission -> an empty or partial answer, never an exception). Standard library only; imported lazily by doctor and
the monitor, never by the launcher.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import paths, procutil

CGROUP_ROOT = Path("/sys/fs/cgroup")
PRESSURE_ROOT = Path("/proc/pressure")
_GIB = 1024**3


@dataclass
class Scope:
    name: str  # claude-hr-1234.scope
    current_gb: float = 0.0
    high_gb: float = 0.0  # soft limit: the kernel throttles and reclaims above it. 0 = none
    max_gb: float = 0.0  # hard limit: the kernel kills above it. 0 = none
    pids: int = 0
    events: dict[str, int] = field(
        default_factory=dict
    )  # memory.events: low high max oom oom_kill ...
    cpu_max: str = (
        ""  # raw cpu.max; empty when the cpu controller is not delegated to the user manager
    )
    working_gb: float | None = (
        None  # memory.current minus reclaimable file cache; None = memory.stat unreadable
    )

    @property
    def used_gb(self) -> float:
        """What the scope really holds. memory.current also counts clean page cache, which a busy scope keeps up to its soft
        limit for as long as it lives (reproduced: 99.5% of a scope's 200 MB was cache, pressure 0.00), so it reads as 'full'
        without anything being wrong. The kernel gives that cache back on demand; this is the Kubernetes working-set rule."""
        return self.current_gb if self.working_gb is None else self.working_gb

    @property
    def ceiling_gb(self) -> float:
        """What the scope's users run into first: the soft limit if there is one, else the hard limit."""
        return self.high_gb or self.max_gb

    @property
    def cpu_limited(self) -> bool:
        return bool(self.cpu_max) and not self.cpu_max.startswith("max")


def _uid() -> int:
    return os.getuid() if hasattr(os, "getuid") else 0  # no getuid on Windows (tests run there too)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _gb(text: str) -> float:
    """Bytes or the word `max` (no limit) -> GB; an unreadable value is 'no limit'."""
    try:
        return int(text) / _GIB
    except ValueError:
        return 0.0


def _flat(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        try:
            out[key] = int(value)
        except ValueError:
            continue
    return out


def scopes(root: Path = CGROUP_ROOT, uid: int | None = None) -> list[Scope]:
    """Our running scopes. systemd puts a transient user scope at
    user.slice/user-<uid>.slice/user@<uid>.service/app.slice/<unit>.scope (verified on systemd 249); the shorter path is
    what older layouts use."""
    if not paths.IS_LINUX:
        return []
    uid = _uid() if uid is None else uid
    base = root / "user.slice" / f"user-{uid}.slice" / f"user@{uid}.service"
    out: list[Scope] = []
    try:
        found = sorted([*base.glob("app.slice/claude-hr-*.scope"), *base.glob("claude-hr-*.scope")])
    except OSError:
        return out
    for d in found:
        if not d.is_dir():
            continue
        try:
            pids = int(_read(d / "pids.current") or 0)
        except ValueError:
            pids = 0
        current = _read(d / "memory.current")
        inactive = _flat(_read(d / "memory.stat")).get("inactive_file")
        working = None
        if inactive is not None and current.isdigit():
            working = max(0, int(current) - inactive) / _GIB
        out.append(
            Scope(
                name=d.name,
                current_gb=_gb(current),
                high_gb=_gb(_read(d / "memory.high")),
                max_gb=_gb(_read(d / "memory.max")),
                pids=pids,
                events=_flat(_read(d / "memory.events")),
                cpu_max=_read(d / "cpu.max"),
                working_gb=working,
            )
        )
    return out


def pressure(root: Path = PRESSURE_ROOT) -> dict[str, dict[str, float]]:
    """Pressure stall information (PSI): {"memory": {"some": 12.0, "full": 3.1, "some_us": 9e6, "full_us": 2e6}, "io": ...,
    "cpu": ...}. `some` / `full` are the percentage of the last 60 seconds that tasks were stalled waiting for that resource,
    `*_us` the microseconds stalled since boot (two readings give the stall over the time between them, which a 60 second
    average read every 15 minutes cannot). Empty without PSI (kernel < 4.20, or booted with psi=0). `some` = at least one task
    stalled, `full` = all of them (cpu has no `full` on older kernels)."""
    out: dict[str, dict[str, float]] = {}
    for kind in ("memory", "io", "cpu"):
        row: dict[str, float] = {}
        for line in _read(root / kind).splitlines():
            parts = line.split()
            if len(parts) < 3 or parts[0] not in {"some", "full"}:
                continue
            for item in parts[1:]:
                key, _, value = item.partition("=")
                if key in {"avg60", "total"}:
                    try:
                        row[parts[0] if key == "avg60" else f"{parts[0]}_us"] = float(value)
                    except ValueError:
                        pass
        if row:
            out[kind] = row
    return out


def user_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """`systemctl --user` needs the user's runtime dir; a job started from cron does not have it."""
    out = dict(os.environ if env is None else env)
    if not out.get("XDG_RUNTIME_DIR"):
        rt = f"/run/user/{_uid()}"
        if os.path.exists(f"{rt}/bus"):
            out["XDG_RUNTIME_DIR"] = rt
    return out


def raise_caps(scope: Scope, max_mb: int, high_mb: int) -> str:
    """Raise a RUNNING scope's memory caps to at least `max_mb` / `high_mb` (a runtime property: it ends with the scope).
    Never lowers anything and never puts a cap on a scope that has none; `high_mb` 0 (the config wants no soft limit) lifts
    the one the scope has. Returns "" when nothing was needed, a one-line description of the change, or "failed: ..." when
    systemd refused."""
    props: list[str] = []
    if scope.max_gb > 0 and max_mb > scope.max_gb * 1024 * 1.05:
        props.append(f"MemoryMax={max_mb}M")
    if scope.high_gb > 0 and high_mb == 0:
        props.append("MemoryHigh=infinity")
    elif scope.high_gb > 0 and high_mb > scope.high_gb * 1024 * 1.05:
        props.append(f"MemoryHigh={high_mb}M")
    if not props:
        return ""
    res = procutil.run(
        ["systemctl", "--user", "set-property", "--runtime", scope.name, *props],
        timeout=20,
        env=user_env(),
    )
    if res.returncode != 0:
        return f"failed: {scope.name} {' '.join(props)} (rc {res.returncode})"
    was = f"{scope.max_gb:.1f}/{scope.high_gb:.1f} GB"
    return f"{scope.name}: {' '.join(props)} (was MemoryMax/High {was})"
