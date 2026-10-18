"""State one cause from the catalog labels and the timeline."""

from __future__ import annotations

from datetime import datetime, timezone

from chainlog_ai.case import Case, Event
from chainlog_ai.style import CAUSE_COLOR, SEVERITY_COLOR, WINDOW_COLOR, paint, paint_redacted, paint_severity, paint_source
from chainlog_ai.timeline import timeline_rows

_SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}
_SOURCE_ORDER = {
    "kubernetes": 0,
    "kubernetes-previous": 1,
    "execution": 2,
    "consensus": 3,
    "validator": 4,
    "builder": 5,
}
_CLUSTER = {"kubernetes", "kubernetes-previous"}
_NAMED = {"execution", "consensus", "validator", "builder"}


def explain(case: Case, *, color: bool = False, width: int | None = None) -> tuple[str, bool]:
    """Return the cause text and whether a cause was cited.

    The cause is the earliest critical failure on the timeline, or the earliest
    warning when the case has no critical line. Each sentence names the source
    and the time of the timeline row printed under it. ``color`` and ``width``
    match the terminal timeline.
    """

    rows = timeline_rows(case, color=color, width=width)
    failures = [(index, event, text) for index, (event, text) in enumerate(rows) if event.failure_class]
    if not failures:
        notice = "evidence is not enough to state a cause"
        if color:
            notice = paint(notice, SEVERITY_COLOR["warning"])
        return f"case {case.id}\n{notice}", False
    _index, event, text = min(failures, key=_cause_key)
    sentence = _sentence(event, color=color)
    window = "window: unknown" if event.time is None else f"window: {event.time} .. {event.time}"
    if color:
        sentence = paint("cause: ", CAUSE_COLOR) + sentence
        window = paint(window, WINDOW_COLOR)
    else:
        sentence = f"cause: {sentence}"
    body = "\n".join([f"case {case.id}", sentence, window, text])
    return body, True


def _sentence(event: Event, *, color: bool) -> str:
    source = _cause_source(event)
    kind = event.failure_class or ""
    if color:
        source = paint_source(source, event.source)
        kind = paint_severity(kind, event.severity)
    if event.time is None:
        sentence = f"{source} failed with {kind}."
    else:
        sentence = f"{source} failed with {kind} at {event.time}."
    return paint_redacted(sentence) if color else sentence


def _cause_source(event: Event) -> str:
    if event.source in _CLUSTER:
        return "cluster"
    if event.source in _NAMED:
        return event.source
    return event.source


def _cause_key(item: tuple[int, Event, str]) -> tuple[int, bool, datetime, int, int]:
    index, event, _text = item
    rank = _SEVERITY_RANK.get(event.severity or "", 3)
    stamp = _stamp(event)
    return (rank, stamp is None, stamp or datetime.max.replace(tzinfo=timezone.utc), _source_rank(event), index)


def _source_rank(event: Event) -> int:
    return _SOURCE_ORDER.get(event.source, len(_SOURCE_ORDER))


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
