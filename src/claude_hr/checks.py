"""Shared result type for doctor-style checks."""

from __future__ import annotations

from dataclasses import dataclass

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    fix: str = ""

    def line(self, color: bool = False) -> str:
        tag = {OK: "OK  ", WARN: "WARN", FAIL: "FAIL", INFO: "INFO"}[self.status]
        if color:
            c = {OK: "32", WARN: "33", FAIL: "31", INFO: "36"}[self.status]
            tag = f"\x1b[{c}m{tag}\x1b[0m"
        text = f"[{tag}] {self.name}: {self.detail}"
        if self.fix and self.status in {WARN, FAIL}:
            text += f"\n         fix: {self.fix}"
        return text
