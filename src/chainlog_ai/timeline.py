"""Print one case in time order across every source."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from chainlog_ai.case import Case, Event
from chainlog_ai.consensus import consensus_fields

# More than a few seconds between a client clock and a cluster event.
# A missing slot is filled from a consensus line inside this same window.
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
_HEALTHY = re.compile(
    r"successfully published attestation|submitted new attestation|\bpublished attestation\b"
    r"|attestation included|successfully published block|\bproposed block\b",
    re.IGNORECASE,
)
_MISS = re.compile(r"missed attestation|missed block proposal|missed proposal", re.IGNORECASE)
_ENGINE_FAILURE = re.compile(
    r"request to execution client timed out|engine api[^\n]*context deadline exceeded"
    r"|newpayload[^\n]*context deadline exceeded|status=invalid|engine_\w+ not supported",
    re.IGNORECASE,
)
_ENGINE_CLASSES = {"engine-timeout", "invalid-payload", "capability-mismatch"}
_PAYLOAD_REQUEST = re.compile(
    r"request to execution client timed out|processing payload|forkchoiceupdated|engine_newpayload",
    re.IGNORECASE,
)
_PAYLOAD_ID = re.compile(
    r"(?:payload(?:[\s_-]*id)?|(?<![A-Za-z])id)\s*[:=]\s*(0x[0-9a-fA-F]+)",
    re.IGNORECASE,
)
_PROCESS_EXIT = re.compile(r"\boomkilled\b|\bkilling\b|\bevicted\b|shutting down|process exited", re.IGNORECASE)
_EXIT_CLASSES = {"oom-killed", "killed-externally", "evicted", "crash-loop"}
_RESET = "\033[0m"
_REDACTED_COLOR = "\033[1;31m"
_GAP_COLOR = "\033[1;33m"
_SEVERITY_COLOR = {"info": "\033[32m", "warning": "\033[33m", "critical": "\033[31m"}
_SOURCE_COLOR = {
    "kubernetes": "\033[34m",
    "kubernetes-previous": "\033[34m",
    "execution": "\033[36m",
    "consensus": "\033[35m",
    "validator": "\033[96m",
    "builder": "\033[94m",
}
_REDACTED = re.compile(re.escape("[redacted]"))


class TimelineError(Exception):
    """The timeline window cannot be used."""


@dataclass
class _Row:
    event: Event
    index: int
    slot: int | None
    epoch: int | None
    block: str | None
    inferred: bool


def render_timeline(
    case: Case,
    *,
    since: str | None = None,
    until: str | None = None,
    color: bool = False,
    width: int | None = None,
) -> str:
    """Return the case in time order. ``since`` and ``until`` are inclusive.

    ``color`` and ``width`` change only a terminal view. Piped output leaves both unset.
    """

    start = _bound(since, "--since")
    end = _bound(until, "--until")
    if start is not None and end is not None and start > end:
        raise TimelineError("--since is after --until")
    aligned = _align(case.events)
    visible = [row for row in aligned if _in_window(row.event, start, end)]
    if start is not None or end is not None:
        visible = [row for row in visible if row.event.time is not None]
    lines = [f"case {case.id}", *_boundary(aligned)]
    ordered = _order_rows(visible)
    pairs = _engine_pairs(ordered)
    previous: Event | None = None
    for row in ordered:
        gap = _clock_gap(previous, row.event)
        if gap is not None:
            lines.append(_paint(gap, _GAP_COLOR) if color else gap)
        if _is_execution_engine_error(row):
            partner = pairs.get(row.index)
            if partner is None:
                lines.append(_unpaired_line(row))
            else:
                lines.append(_engine_pair_line(row, partner))
        lines.append(_row(row, color=color, width=width))
        previous = row.event
    return "\n".join(lines)


def _boundary(rows: list[_Row]) -> list[str]:
    """Name the last attested or proposed slot and the first bad slot."""

    healthy = [row for row in rows if _is_healthy(row)]
    misses = [row for row in rows if _is_miss(row)]
    if not healthy and not misses:
        return ["boundary unknown"]
    bad = [row for row in rows if _is_bad(row)]
    last = _latest_slot(healthy)
    first = _earliest_slot(bad)
    last_text = "unknown" if last is None else str(last)
    first_text = "unknown" if first is None else str(first)
    return [f"last healthy slot {last_text}", f"first bad slot {first_text}"]


def _is_healthy(row: _Row) -> bool:
    return _HEALTHY.search(row.event.excerpt) is not None


def _is_miss(row: _Row) -> bool:
    return _MISS.search(row.event.excerpt) is not None


def _is_bad(row: _Row) -> bool:
    event = row.event
    if _is_miss(row):
        return True
    if event.failure_class in _ENGINE_CLASSES or _ENGINE_FAILURE.search(event.excerpt):
        return True
    return event.failure_class in _EXIT_CLASSES or _PROCESS_EXIT.search(event.excerpt) is not None


def _latest_slot(rows: list[_Row]) -> int | None:
    slots = [row.slot for row in rows if row.slot is not None]
    if not slots:
        return None
    return max(slots)


def _earliest_slot(rows: list[_Row]) -> int | None:
    if not rows:
        return None
    first = min(rows, key=_bad_order)
    return first.slot


def _engine_pairs(rows: list[_Row]) -> dict[int, _Row]:
    """Map each execution engine error to the consensus line that requested the payload."""

    requests = [row for row in rows if _is_payload_request(row)]
    paired: dict[int, _Row] = {}
    for row in rows:
        if not _is_execution_engine_error(row):
            continue
        partner = _match_request(row, requests)
        if partner is not None:
            paired[row.index] = partner
    return paired


def _is_execution_engine_error(row: _Row) -> bool:
    event = row.event
    if event.source != "execution":
        return False
    if event.failure_class in _ENGINE_CLASSES:
        return True
    return _ENGINE_FAILURE.search(event.excerpt) is not None


def _is_payload_request(row: _Row) -> bool:
    event = row.event
    if event.source != "consensus":
        return False
    if event.failure_class == "engine-timeout":
        return True
    return _PAYLOAD_REQUEST.search(event.excerpt) is not None


def _match_request(row: _Row, requests: list[_Row]) -> _Row | None:
    candidates: list[_Row] = []
    for request in requests:
        if row.slot is not None and request.slot is not None:
            if row.slot == request.slot:
                candidates.append(request)
            continue
        if _within_gap(row, request):
            candidates.append(request)
    if not candidates:
        return None
    return min(candidates, key=lambda request: _pair_distance(row, request))


def _within_gap(row: _Row, other: _Row) -> bool:
    left = _event_time(row.event)
    right = _event_time(other.event)
    if left is None or right is None:
        return False
    return abs(left - right) <= CLOCK_GAP


def _pair_distance(row: _Row, request: _Row) -> tuple[timedelta, int]:
    left = _event_time(row.event)
    right = _event_time(request.event)
    if left is None or right is None:
        return (timedelta.max, request.index)
    return (abs(left - right), request.index)


def _engine_pair_line(row: _Row, request: _Row) -> str:
    parts = ["engine pair"]
    slot = row.slot if row.slot is not None else request.slot
    if slot is not None:
        parts.append(f"slot={slot}")
    payload = _payload_id(row) or _payload_id(request)
    if payload is not None:
        parts.append(f"payload={payload}")
    block = _pair_block(row, request, payload)
    if block is not None:
        parts.append(f"block={block}")
    return " ".join(parts)


def _unpaired_line(row: _Row) -> str:
    if row.slot is None:
        return "engine error unpaired"
    return f"engine error unpaired slot={row.slot}"


def _payload_id(row: _Row) -> str | None:
    found = _PAYLOAD_ID.search(row.event.excerpt)
    if found is None:
        return None
    return found.group(1)


def _pair_block(row: _Row, request: _Row, payload: str | None) -> str | None:
    for candidate in (row.block, request.block):
        if candidate is not None and candidate != payload:
            return candidate
    return None


def _bad_order(row: _Row) -> tuple[bool, datetime, int, int]:
    stamp = _event_time(row.event)
    return (stamp is None, stamp or datetime.max.replace(tzinfo=timezone.utc), _source_rank(row.event), row.index)


def _align(events: list[Event]) -> list[_Row]:
    rows: list[_Row] = []
    for index, event in enumerate(events):
        slot, epoch, block = consensus_fields(event.excerpt)
        if slot is None:
            slot = event.slot
        if epoch is None:
            epoch = event.epoch
        if block is None:
            block = event.block
        rows.append(_Row(event, index, slot, epoch, block, False))
    for row in rows:
        if row.slot is not None:
            continue
        nearby = _nearby_consensus_slots(row, rows)
        if len(nearby) == 1:
            row.slot = next(iter(nearby))
            row.inferred = True
    return rows


def _nearby_consensus_slots(row: _Row, rows: list[_Row]) -> set[int]:
    stamp = _event_time(row.event)
    if stamp is None:
        return set()
    found: set[int] = set()
    for other in rows:
        if other.event.source != "consensus" or other.slot is None or other.inferred:
            continue
        other_stamp = _event_time(other.event)
        if other_stamp is None:
            continue
        if abs(other_stamp - stamp) <= CLOCK_GAP:
            found.add(other.slot)
    return found


def _order_rows(rows: list[_Row]) -> list[_Row]:
    anchors: dict[int, datetime] = {}
    for row in rows:
        if row.slot is None:
            continue
        stamp = _event_time(row.event)
        if stamp is None:
            continue
        current = anchors.get(row.slot)
        anchors[row.slot] = stamp if current is None else min(current, stamp)
    return sorted(rows, key=lambda row: _row_key(row, anchors))


def _row_key(row: _Row, anchors: dict[int, datetime]) -> tuple[datetime, int, int, datetime, int, int]:
    stamp = _event_time(row.event) or datetime.max.replace(tzinfo=timezone.utc)
    if row.slot is None:
        return (stamp, 1, 0, stamp, _source_rank(row.event), row.index)
    anchor = anchors.get(row.slot, stamp)
    return (anchor, 0, row.slot, stamp, _source_rank(row.event), row.index)


def _source_rank(event: Event) -> int:
    return _SOURCE_ORDER.get(event.source, len(_SOURCE_ORDER))


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


def _row(row: _Row, *, color: bool = False, width: int | None = None) -> str:
    event = row.event
    when = event.time or "-"
    client = event.client or "-"
    kind = event.failure_class or "unclassified"
    fields = _chain_fields(row)
    head = " ".join(part for part in (when, event.source, client, kind, fields) if part)
    excerpt = event.excerpt
    plain = f"{head} {excerpt}" if excerpt else head
    narrow = width is not None and bool(excerpt) and len(plain) > width
    if color:
        source = _paint(event.source, _SOURCE_COLOR.get(event.source, "\033[36m"))
        shown = _paint(kind, _SEVERITY_COLOR[event.severity]) if event.severity in _SEVERITY_COLOR else kind
        head = " ".join(part for part in (when, source, client, shown, fields) if part)
        excerpt = _REDACTED.sub(lambda match: _paint(match.group(0), _REDACTED_COLOR), excerpt)
    if narrow:
        return f"{head}\n  {excerpt}"
    if excerpt:
        return f"{head} {excerpt}"
    return head


def _paint(text: str, color: str) -> str:
    return f"{color}{text}{_RESET}"


def _chain_fields(row: _Row) -> str:
    parts: list[str] = []
    if row.slot is not None:
        slot = f"slot={row.slot}"
        if row.inferred:
            slot += " inferred"
        parts.append(slot)
    if row.epoch is not None:
        parts.append(f"epoch={row.epoch}")
    if row.block is not None:
        parts.append(f"block={row.block}")
    return " ".join(parts)


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
