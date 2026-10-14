"""Print one case in time order across every source."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from chainlog_ai.case import Case, Event

# More than a few seconds between a client clock and a cluster event.
CLOCK_GAP = timedelta(seconds=5)

_SOURCE_ORDER = {
    "kubernetes": 0,
    "kubernetes-previous": 1,
    "execution": 2,
    "consensus": 3,
    "validator": 4,
    "builder": 5,
}
_CLUSTER = {"kubernetes", "kubernetes-previous"}
_CLIENTS = {"execution", "consensus", "validator", "builder"}


class TimelineError(Exception):
    """The timeline window cannot be used."""


def render_timeline(case: Case, *, since: str | None = None, until: str | None = None) -> str:
    """Return the case in time order. ``since`` and ``until`` are inclusive."""

    start = _bound(since, "--since")
    end = _bound(until, "--until")
    if start is not None and end is not None and start > end:
        raise TimelineError("--since is after --until")
    rows = _ordered(case.events, start, end)
    lines = [f"case {case.id}"]
    previous: Event | None = None
    for event in rows:
        gap = _clock_gap(previous, event)
        if gap is not None:
            lines.append(gap)
        lines.append(_row(event))
        previous = event
    return "\n".join(lines)


def _ordered(
    events: list[Event],
    start: datetime | None,
    end: datetime | None,
) -> list[Event]:
    windowed = [event for event in events if _in_window(event, start, end)]
    timed = [event for event in windowed if event.time is not None]
    undated = [event for event in windowed if event.time is None]
    if start is None and end is None:
        ranked = timed + undated
    else:
        ranked = timed
    return sorted(ranked, key=lambda event: _sort_key(event, events))


def _in_window(event: Event, start: datetime | None, end: datetime | None) -> bool:
    if start is None and end is None:
        return True
    stamp = _event_time(event)
    if stamp is None:
        return False
    if start is not None and stamp < start:
        return False
    if end is not None and stamp > end:
        return False
    return True


def _sort_key(event: Event, events: list[Event]) -> tuple[datetime, int, int]:
    stamp = _event_time(event) or datetime.max.replace(tzinfo=timezone.utc)
    return (stamp, _SOURCE_ORDER.get(event.source, len(_SOURCE_ORDER)), events.index(event))


def _row(event: Event) -> str:
    when = event.time or "-"
    client = event.client or "-"
    kind = event.failure_class or "unclassified"
    return f"{when} {event.source} {client} {kind} {event.excerpt}"


def _clock_gap(previous: Event | None, event: Event) -> str | None:
    if previous is None or previous.time is None or event.time is None:
        return None
    if _is_cluster(previous) == _is_cluster(event):
        return None
    if not (_is_client_clock(previous) or _is_client_clock(event)):
        return None
    earlier = _event_time(previous)
    later = _event_time(event)
    if earlier is None or later is None:
        return None
    delta = abs(later - earlier)
    if delta <= CLOCK_GAP:
        return None
    seconds = round(delta.total_seconds())
    return f"clock gap {seconds}s between {previous.source} and {event.source}"


def _is_cluster(event: Event) -> bool:
    return event.source in _CLUSTER and event.client is None


def _is_client_clock(event: Event) -> bool:
    if event.source in _CLIENTS:
        return True
    return event.source in _CLUSTER and event.client is not None


def _event_time(event: Event) -> datetime | None:
    if event.time is None:
        return None
    try:
        return _parse_time(event.time)
    except TimelineError:
        return None


def _bound(value: str | None, flag: str) -> datetime | None:
    if value is None:
        return None
    try:
        return _parse_time(value)
    except TimelineError as exc:
        raise TimelineError(f"invalid time for {flag}: {value}") from exc


def _parse_time(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise TimelineError(f"invalid time: {value}") from exc
    if parsed.tzinfo is None:
        raise TimelineError(f"invalid time: {value}")
    return parsed.astimezone(timezone.utc)
