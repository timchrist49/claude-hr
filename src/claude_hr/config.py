"""Layered configuration: packaged defaults < system file < user file < CLAUDE_HR_* environment.

Environment overrides use `CLAUDE_HR_<SECTION>__<KEY>` (double underscore = nesting), e.g.
`CLAUDE_HR_HEADROOM__PORT=9000`, `CLAUDE_HR_RESOURCES__MAX_SESSIONS=4`. Values are parsed as TOML
scalars when possible (true/false/ints/quoted strings) and fall back to plain strings.
"""

from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path
from typing import Any

from . import paths


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _load_toml(path: Path) -> dict:
    try:
        # utf-8-sig: Windows PowerShell 5.1 (`Set-Content -Encoding UTF8`) and some editors write a BOM, which tomllib rejects
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path} is not valid UTF-8: {exc}") from exc
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid TOML in {path}: {exc}") from exc


def _parse_scalar(raw: str) -> Any:
    try:
        return tomllib.loads(f"v = {raw}")["v"]
    except tomllib.TOMLDecodeError:
        return raw


def _env_overrides(environ: dict[str, str]) -> dict:
    out: dict = {}
    for name, raw in environ.items():
        if not name.startswith("CLAUDE_HR_") or "__" not in name:
            continue
        parts = [p.lower() for p in name[len("CLAUDE_HR_") :].split("__") if p]
        if len(parts) < 2:
            continue
        cur = out
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        cur[parts[-1]] = _parse_scalar(raw)
    return out


class Config:
    def __init__(self, data: dict):
        self.data = data

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def section(self, name: str) -> dict:
        value = self.data.get(name, {})
        return value if isinstance(value, dict) else {}

    def flag(self, dotted: str, default: bool = False) -> bool:
        value = self.get(dotted, default)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)


def defaults_path() -> Path:
    return paths.package_data_dir() / "defaults.toml"


def load(environ: dict[str, str] | None = None, *, with_user: bool = True) -> Config:
    environ = os.environ if environ is None else environ
    data = _load_toml(defaults_path())
    data = _deep_merge(data, _load_toml(paths.system_config_file()))
    if with_user:
        data = _deep_merge(data, _load_toml(paths.user_config_file()))
    data = _deep_merge(data, _env_overrides(dict(environ)))
    return Config(data)
