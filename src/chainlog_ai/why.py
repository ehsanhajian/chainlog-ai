"""State one cause from the catalog labels and the timeline."""

from __future__ import annotations

from datetime import datetime, timezone

from chainlog_ai.case import Case, Event
from chainlog_ai.metrics import metric_phrase
from chainlog_ai.style import CAUSE_COLOR, SEVERITY_COLOR, WINDOW_COLOR, paint, paint_redacted, paint_severity, paint_source
from chainlog_ai.timeline import covered_slots, first_bad_slot, render_timeline, timeline_rows

_SOURCE_ORDER = {
    "kubernetes": 0,
    "kubernetes-previous": 1,
    "execution": 2,
    "consensus": 3,
    "validator": 4,
    "builder": 5,
    "alert": 6,
    "metric": 7,
}
_CLUSTER = {"kubernetes", "kubernetes-previous"}
_NAMED = {"execution", "consensus", "validator", "builder"}
# A miss, a restart, or a probe failure is a symptom of an earlier failure.
_SYMPTOMS = {"missed-attestation", "missed-proposal", "crash-loop", "probe-failed"}
# Pressure and a drifting clock make a failure worse. They are the trigger only
# when the case has no class that explains the failure.
_CONDITIONS = {"cpu-pressure", "memory-pressure", "disk-pressure", "clock-not-synced"}
_SLASHING = {"doppelganger", "slashable"}
# These classes are one failure seen from two logs. A cause needs both sources.
_USUAL = {
    "engine-timeout": frozenset({"execution", "consensus"}),
    "invalid-payload": frozenset({"execution", "consensus"}),
    "capability-mismatch": frozenset({"execution", "consensus"}),
    "relay-timeout": frozenset({"builder", "consensus"}),
    "builder-timeout": frozenset({"builder", "consensus"}),
}
_SOURCE_NAMES = ("execution", "consensus", "validator", "builder", "cluster")
_Row = tuple[int, Event, str]


def explain(
    case: Case,
    *,
    color: bool = False,
    width: int | None = None,
    since: str | None = None,
    until: str | None = None,
) -> tuple[str, bool]:
    """Return the cause text and whether a trigger was cited.

    The trigger is the earliest class that explains the later lines. A
    doppelganger or a slashable duty is named critical before that trigger.
    Conditions such as disk pressure before that failure are contributors. A
    miss, a restart, or a probe failure is a symptom, including when it is the
    first line in the case. A thin case names the missing source or the slot
    the window does not cover, prints the timeline, and does not state a cause.
    ``color`` and ``width`` match the terminal timeline.
    """

    rows = timeline_rows(case, color=color, width=width, since=since, until=until)
    failures = [(index, event, text) for index, (event, text) in enumerate(rows) if event.failure_class]
    slashing = sorted((item for item in failures if _is_slashing(item)), key=_time_key)
    trigger = _trigger(failures)
    gaps = _gaps(case, failures, since=since, until=until)
    if gaps or (trigger is None and not slashing):
        return _not_enough(case, gaps, color=color, width=width, since=since, until=until), False
    contributors = [item for item in failures if trigger is not None and _is_condition(item) and _made_worse(item, trigger)]
    if trigger is not None:
        contributors.extend(
            item
            for item in failures
            if item[1].source == "metric" and _metric_in_span(item, rows, since=since, until=until)
        )
    symptoms = [item for item in failures if _is_symptom(item)]
    cited = [*slashing, *([trigger] if trigger is not None else []), *contributors, *symptoms]
    lines = [f"case {case.id}"]
    for item in slashing:
        lines.append(_headed("critical", item, color=color))
        lines.append(item[2])
    if trigger is not None:
        lines.append(_headed("trigger", trigger, color=color))
        lines.append(trigger[2])
    for item in sorted(contributors, key=_time_key):
        lines.append(_headed("contributor", item, color=color))
        lines.append(item[2])
    for item in sorted(symptoms, key=_time_key):
        lines.append(_headed("symptom", item, color=color))
        lines.append(item[2])
    window = _window(cited)
    lines.append(paint(window, WINDOW_COLOR) if color else window)
    return "\n".join(lines), True


def _not_enough(
    case: Case,
    gaps: list[str],
    *,
    color: bool,
    width: int | None,
    since: str | None,
    until: str | None,
) -> str:
    notice = "evidence is not enough to state a cause"
    if color:
        notice = paint(notice, SEVERITY_COLOR["warning"])
        gaps = [paint(gap, SEVERITY_COLOR["warning"]) for gap in gaps]
    timeline = render_timeline(case, since=since, until=until, color=color, width=width)
    body = timeline.split("\n", 1)[1] if "\n" in timeline else ""
    lines = [f"case {case.id}", notice, *gaps]
    if body:
        lines.append(body)
    return "\n".join(lines)


def _gaps(case: Case, failures: list[_Row], *, since: str | None, until: str | None) -> list[str]:
    """Name a missing source or a slot the window does not cover."""

    gaps: list[str] = []
    present = _present_sources(case)
    needed: set[str] = set()
    for _index, event, _text in failures:
        needed.update(_USUAL.get(event.failure_class or "", ()))
    for name in _SOURCE_NAMES:
        if name in needed and name not in present:
            gaps.append(f"missing {name}")
    slot = first_bad_slot(case)
    if slot is not None and slot not in covered_slots(case, since=since, until=until):
        gaps.append(f"window does not cover slot {slot}")
    if not failures and not gaps:
        gaps.append("missing a classified failure")
    elif failures and _trigger(failures) is None and not any(_is_slashing(item) for item in failures) and not gaps:
        gaps.append("missing a class that explains the failure")
    return gaps


def _present_sources(case: Case) -> set[str]:
    present: set[str] = set()
    for event in case.events:
        if event.source in _CLUSTER:
            present.add("cluster")
        elif event.source in _NAMED:
            present.add(event.source)
    return present


def _trigger(failures: list[_Row]) -> _Row | None:
    explaining = [
        item
        for item in failures
        if item[1].source != "metric"
        and not _is_symptom(item)
        and not _is_condition(item)
        and not _is_slashing(item)
    ]
    if explaining:
        return min(explaining, key=_time_key)
    conditions = [item for item in failures if _is_condition(item)]
    if conditions:
        return min(conditions, key=_time_key)
    return None


def _is_slashing(item: _Row) -> bool:
    return item[1].failure_class in _SLASHING


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


def _metric_in_span(item: _Row, rows: list[tuple[Event, str]], *, since: str | None, until: str | None) -> bool:
    """A metric counts when its sample sits in the window that holds the trigger."""

    if since is not None or until is not None:
        return True
    stamps = [
        stamp
        for event, _text in rows
        if event.source != "metric" and (stamp := _stamp(event)) is not None
    ]
    if not stamps:
        return True
    stamp = _stamp(item[1])
    if stamp is None:
        return False
    return min(stamps) <= stamp <= max(stamps)


def _headed(role: str, item: _Row, *, color: bool) -> str:
    sentence = _metric_sentence(item[1], color=color) if item[1].source == "metric" else _sentence(item[1], color=color)
    if not color:
        return f"{role}: {sentence}"
    label = SEVERITY_COLOR["critical"] if role == "critical" else CAUSE_COLOR
    return paint(f"{role}: ", label) + sentence


def _window(items: list[_Row]) -> str:
    stamps = [(_stamp(event), event.time) for _index, event, _text in items if event.time]
    known = [(stamp, text) for stamp, text in stamps if stamp is not None]
    if not known:
        return "window: unknown"
    known.sort(key=lambda item: item[0])
    return f"window: {known[0][1]} .. {known[-1][1]}"


def _metric_sentence(event: Event, *, color: bool) -> str:
    phrase = metric_phrase(event.failure_class or "")
    when = f" at {event.time}" if event.time else ""
    source = paint_source("metric", "metric") if color else "metric"
    return f"{source} {phrase}{when}."


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
