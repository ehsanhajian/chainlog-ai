"""State one cause from the catalog labels and the timeline."""

from __future__ import annotations

from datetime import datetime, timezone

from chainlog_ai.case import Case, Event
from chainlog_ai.style import CAUSE_COLOR, SEVERITY_COLOR, WINDOW_COLOR, paint, paint_redacted, paint_severity, paint_source
from chainlog_ai.timeline import timeline_rows

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
# A miss, a restart, or a probe failure is a symptom of an earlier failure.
_SYMPTOMS = {"missed-attestation", "missed-proposal", "crash-loop", "probe-failed"}
# Pressure and a drifting clock make a failure worse. They are the trigger only
# when the case has no class that explains the failure.
_CONDITIONS = {"cpu-pressure", "memory-pressure", "disk-pressure", "clock-not-synced"}
_Row = tuple[int, Event, str]


def explain(case: Case, *, color: bool = False, width: int | None = None) -> tuple[str, bool]:
    """Return the cause text and whether a trigger was cited.

    The trigger is the earliest class that explains the later lines. Conditions
    such as disk pressure before that failure are contributors. A miss, a
    restart, or a probe failure is a symptom, including when it is the first
    line in the case. ``color`` and ``width`` match the terminal timeline.
    """

    rows = timeline_rows(case, color=color, width=width)
    failures = [(index, event, text) for index, (event, text) in enumerate(rows) if event.failure_class]
    trigger = _trigger(failures)
    if trigger is None:
        notice = "evidence is not enough to state a cause"
        if color:
            notice = paint(notice, SEVERITY_COLOR["warning"])
        return f"case {case.id}\n{notice}", False
    contributors = [item for item in failures if _is_condition(item) and _made_worse(item, trigger)]
    symptoms = [item for item in failures if _is_symptom(item)]
    cited = [trigger, *contributors, *symptoms]
    lines = [f"case {case.id}", _headed("trigger", trigger, color=color), trigger[2]]
    for item in sorted(contributors, key=_time_key):
        lines.append(_headed("contributor", item, color=color))
        lines.append(item[2])
    for item in sorted(symptoms, key=_time_key):
        lines.append(_headed("symptom", item, color=color))
        lines.append(item[2])
    window = _window(cited)
    lines.append(paint(window, WINDOW_COLOR) if color else window)
    return "\n".join(lines), True


def _trigger(failures: list[_Row]) -> _Row | None:
    explaining = [item for item in failures if not _is_symptom(item) and not _is_condition(item)]
    if explaining:
        return min(explaining, key=_time_key)
    conditions = [item for item in failures if _is_condition(item)]
    if conditions:
        return min(conditions, key=_time_key)
    return None


def _is_symptom(item: _Row) -> bool:
    return item[1].failure_class in _SYMPTOMS


def _is_condition(item: _Row) -> bool:
    return item[1].failure_class in _CONDITIONS


def _made_worse(condition: _Row, trigger: _Row) -> bool:
    """A condition counts when it is not after the trigger."""

    if condition[1] is trigger[1]:
        return False
    left = _stamp(condition[1])
    right = _stamp(trigger[1])
    if right is None:
        return left is None
    if left is None:
        return True
    return left <= right


def _headed(role: str, item: _Row, *, color: bool) -> str:
    sentence = _sentence(item[1], color=color)
    if not color:
        return f"{role}: {sentence}"
    return paint(f"{role}: ", CAUSE_COLOR) + sentence


def _window(items: list[_Row]) -> str:
    stamps = [(_stamp(event), event.time) for _index, event, _text in items if event.time]
    known = [(stamp, text) for stamp, text in stamps if stamp is not None]
    if not known:
        return "window: unknown"
    known.sort(key=lambda item: item[0])
    return f"window: {known[0][1]} .. {known[-1][1]}"


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


def _time_key(item: _Row) -> tuple[bool, datetime, int, int]:
    index, event, _text = item
    stamp = _stamp(event)
    return (stamp is None, stamp or datetime.max.replace(tzinfo=timezone.utc), _source_rank(event), index)


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
