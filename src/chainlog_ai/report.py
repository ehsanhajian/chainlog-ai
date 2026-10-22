"""Write a markdown and JSON incident report for a local case."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from chainlog_ai.case import Case, Event, case_file, load_case
from chainlog_ai.patterns import find_patterns
from chainlog_ai.timeline import render_timeline, timeline_rows
from chainlog_ai.why import explain

_ROLES = ("critical", "trigger", "contributor", "symptom")
_SLOT = re.compile(r"slot=(\d+)")
_EPOCH = re.compile(r"epoch=(\d+)")
_BLOCK = re.compile(r"block=(\S+)")


class ReportError(Exception):
    """The report could not be written."""


@dataclass(frozen=True)
class _Cited:
    role: str
    sentence: str
    row: str
    event: Event


def write_report(case_id: str, *, paths: bool = True) -> tuple[Path, Path, bool]:
    """Write ``report.md`` and ``report.json`` next to the case.

    Return the two paths and whether the cause was stated. ``paths`` keeps
    local file paths in the report. The slashing block is first when the
    cause includes one.
    """

    note = _pattern_note(case_id, find_patterns(case_id))
    case = load_case(case_id)
    text, stated = explain(case, since=case.since, until=case.until)
    cited, notice, gaps, window = _read_cause(case, text, stated)
    boundary = _boundary(case)
    payload = _payload(case.id, cited, notice, gaps, window, stated, boundary, note, paths=paths)
    markdown = _markdown(case.id, cited, notice, gaps, window, boundary, note, paths=paths)
    directory = case_file(case.id).parent
    markdown_path = directory / "report.md"
    json_path = directory / "report.json"
    try:
        markdown_path.write_text(markdown, encoding="utf-8")
        json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        raise ReportError(f"cannot write report {directory}") from exc
    return markdown_path, json_path, stated


def _read_cause(
    case: Case,
    text: str,
    stated: bool,
) -> tuple[list[_Cited], str | None, list[str], str | None]:
    lines = text.splitlines()
    if not stated:
        notice = lines[1] if len(lines) > 1 else "evidence is not enough to state a cause"
        gaps: list[str] = []
        for line in lines[2:]:
            if line == "boundary unknown" or line.startswith("last healthy slot "):
                break
            gaps.append(line)
        return [], notice, gaps, None
    cited: list[_Cited] = []
    unused = list(timeline_rows(case, since=case.since, until=case.until))
    window: str | None = None
    index = 1
    while index < len(lines):
        line = lines[index]
        if line.startswith("window: "):
            window = line.removeprefix("window: ")
            index += 1
            continue
        role = next((name for name in _ROLES if line.startswith(f"{name}: ")), None)
        if role is None or index + 1 >= len(lines):
            raise ReportError("report could not read the cause")
        row = lines[index + 1]
        cited.append(_Cited(role, line, row, _take(unused, row)))
        index += 2
    return cited, None, [], window


def _take(unused: list[tuple[Event, str]], row: str) -> Event:
    for index, (event, text) in enumerate(unused):
        if text == row:
            unused.pop(index)
            return event
    raise ReportError("report could not read the cause")


def _boundary(case: Case) -> tuple[int | None, int | None]:
    last: int | None = None
    first: int | None = None
    for line in render_timeline(case).splitlines()[1:3]:
        if line == "boundary unknown":
            return None, None
        if line.startswith("last healthy slot "):
            last = _number(line.removeprefix("last healthy slot "))
        elif line.startswith("first bad slot "):
            first = _number(line.removeprefix("first bad slot "))
    return last, first


def _pattern_note(case_id: str, text: str) -> str:
    lines = text.splitlines()
    if lines and lines[0] == f"case {case_id}":
        lines = lines[1:]
    return "\n".join(lines)


def _payload(
    case_id: str,
    cited: list[_Cited],
    notice: str | None,
    gaps: list[str],
    window: str | None,
    stated: bool,
    boundary: tuple[int | None, int | None],
    note: str,
    *,
    paths: bool,
) -> dict[str, object]:
    grouped = {role: [item for item in cited if item.role == role] for role in _ROLES}
    trigger = grouped["trigger"][0] if grouped["trigger"] else None
    return {
        "case": case_id,
        "critical": [_citation(item, paths=paths) for item in grouped["critical"]],
        "cause": {
            "stated": stated,
            "notice": notice,
            "gaps": gaps,
            "trigger": None if trigger is None else _citation(trigger, paths=paths),
            "contributors": [_citation(item, paths=paths) for item in grouped["contributor"]],
            "symptoms": [_citation(item, paths=paths) for item in grouped["symptom"]],
            "window": window,
        },
        "boundary": {"last_healthy_slot": boundary[0], "first_bad_slot": boundary[1]},
        "citations": [_citation(item, paths=paths) for item in cited],
        "pattern": note,
    }


def _citation(item: _Cited, *, paths: bool) -> dict[str, object]:
    event = item.event
    slot = _match_int(_SLOT, item.row)
    epoch = _match_int(_EPOCH, item.row)
    block = _match(_BLOCK, item.row)
    return {
        "role": item.role,
        "time": event.time,
        "source": event.source,
        "client": event.client,
        "class": event.failure_class,
        "severity": event.severity,
        "excerpt": event.excerpt,
        "path": event.path if paths else None,
        "slot": event.slot if slot is None else slot,
        "epoch": event.epoch if epoch is None else epoch,
        "block": event.block if block is None else block,
    }


def _markdown(
    case_id: str,
    cited: list[_Cited],
    notice: str | None,
    gaps: list[str],
    window: str | None,
    boundary: tuple[int | None, int | None],
    note: str,
    *,
    paths: bool,
) -> str:
    lines = [f"# {case_id}", ""]
    critical = [item for item in cited if item.role == "critical"]
    if critical:
        lines.extend(["## Slashing risk", ""])
        for item in critical:
            lines.extend(_quote(item, paths=paths))
    lines.extend(["## Cause", ""])
    if notice:
        lines.extend([notice, ""])
    for gap in gaps:
        lines.append(gap)
    if gaps:
        lines.append("")
    trigger = next((item for item in cited if item.role == "trigger"), None)
    if trigger is not None:
        lines.extend(_quote(trigger, paths=paths))
    for role, heading in (("contributor", "Contributors"), ("symptom", "Symptoms")):
        group = [item for item in cited if item.role == role]
        if not group:
            continue
        lines.extend([f"### {heading}", ""])
        for item in group:
            lines.extend(_quote(item, paths=paths))
    if window:
        lines.extend([f"window: {window}", ""])
    lines.extend(["## Boundary", ""])
    if boundary == (None, None):
        lines.append("boundary unknown")
    else:
        last = "unknown" if boundary[0] is None else str(boundary[0])
        first = "unknown" if boundary[1] is None else str(boundary[1])
        lines.append(f"last healthy slot {last}")
        lines.append(f"first bad slot {first}")
    lines.extend(["", "## Citations", ""])
    if not cited:
        lines.append("none")
    else:
        for item in cited:
            when = item.event.time or "unknown time"
            lines.append(f"- {item.role} at {when}: {item.sentence.split(': ', 1)[-1]}")
    lines.extend(["", "## Pattern", "", note, ""])
    return "\n".join(lines).rstrip() + "\n"


def _quote(item: _Cited, *, paths: bool) -> list[str]:
    lines = [item.sentence, "", f"> {item.row}", ""]
    if paths:
        lines.extend([f"path: {item.event.path}", ""])
    return lines


def _number(text: str) -> int | None:
    if text == "unknown":
        return None
    return int(text)


def _match(pattern: re.Pattern[str], row: str) -> str | None:
    found = pattern.search(row)
    if found is None:
        return None
    return found.group(1)


def _match_int(pattern: re.Pattern[str], row: str) -> int | None:
    found = _match(pattern, row)
    if found is None:
        return None
    return int(found)
