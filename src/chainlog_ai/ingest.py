"""Read operator log files into a local case."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from chainlog_ai.builder import detect_builder_client, normalize_builder_client
from chainlog_ai.case import (
    Case,
    Event,
    FileCursor,
    Rotation,
    append_events,
    open_case,
    save_case,
    validate_case_id,
)
from chainlog_ai.consensus import (
    consensus_fields,
    detect_consensus_client,
    normalize_consensus_client,
    parse_consensus_time,
)
from chainlog_ai.execution import (
    detect_execution_client,
    normalize_execution_client,
    parse_execution_time,
)
from chainlog_ai.kubernetes import (
    PREVIOUS_SOURCE,
    application_client,
    container_time,
    cri_message,
    event_place,
    event_time,
    pod_log_path,
)
from chainlog_ai.redact import redact_excerpt
from chainlog_ai.validator import detect_validator_client, normalize_validator_client

SOURCES = ("execution", "consensus", "validator", "builder", "kubernetes")


class InputError(Exception):
    """An input file could not be read."""


def ingest(
    inputs: list[tuple[str, ...]],
    case_id: str | None = None,
    *,
    follow: bool = False,
    should_stop: Callable[[], bool] | None = None,
    pause: float = 0.2,
    on_ready: Callable[[str], None] | None = None,
    on_events: Callable[[list[Event]], None] | None = None,
    alert: str | None = None,
    metrics: str | None = None,
) -> str:
    """Read ``inputs`` and write them to a case. Return the case id.

    ``follow`` keeps reading the local files until the operator stops the
    command. A later ingest of the same case continues from the stored offset.
    """

    if case_id is not None:
        validate_case_id(case_id)
    if follow and not inputs:
        follow = False
    case = open_case(case_id)
    if alert:
        from chainlog_ai.alert import read_alert

        read_alert(case, Path(alert))
    if metrics:
        from chainlog_ai.metrics import read_metrics

        read_metrics(case, Path(metrics))
    if on_ready is not None:
        on_ready(case.id)
    try:
        while True:
            added, changed = _read_inputs(case, inputs, keep_partial=not follow)
            if follow and added and on_events is not None:
                on_events(added)
            if added or changed:
                save_case(case)
            if not follow:
                break
            if should_stop is not None and should_stop():
                break
            time.sleep(pause)
    except KeyboardInterrupt:
        pass
    save_case(case)
    return case.id


def _read_inputs(
    case: Case,
    inputs: list[tuple[str, ...]],
    *,
    keep_partial: bool,
) -> tuple[list[Event], bool]:
    changed = False
    events: list[Event] = []
    for item in inputs:
        source, raw_path, client, meta = _input(item)
        if source not in SOURCES:
            raise InputError(f"unknown source: {source}")
        found, advanced = _read_followed(case, Path(raw_path), source, client, meta, keep_partial)
        events.extend(found)
        changed = changed or advanced or bool(found)
    if events:
        _mark_previous_containers(events)
        before = len(case.events)
        append_events(case, events)
        added = case.events[before:]
    else:
        added = []
    return added, changed or bool(added)


def _read_followed(
    case: Case,
    path: Path,
    source: str,
    client: str | None,
    meta: dict[str, object] | None,
    keep_partial: bool,
) -> tuple[list[Event], bool]:
    file_path = path.expanduser()
    if not file_path.is_file():
        if keep_partial:
            raise InputError(f"cannot read {path}: not a file")
        return [], False
    resolved = file_path.resolve()
    try:
        handle = resolved.open("rb")
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc.strerror}") from exc
    with handle:
        state = os.fstat(handle.fileno())
        cursor, rotated = _cursor(case, str(resolved), state)
        handle.seek(cursor.offset)
        chunk = handle.read()
    known = _known_client(case, str(resolved))
    events, consumed = read_file(
        resolved,
        source,
        client=client,
        meta=meta,
        data=chunk,
        base=cursor.offset,
        keep_partial=keep_partial,
        known_client=known,
        full_file=cursor.offset == 0,
    )
    for event in events:
        event.generation = cursor.generation
    cursor.offset += consumed
    return events, rotated or consumed > 0


def _cursor(case: Case, key: str, state: os.stat_result) -> tuple[FileCursor, bool]:
    cursor = case.files.get(key)
    if cursor is None:
        cursor = FileCursor(offset=0, inode=state.st_ino, device=state.st_dev)
        case.files[key] = cursor
        return cursor, True
    replaced = cursor.inode != state.st_ino or cursor.device != state.st_dev
    truncated = state.st_size < cursor.offset
    if not replaced and not truncated:
        return cursor, False
    cursor.rotations.append(Rotation(time=_utc_now(), inode=cursor.inode))
    cursor.inode = state.st_ino
    cursor.device = state.st_dev
    cursor.generation += 1
    cursor.offset = 0
    return cursor, True


def _known_client(case: Case, path: str) -> str | None:
    for event in reversed(case.events):
        if event.path == path and event.client:
            return event.client
    return None


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_file(
    path: Path,
    source: str,
    client: str | None = None,
    meta: dict[str, object] | None = None,
    *,
    data: bytes | None = None,
    base: int = 0,
    keep_partial: bool = True,
    known_client: str | None = None,
    full_file: bool = True,
) -> tuple[list[Event], int]:
    file_path = path.expanduser()
    if data is None:
        if not file_path.is_file():
            raise InputError(f"cannot read {path}: not a file")
        try:
            data = file_path.read_bytes()
        except OSError as exc:
            raise InputError(f"cannot read {path}: {exc.strerror}") from exc
        base = 0
        full_file = True
    stored_path = str(file_path.resolve())
    if source == "kubernetes" and full_file and base == 0:
        cluster = _cluster_events(file_path, data)
        if cluster is not None:
            return cluster, len(data)
    if keep_partial and base == 0 and source != "kubernetes":
        records = _records(data)
        consumed = len(data)
    else:
        records, consumed = _complete_lines(data, base, keep_partial=keep_partial)
    if source == "kubernetes":
        place = _kube_meta(file_path, meta)
        return _container_log(records, stored_path, place, known_client), consumed
    named = _named_client(source, client, records) or known_client
    now = datetime.now(timezone.utc)
    events = []
    for start, end, excerpt in records:
        when, slot, epoch, block = _line_fields(source, excerpt, now)
        events.append(
            Event(
                source=source,
                client=named,
                path=stored_path,
                byte_start=start,
                byte_end=end,
                excerpt=redact_excerpt(excerpt),
                time=when,
                slot=slot,
                epoch=epoch,
                block=block,
            )
        )
    return events, consumed


def _input(item: tuple[str, ...]) -> tuple[str, str, str | None, dict[str, object] | None]:
    source, raw_path = item[0], item[1]
    client = item[2] if len(item) > 2 else None
    meta = item[3] if len(item) > 3 else None
    if not isinstance(client, str):
        client = None
    if not isinstance(meta, dict):
        meta = None
    return source, raw_path, client, meta


def _named_client(
    source: str,
    client: str | None,
    records: list[tuple[int, int, str]],
) -> str | None:
    text = "\n".join(excerpt for _, _, excerpt in records)
    if source == "execution":
        if client is not None:
            return normalize_execution_client(client)
        return detect_execution_client(text)
    if source == "consensus":
        if client is not None:
            return normalize_consensus_client(client)
        return detect_consensus_client(text)
    if source == "validator":
        if client is not None:
            return normalize_validator_client(client)
        return detect_validator_client(text)
    if source == "builder":
        if client is not None:
            return normalize_builder_client(client)
        return detect_builder_client(text)
    return None


def _line_fields(
    source: str,
    excerpt: str,
    now: datetime,
) -> tuple[str | None, int | None, int | None, str | None]:
    if source == "execution":
        return parse_execution_time(excerpt, now=now), None, None, None
    if source == "consensus":
        slot, epoch, block = consensus_fields(excerpt)
        return parse_consensus_time(excerpt, now=now), slot, epoch, block
    if source in {"validator", "builder"}:
        return parse_consensus_time(excerpt, now=now), None, None, None
    return None, None, None, None


def _cluster_events(path: Path, data: bytes) -> list[Event] | None:
    text = _decode_utf8(data)
    if text is None:
        return None
    documents = kubernetes_events(text)
    if documents is None:
        return None
    stored_path = str(path.resolve())
    now = datetime.now(timezone.utc)
    return [
        _cluster_event(path_text=text, event=event, start=start, end=end, stored_path=stored_path, now=now)
        for event, start, end in documents
    ]


def _kube_meta(path: Path, meta: dict[str, object] | None) -> dict[str, object]:
    found = pod_log_path(path) or {}
    if meta:
        found.update({key: value for key, value in meta.items() if value is not None})
    restart = found.get("restart")
    if restart is not None and (isinstance(restart, bool) or not isinstance(restart, int) or restart < 0):
        raise InputError(f"invalid restart count: {restart}")
    return found


def _cluster_event(
    *,
    path_text: str,
    event: dict[str, object],
    start: int,
    end: int,
    stored_path: str,
    now: datetime,
) -> Event:
    byte_start, byte_end = _byte_span(path_text, start, end)
    pod, container, namespace, reason = event_place(event)
    return Event(
        source="kubernetes",
        client=None,
        path=stored_path,
        byte_start=byte_start,
        byte_end=byte_end,
        excerpt=redact_excerpt(_event_excerpt(event)),
        time=event_time(event, now=now),
        pod=pod,
        container=container,
        namespace=namespace,
        reason=reason,
    )


def _container_log(
    records: list[tuple[int, int, str]],
    stored_path: str,
    place: dict[str, object],
    known_client: str | None,
) -> list[Event]:
    now = datetime.now(timezone.utc)
    messages = [_application_line(excerpt)[0] for _, _, excerpt in records]
    named = application_client("\n".join(messages)) or known_client
    source = PREVIOUS_SOURCE if place.get("previous") is True else "kubernetes"
    restart = place.get("restart")
    counted = restart if isinstance(restart, int) else None
    events: list[Event] = []
    for start, end, excerpt in records:
        message, offset = _application_line(excerpt)
        if not message.strip():
            continue
        prefix = len(excerpt[:offset].encode("utf-8"))
        events.append(
            Event(
                source=source,
                client=named,
                path=stored_path,
                byte_start=start + prefix,
                byte_end=start + prefix + len(message.encode("utf-8")),
                excerpt=redact_excerpt(message),
                time=container_time(excerpt, message, now=now),
                pod=_text_field(place.get("pod")),
                container=_text_field(place.get("container")),
                namespace=_text_field(place.get("namespace")),
                restart=counted,
            )
        )
    return events


def _application_line(excerpt: str) -> tuple[str, int]:
    found = cri_message(excerpt)
    if found is None:
        return excerpt, 0
    return found


def _text_field(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _mark_previous_containers(events: list[Event]) -> None:
    """A lower restart of the same container is a previous source."""

    groups: dict[tuple[str | None, str | None, str | None], list[Event]] = {}
    for event in events:
        if event.restart is None or not event.pod:
            continue
        if event.source not in {"kubernetes", PREVIOUS_SOURCE}:
            continue
        if event.reason is not None:
            continue
        key = (event.namespace, event.pod, event.container)
        groups.setdefault(key, []).append(event)
    for group in groups.values():
        counts = {event.restart for event in group}
        if len(counts) < 2:
            continue
        current = max(count for count in counts if count is not None)
        for event in group:
            if event.restart is not None and event.restart < current:
                event.source = PREVIOUS_SOURCE


def _records(data: bytes) -> list[tuple[int, int, str]]:
    text = _decode_utf8(data)
    if text is not None:
        events = kubernetes_events(text)
        if events is not None:
            return [
                (*_byte_span(text, start, end), _event_excerpt(event))
                for event, start, end in events
            ]
    return _line_records(data)


def _decode_utf8(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _complete_lines(
    data: bytes,
    base: int,
    *,
    keep_partial: bool,
) -> tuple[list[tuple[int, int, str]], int]:
    if not data:
        return [], 0
    view = data
    consumed = len(data)
    if not keep_partial and not data.endswith((b"\n", b"\r")):
        last_nl = max(data.rfind(b"\n"), data.rfind(b"\r"))
        if last_nl < 0:
            return [], 0
        view = data[: last_nl + 1]
        consumed = last_nl + 1
    return _line_records(view, base), consumed


def _line_records(data: bytes, base: int = 0) -> list[tuple[int, int, str]]:
    records: list[tuple[int, int, str]] = []
    start = 0
    for raw in data.splitlines(keepends=True):
        content = raw.rstrip(b"\r\n")
        end = start + len(content)
        if content.strip():
            records.append((base + start, base + end, content.decode("utf-8", errors="replace")))
        start += len(raw)
    return records


def _byte_span(text: str, start: int, end: int) -> tuple[int, int]:
    prefix = len(text[:start].encode("utf-8"))
    size = len(text[start:end].encode("utf-8"))
    return prefix, prefix + size


def _event_excerpt(event: dict[str, object]) -> str:
    reason = event.get("reason")
    message = event.get("message")
    if message is None:
        message = event.get("note")
    reason_text = reason if isinstance(reason, str) else ""
    message_text = message if isinstance(message, str) else ""
    if reason_text and message_text:
        return f"{reason_text}: {message_text}"
    if message_text or reason_text:
        return message_text or reason_text
    return json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def kubernetes_events(text: str) -> list[tuple[dict[str, object], int, int]] | None:
    """Return Kubernetes events and the character span of each object.

    ``None`` means this text is not a Kubernetes events document.
    An empty list means the document was an events list with no events.
    """

    index = _skip_ws(text, 0)
    if index >= len(text) or text[index] not in "{[":
        return None
    decoder = json.JSONDecoder()
    try:
        if text[index] == "[":
            items, end = _parse_array(text, index, decoder)
            if _skip_ws(text, end) != len(text):
                return None
            if items and all(_is_event(value) for value, _, _ in items):
                return [(value, start, stop) for value, start, stop in items]
            return None
        fields, item_spans, start, end = _parse_object(text, index, decoder)
        if _skip_ws(text, end) != len(text):
            return None
    except (ValueError, json.JSONDecodeError, IndexError):
        return None
    if fields.get("kind") == "EventList":
        if item_spans is None:
            return None
        return [(value, start, stop) for value, start, stop in item_spans if isinstance(value, dict)]
    if item_spans and all(_is_event(value) for value, _, _ in item_spans):
        return [(value, start, stop) for value, start, stop in item_spans]
    if _is_event(fields):
        return [(fields, start, end)]
    return None


def _is_event(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("kind") == "Event":
        return True
    message = value.get("message", value.get("note"))
    reason = value.get("reason")
    involved = value.get("involvedObject", value.get("regarding"))
    return involved is not None and (isinstance(message, str) or isinstance(reason, str))


def _skip_ws(text: str, index: int) -> int:
    while index < len(text) and text[index].isspace():
        index += 1
    return index


def _parse_array(
    text: str, index: int, decoder: json.JSONDecoder
) -> tuple[list[tuple[dict[str, object], int, int]], int]:
    if text[index] != "[":
        raise ValueError("expected array")
    index += 1
    items: list[tuple[dict[str, object], int, int]] = []
    while True:
        index = _skip_ws(text, index)
        if index < len(text) and text[index] == "]":
            return items, index + 1
        start = index
        value, index = decoder.raw_decode(text, index)
        if isinstance(value, dict):
            items.append((value, start, index))
        else:
            raise ValueError("event item must be an object")
        index = _skip_ws(text, index)
        if index < len(text) and text[index] == ",":
            index += 1
            continue
        if index < len(text) and text[index] == "]":
            return items, index + 1
        raise ValueError("expected comma or end of array")


def _parse_object(
    text: str, index: int, decoder: json.JSONDecoder
) -> tuple[dict[str, object], list[tuple[dict[str, object], int, int]] | None, int, int]:
    if text[index] != "{":
        raise ValueError("expected object")
    start = index
    index += 1
    fields: dict[str, object] = {}
    item_spans: list[tuple[dict[str, object], int, int]] | None = None
    while True:
        index = _skip_ws(text, index)
        if index < len(text) and text[index] == "}":
            return fields, item_spans, start, index + 1
        key, index = decoder.raw_decode(text, index)
        if not isinstance(key, str):
            raise ValueError("object key must be a string")
        index = _skip_ws(text, index)
        if index >= len(text) or text[index] != ":":
            raise ValueError("expected colon")
        index = _skip_ws(text, index + 1)
        if key == "items" and index < len(text) and text[index] == "[":
            item_spans, index = _parse_array(text, index, decoder)
            fields["items"] = [value for value, _, _ in item_spans]
        else:
            fields[key], index = decoder.raw_decode(text, index)
        index = _skip_ws(text, index)
        if index < len(text) and text[index] == ",":
            index += 1
            continue
        if index < len(text) and text[index] == "}":
            return fields, item_spans, start, index + 1
        raise ValueError("expected comma or end of object")
