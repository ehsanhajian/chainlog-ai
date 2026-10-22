"""Terminal colors for severity, source, and redacted text."""

from __future__ import annotations

import re

RESET = "\033[0m"
REDACTED_COLOR = "\033[1;31m"
GAP_COLOR = "\033[1;33m"
CAUSE_COLOR = "\033[1m"
WINDOW_COLOR = "\033[2m"
SEVERITY_COLOR = {"info": "\033[32m", "warning": "\033[33m", "critical": "\033[31m"}
SOURCE_COLOR = {
    "kubernetes": "\033[34m",
    "kubernetes-previous": "\033[34m",
    "execution": "\033[36m",
    "consensus": "\033[35m",
    "validator": "\033[96m",
    "builder": "\033[94m",
    "alert": "\033[93m",
}
_REDACTED = re.compile(re.escape("[redacted]"))


def paint(text: str, color: str) -> str:
    return f"{color}{text}{RESET}"


def paint_source(label: str, source: str) -> str:
    return paint(label, SOURCE_COLOR.get(source, "\033[36m"))


def paint_severity(label: str, severity: str | None) -> str:
    color = SEVERITY_COLOR.get(severity or "")
    if color is None:
        return label
    return paint(label, color)


def paint_redacted(text: str) -> str:
    return _REDACTED.sub(lambda match: paint(match.group(0), REDACTED_COLOR), text)
