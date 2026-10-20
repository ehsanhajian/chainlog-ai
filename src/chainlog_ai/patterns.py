"""Match a case against earlier cases, and compare a redacted signature across hosts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from chainlog_ai.case import Case, Event, case_file, list_cases, load_case
from chainlog_ai.classify import classify_case

_BANNER = re.compile(r"\bStarting\b|\bstarted\b|\bversion\s*[:=]", re.IGNORECASE)
_LABELED = re.compile(r"version\s*[:=]\s*([A-Za-z0-9][A-Za-z0-9._/\-]*)", re.IGNORECASE)
_EMBEDDED = re.compile(
    r"\b([A-Za-z][A-Za-z0-9]*\/v?\d+\.\d+\.\d+(?:[-.+][A-Za-z0-9.]+)*|v\d+\.\d+\.\d+(?:[-.+][A-Za-z0-9.]+)*)\b"
)
_BARE = re.compile(r"\b(\d+\.\d+\.\d+(?:[-.+][A-Za-z0-9.]+)*)\b")
_CLUSTER = {"kubernetes", "kubernetes-previous"}
_FAR = datetime.max.replace(tzinfo=timezone.utc)


class SignatureError(Exception):
    """The signature file cannot be used."""


@dataclass(frozen=True)
class _Stream:
    client: str
    version: str
    classes: tuple[str, ...]
    previous: str | None = None


def find_patterns(case_id: str) -> str:
    """Classify local cases and list earlier ones with a similar class order."""

    ids = list_cases()
    if case_id not in ids:
        load_case(case_id)
    for item in ids:
        classify_case(item)
    current = load_case(case_id)
    current_streams = _streams(current)
    others = [load_case(item) for item in ids if item != case_id]
    earlier = sorted(
        (case for case in others if _is_earlier(case, current)),
        key=lambda case: (_earliest(case) is None, _earliest(case) or _FAR, case.id),
    )
    matched = [case.id for case in earlier if _streams_match(current_streams, _streams(case))]
    return _format(current.id, matched, has_earlier=bool(earlier), streams=current_streams)


def _format(case_id: str, matched: list[str], *, has_earlier: bool, streams: list[_Stream]) -> str:
    lines = [f"case {case_id}", *[_version_line(stream) for stream in streams]]
    if not has_earlier:
        lines.append("first time on this machine")
        return "\n".join(lines)
    count = len(matched)
    noun = "case" if count == 1 else "cases"
    lines.append(f"{count} earlier {noun}")
    lines.extend(matched)
    return "\n".join(lines)


def _version_line(stream: _Stream) -> str:
    if stream.previous:
        return f"version {stream.client} {stream.previous} to {stream.version}"
    return f"version {stream.client} {stream.version}"


def write_signature(case_id: str, path: Path) -> str:
    """Write a signature of class, client, version, and class order. No log lines."""

    classify_case(case_id)
    streams = _streams(load_case(case_id))
    payload = {"streams": [_stream_dict(stream) for stream in streams]}
    try:
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        raise SignatureError(f"cannot write signature {path}") from exc
    return "\n".join([f"case {case_id}", *[_version_line(stream) for stream in streams], f"signature {path}"])


def match_signature(path: Path) -> str:
    """Report whether any local case matches a signature from another host."""

    wanted = _read_signature(path)
    ids = list_cases()
    for item in ids:
        classify_case(item)
    cases = sorted(
        (load_case(item) for item in ids),
        key=lambda case: (_earliest(case) is None, _earliest(case) or _FAR, case.id),
    )
    matched = [case.id for case in cases if _streams_match(wanted, _streams(case))]
    if not matched:
        return "no match"
    return "\n".join(["match", *matched])


def _streams_match(left: list[_Stream], right: list[_Stream]) -> bool:
    for one in left:
        for other in right:
            if one.client == other.client and one.version == other.version and _similar(one.classes, other.classes):
                return True
    return False


def _stream_dict(stream: _Stream) -> dict[str, object]:
    payload: dict[str, object] = {
        "client": stream.client,
        "version": stream.version,
        "classes": list(stream.classes),
    }
    if stream.previous:
        payload = {
            "client": stream.client,
            "version": stream.version,
            "previous": stream.previous,
            "classes": list(stream.classes),
        }
    return payload


def _read_signature(path: Path) -> list[_Stream]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SignatureError(f"cannot read signature {path}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("streams"), list):
        raise SignatureError(f"cannot read signature {path}")
    streams: list[_Stream] = []
    for item in data["streams"]:
        if not isinstance(item, dict):
            raise SignatureError(f"cannot read signature {path}")
        client = item.get("client")
        version = item.get("version")
        classes = item.get("classes")
        if not isinstance(client, str) or not client:
            raise SignatureError(f"cannot read signature {path}")
        if not isinstance(version, str) or not version:
            raise SignatureError(f"cannot read signature {path}")
        if not isinstance(classes, list) or not classes or not all(isinstance(name, str) and name for name in classes):
            raise SignatureError(f"cannot read signature {path}")
        previous = item.get("previous")
        if previous is not None and (not isinstance(previous, str) or not previous or previous == version):
            raise SignatureError(f"cannot read signature {path}")
        streams.append(_Stream(client, version, tuple(classes), previous=previous))
    return streams


def _streams(case: Case) -> list[_Stream]:
    grouped: dict[str, list[tuple[int, Event]]] = {}
    for index, event in enumerate(case.events):
        grouped.setdefault(_client_key(event), []).append((index, event))
    streams: list[_Stream] = []
    for client, items in grouped.items():
        streams.extend(_client_streams(client, items))
    streams.sort(key=lambda stream: stream.client)
    return streams


def _client_streams(client: str, items: list[tuple[int, Event]]) -> list[_Stream]:
    ordered = sorted(items, key=lambda item: _event_key(item[1], item[0]))
    streams: list[_Stream] = []
    classes: list[str] = []
    current: str | None = None
    segment_version: str | None = None
    segment_previous: str | None = None

    def flush() -> None:
        nonlocal classes
        if not classes:
            return
        streams.append(
            _Stream(client, segment_version or "unknown", tuple(classes), previous=segment_previous)
        )
        classes = []

    for _index, event in ordered:
        found = _version_on(event.excerpt)
        if found is not None and found != current:
            if classes:
                flush()
                segment_previous = current or "unknown"
            elif current is not None:
                segment_previous = current
            else:
                segment_previous = None
            segment_version = found
            current = found
        if event.failure_class and (not classes or classes[-1] != event.failure_class):
            classes.append(event.failure_class)
    flush()
    return streams


def _version_on(excerpt: str) -> str | None:
    """Return the version printed on a banner or startup line."""

    if _BANNER.search(excerpt) is None:
        return None
    labeled = _LABELED.search(excerpt)
    if labeled is not None:
        return labeled.group(1)
    embedded = _EMBEDDED.search(excerpt)
    if embedded is not None:
        return embedded.group(1)
    bare = _BARE.search(excerpt)
    if bare is not None:
        return bare.group(1)
    return None


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
