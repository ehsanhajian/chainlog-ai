"""Open a case from a ValidatorPulse or Prometheus alert."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from chainlog_ai.case import Case, Event, append_events
from chainlog_ai.ingest import InputError
from chainlog_ai.redact import redact_excerpt

_HOUR = timedelta(hours=1)
_AFTER = timedelta(minutes=15)


def read_alert(case: Case, path: Path) -> None:
    """Store each alert on the timeline and set the default window once."""

    file_path = path.expanduser()
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc.strerror}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InputError(f"cannot read alert {path}") from exc
    found = _alerts(data)
    if not found:
        raise InputError(f"alert file has no alert: {path}")
    resolved = str(file_path.resolve())
    events: list[Event] = []
    stamps: list[datetime] = []
    for index, item in enumerate(found):
        name, status, fired = _fields(item, path)
        stamps.append(fired)
        excerpt = redact_excerpt(name if not status else f"{name} {status}")
        events.append(
            Event(
                source="alert",
                client=None,
                path=resolved,
                byte_start=index,
                byte_end=index + 1,
                excerpt=excerpt,
                time=_iso(fired),
            )
        )
    if case.since is None or case.until is None:
        case.since = _iso(min(stamps) - _HOUR)
        case.until = _iso(max(stamps) + _AFTER)
    append_events(case, events)


def _alerts(data: object) -> list[dict[str, object]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    raw = data.get("alerts")
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return [data]


def _fields(item: dict[str, object], path: Path) -> tuple[str, str, datetime]:
    name = _name(item)
    if not name:
        raise InputError(f"alert file has no alert: {path}")
    raw_time = item.get("startsAt")
    if not isinstance(raw_time, str) or not raw_time.strip():
        raise InputError(f"alert file has no alert: {path}")
    status = item.get("status")
    if not isinstance(status, str) or not status.strip():
        status = ""
    return name, status.strip(), _stamp(raw_time.strip(), path)


def _name(item: dict[str, object]) -> str:
    labels = item.get("labels")
    if isinstance(labels, dict):
        named = labels.get("alertname")
        if isinstance(named, str) and named.strip():
            return named.strip()
    named = item.get("alertname")
    if isinstance(named, str) and named.strip():
        return named.strip()
    return ""


def _stamp(text: str, path: Path) -> datetime:
    parsed_text = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(parsed_text)
    except ValueError as exc:
        raise InputError(f"cannot read alert {path}") from exc
    if parsed.tzinfo is None:
        raise InputError(f"alert time needs a timezone: {path}")
    return parsed.astimezone(timezone.utc)


def _iso(stamp: datetime) -> str:
    return stamp.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
