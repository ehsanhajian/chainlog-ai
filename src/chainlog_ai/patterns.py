"""Match a case against earlier cases stored on this machine."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from chainlog_ai.case import Case, Event, case_file, list_cases, load_case
from chainlog_ai.classify import classify_case

_VERSION = re.compile(r"version[:=]\s*([A-Za-z0-9][A-Za-z0-9._/\-]*)", re.IGNORECASE)
_CLUSTER = {"kubernetes", "kubernetes-previous"}
_FAR = datetime.max.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class _Stream:
    client: str
    version: str
    classes: tuple[str, ...]


def find_patterns(case_id: str) -> str:
    """Classify local cases and list earlier ones with a similar class order."""

    ids = list_cases()
    if case_id not in ids:
        load_case(case_id)
    for item in ids:
        classify_case(item)
    current = load_case(case_id)
    others = [load_case(item) for item in ids if item != case_id]
    earlier = sorted(
        (case for case in others if _is_earlier(case, current)),
        key=lambda case: (_earliest(case) is None, _earliest(case) or _FAR, case.id),
    )
    matched = [case.id for case in earlier if _matches(current, case)]
    return _format(current.id, matched, has_earlier=bool(earlier))


def _format(case_id: str, matched: list[str], *, has_earlier: bool) -> str:
    if not has_earlier:
        return f"case {case_id}\nfirst time on this machine"
    count = len(matched)
    noun = "case" if count == 1 else "cases"
    lines = [f"case {case_id}", f"{count} earlier {noun}", *matched]
    return "\n".join(lines)


def _matches(current: Case, earlier: Case) -> bool:
    for left in _streams(current):
        for right in _streams(earlier):
            if left.client == right.client and left.version == right.version and _similar(left.classes, right.classes):
                return True
    return False


def _streams(case: Case) -> list[_Stream]:
    grouped: dict[str, list[tuple[int, Event]]] = {}
    for index, event in enumerate(case.events):
        grouped.setdefault(_client_key(event), []).append((index, event))
    streams: list[_Stream] = []
    for client, items in grouped.items():
        ordered = sorted(items, key=lambda item: _event_key(item[1], item[0]))
        version = "unknown"
        classes: list[str] = []
        for _index, event in ordered:
            if version == "unknown":
                found = _VERSION.search(event.excerpt)
                if found is not None:
                    version = found.group(1)
            if event.failure_class and (not classes or classes[-1] != event.failure_class):
                classes.append(event.failure_class)
        if classes:
            streams.append(_Stream(client, version, tuple(classes)))
    return streams


def _similar(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    if not (set(left) & set(right)):
        return False
    return _is_subsequence(left, right) or _is_subsequence(right, left)


def _is_subsequence(needle: tuple[str, ...], haystack: tuple[str, ...]) -> bool:
    position = 0
    for name in haystack:
        if position < len(needle) and name == needle[position]:
            position += 1
    return position == len(needle)


def _client_key(event: Event) -> str:
    if event.client:
        return event.client
    if event.source in _CLUSTER:
        return "cluster"
    return event.source


def _is_earlier(other: Case, current: Case) -> bool:
    left = _earliest(other)
    right = _earliest(current)
    if left is not None and right is not None and left != right:
        return left < right
    return _mtime(other) < _mtime(current)


def _earliest(case: Case) -> datetime | None:
    stamps = [_stamp(event) for event in case.events]
    known = [stamp for stamp in stamps if stamp is not None]
    if not known:
        return None
    return min(known)


def _mtime(case: Case) -> float:
    return case_file(case.id).stat().st_mtime


def _event_key(event: Event, index: int) -> tuple[bool, datetime, int]:
    stamp = _stamp(event)
    return (stamp is None, stamp or datetime.max.replace(tzinfo=timezone.utc), index)


def _stamp(event: Event) -> datetime | None:
    if event.time is None:
        return None
    text = event.time.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)
