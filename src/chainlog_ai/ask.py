"""Answer a question from the case. A remote model stays off unless asked."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from importlib.resources import files

from chainlog_ai.case import Case, Event
from chainlog_ai.redact import redact_excerpt
from chainlog_ai.style import paint_severity, paint_source
from chainlog_ai.timeline import timeline_rows
from chainlog_ai.why import explain

REMOTE_NOTICE = "remote call about to happen: question, cited rows, and runbook text"

_Row = tuple[Event, str]
_SLOT_BEFORE = re.compile(r"slot before the miss", re.IGNORECASE)
_SLOT = re.compile(r"\bslot\s*[:=]?\s*(\d+)\b", re.IGNORECASE)
_SLOT_FIELD = re.compile(r"\bslot=(\d+)\b")
_CAUSE = re.compile(r"\b(?:why|cause)\b|\bwhat (?:happened|failed|went wrong)\b", re.IGNORECASE)
_MISS = re.compile(r"missed attestation|missed block proposal|missed proposal", re.IGNORECASE)
_MISS_CLASS = {"missed-attestation", "missed-proposal"}
_CLUSTER = {"kubernetes", "kubernetes-previous"}
class RemoteError(Exception):
    """The remote call was refused or did not complete."""


_STOP = {
    "what",
    "when",
    "where",
    "which",
    "why",
    "how",
    "did",
    "does",
    "the",
    "this",
    "that",
    "with",
    "from",
    "into",
    "about",
    "before",
    "after",
    "have",
    "happened",
    "fail",
    "failed",
    "failure",
    "case",
    "tell",
    "show",
    "please",
    "there",
    "were",
    "was",
    "are",
    "and",
    "for",
    "slot",
    "just",
    "your",
}


def answer(
    case: Case,
    question: str,
    *,
    color: bool = False,
    width: int | None = None,
) -> tuple[str, bool, dict[str, object] | None]:
    """Answer from the cause and the runbook, citing timeline rows.

    The third value is the remote payload: the question, the cited rows, and
    the runbook text. Nothing is sent from here. An answer that cannot cite a
    row says the case does not contain that and has no payload.
    """

    plain = timeline_rows(case, since=case.since, until=case.until)
    chosen = _select(plain, question)
    if chosen is None:
        plain_text, _cited = explain(case, since=case.since, until=case.until)
        cited_plain = _cited_rows(plain, plain_text)
        if not cited_plain:
            return _missing(case.id), False, None
        display = plain_text
        if color or width is not None:
            display = explain(case, color=color, width=width, since=case.since, until=case.until)[0]
        notes = _runbook_lines(cited_plain)
        return _with_runbook(display, cited_plain), True, _payload(question, cited_plain, notes)
    if not chosen:
        return _missing(case.id), False, None
    shown = _display_rows(case, color=color, width=width)
    lines = [f"case {case.id}"]
    for event, text in chosen:
        lines.append(_lead(event, color=color))
        lines.append(shown.get(id(event), text))
    notes = _runbook_lines(chosen)
    lines.extend(notes)
    return "\n".join(lines), True, _payload(question, chosen, notes)


def case_is_redacted(case: Case) -> bool:
    """Return whether every stored excerpt is unchanged by redaction."""

    return all(redact_excerpt(event.excerpt) == event.excerpt for event in case.events)


def send_remote(url: str, payload: dict[str, object]) -> None:
    """POST the payload as JSON. The body is the question, rows, and runbook."""

    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
    except urllib.error.URLError as exc:
        raise RemoteError(f"remote call failed: {exc.reason}") from exc


def _missing(case_id: str) -> str:
    return f"case {case_id}\nthe case does not contain that"


def _payload(question: str, rows: list[_Row], notes: list[str]) -> dict[str, object]:
    return {
        "question": question,
        "rows": [text for _event, text in rows],
        "runbook": notes,
    }


def _display_rows(case: Case, *, color: bool, width: int | None) -> dict[int, str]:
    if not color and width is None:
        return {}
    return {
        id(event): text
        for event, text in timeline_rows(case, color=color, width=width, since=case.since, until=case.until)
    }


def _select(rows: list[_Row], question: str) -> list[_Row] | None:
    """Return matching rows, or None when the question asks for the cause."""

    if _SLOT_BEFORE.search(question):
        return _slot_before_miss(rows)
    slot = _explicit_slot(question)
    if slot is not None:
        return [row for row in rows if _row_slot(row) == slot]
    words = set(re.findall(r"[a-z0-9]+", question.lower()))
    by_class = [row for row in rows if row[0].failure_class and _class_match(row[0].failure_class, words)]
    if by_class:
        return by_class
    keywords = [word for word in words if len(word) >= 4 and word not in _STOP]
    if keywords:
        return [row for row in rows if _keyword_match(row[0], keywords)]
    if _CAUSE.search(question):
        return None
    return []


def _slot_before_miss(rows: list[_Row]) -> list[_Row]:
    misses = [row for row in rows if _is_miss(row) and _row_slot(row) is not None]
    if not misses:
        return []
    first = min(misses, key=_row_time)
    target = (_row_slot(first) or 0) - 1
    return [row for row in rows if _row_slot(row) == target]


def _explicit_slot(question: str) -> int | None:
    found = _SLOT.search(question)
    if found is None:
        return None
    return int(found.group(1))


def _class_match(failure_class: str, words: set[str]) -> bool:
    tokens = [part for part in failure_class.split("-") if len(part) >= 4]
    return bool(tokens) and all(token in words for token in tokens)


def _keyword_match(event: Event, keywords: list[str]) -> bool:
    haystack = " ".join(
        part
        for part in (event.failure_class, event.source, event.client, event.excerpt)
        if part
    ).lower()
    return any(word in haystack for word in keywords)


def _is_miss(row: _Row) -> bool:
    event, _text = row
    if event.failure_class in _MISS_CLASS:
        return True
    return _MISS.search(event.excerpt) is not None


def _row_slot(row: _Row) -> int | None:
    event, text = row
    found = _SLOT_FIELD.search(text)
    if found is not None:
        return int(found.group(1))
    return event.slot


def _row_time(row: _Row) -> tuple[bool, str]:
    event, _text = row
    return (event.time is None, event.time or "")


def _cited_rows(rows: list[_Row], text: str) -> list[_Row]:
    return [row for row in rows if row[1] and row[1] in text]


def _with_runbook(text: str, rows: list[_Row]) -> str:
    notes = _runbook_lines(rows)
    if not notes:
        return text
    return text + "\n" + "\n".join(notes)


def _runbook_lines(rows: list[_Row]) -> list[str]:
    book = _runbook()
    lines: list[str] = []
    seen: set[str] = set()
    for event, _text in rows:
        name = event.failure_class
        if not name or name in seen:
            continue
        entry = book.get(name)
        if entry is None:
            continue
        sentence, source, checks = entry
        if not sentence or not source:
            continue
        seen.add(name)
        lines.append(f"runbook {name}: {sentence}")
        lines.append(source)
        for check, check_source in checks:
            lines.append(f"check: {check}")
            lines.append(check_source)
    return lines


def _runbook() -> dict[str, tuple[str, str, tuple[tuple[str, str], ...]]]:
    raw = files("chainlog_ai").joinpath("runbook.json").read_text(encoding="utf-8")
    return _parse_runbook(json.loads(raw))


def _parse_runbook(data: object) -> dict[str, tuple[str, str, tuple[tuple[str, str], ...]]]:
    found: dict[str, tuple[str, str, tuple[tuple[str, str], ...]]] = {}
    if not isinstance(data, list):
        return found
    for item in data:
        if not isinstance(item, dict):
            continue
        name = item.get("class")
        sentence = item.get("text")
        source = item.get("source")
        if not isinstance(name, str) or not isinstance(sentence, str) or not isinstance(source, str):
            continue
        if not name or not sentence.strip() or not source.strip():
            continue
        found[name] = (sentence.strip(), source.strip(), _checks(item.get("checks")))
    return found


def _checks(raw: object) -> tuple[tuple[str, str], ...]:
    """Keep read-only checks that name a source. A check without one is left out."""

    if not isinstance(raw, list):
        return ()
    found: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        source = item.get("source")
        if not isinstance(text, str) or not isinstance(source, str):
            continue
        if not text.strip() or not source.strip():
            continue
        found.append((text.strip(), source.strip()))
    return tuple(found)


def _lead(event: Event, *, color: bool) -> str:
    source = "cluster" if event.source in _CLUSTER else event.source
    shown_source = paint_source(source, event.source) if color else source
    if event.failure_class:
        kind = paint_severity(event.failure_class, event.severity) if color else event.failure_class
        if event.time is None:
            return f"{shown_source} failed with {kind}."
        return f"{shown_source} failed with {kind} at {event.time}."
    if event.time is None:
        return f"{shown_source}."
    return f"{shown_source} at {event.time}."
