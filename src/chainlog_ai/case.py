"""Local case directory."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

_CASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")


class CaseError(Exception):
    """The case directory could not be used."""


class CaseNotFoundError(CaseError):
    """No case with this id is stored."""


class CaseIdError(CaseError):
    """The case id cannot be used as a directory name."""


@dataclass
class Event:
    source: str
    client: str | None
    path: str
    byte_start: int
    byte_end: int
    excerpt: str
    time: str | None = None
    slot: int | None = None
    epoch: int | None = None
    block: str | None = None
    pod: str | None = None
    container: str | None = None
    namespace: str | None = None
    reason: str | None = None
    restart: int | None = None
    generation: int = 0
    failure_class: str | None = None
    rule: str | None = None

    def identity(self) -> tuple[str, str, int, int, int]:
        return (self.source, self.path, self.byte_start, self.byte_end, self.generation)

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "client": self.client,
            "time": self.time,
            "path": self.path,
            "byte_start": self.byte_start,
            "byte_end": self.byte_end,
            "slot": self.slot,
            "epoch": self.epoch,
            "block": self.block,
            "pod": self.pod,
            "container": self.container,
            "namespace": self.namespace,
            "reason": self.reason,
            "restart": self.restart,
            "generation": self.generation,
            "class": self.failure_class,
            "rule": self.rule,
            "excerpt": self.excerpt,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Event:
        client = data["client"]
        time = data.get("time")
        byte_start = data["byte_start"]
        byte_end = data["byte_end"]
        source = data["source"]
        path = data["path"]
        excerpt = data["excerpt"]
        slot = data.get("slot")
        epoch = data.get("epoch")
        block = data.get("block")
        pod = data.get("pod")
        container = data.get("container")
        namespace = data.get("namespace")
        reason = data.get("reason")
        restart = data.get("restart")
        generation = data.get("generation", 0)
        failure_class = data.get("class")
        rule = data.get("rule")
        if not isinstance(source, str) or not isinstance(path, str) or not isinstance(excerpt, str):
            raise TypeError("event fields must be strings")
        if client is not None and not isinstance(client, str):
            raise TypeError("client must be a string or null")
        if time is not None and not isinstance(time, str):
            raise TypeError("time must be a string or null")
        if isinstance(byte_start, bool) or not isinstance(byte_start, int):
            raise TypeError("byte_start must be an integer")
        if isinstance(byte_end, bool) or not isinstance(byte_end, int):
            raise TypeError("byte_end must be an integer")
        if isinstance(slot, bool) or (slot is not None and not isinstance(slot, int)):
            raise TypeError("slot must be an integer or null")
        if isinstance(epoch, bool) or (epoch is not None and not isinstance(epoch, int)):
            raise TypeError("epoch must be an integer or null")
        if block is not None and not isinstance(block, str):
            raise TypeError("block must be a string or null")
        for label, value in (
            ("pod", pod),
            ("container", container),
            ("namespace", namespace),
            ("reason", reason),
        ):
            if value is not None and not isinstance(value, str):
                raise TypeError(f"{label} must be a string or null")
        if isinstance(restart, bool) or (restart is not None and not isinstance(restart, int)):
            raise TypeError("restart must be an integer or null")
        if generation is None:
            generation = 0
        if isinstance(generation, bool) or not isinstance(generation, int):
            raise TypeError("generation must be an integer")
        if failure_class is not None and not isinstance(failure_class, str):
            raise TypeError("class must be a string or null")
        if rule is not None and not isinstance(rule, str):
            raise TypeError("rule must be a string or null")
        return cls(
            source=source,
            client=client,
            path=path,
            time=time,
            byte_start=byte_start,
            byte_end=byte_end,
            excerpt=excerpt,
            slot=slot,
            epoch=epoch,
            block=block,
            pod=pod,
            container=container,
            namespace=namespace,
            reason=reason,
            restart=restart,
            generation=generation,
            failure_class=failure_class,
            rule=rule,
        )


@dataclass
class Rotation:
    """A file was replaced, so a later read starts at the beginning."""

    time: str
    inode: int

    def to_dict(self) -> dict[str, object]:
        return {"time": self.time, "inode": self.inode}

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Rotation:
        stamp = data["time"]
        inode = data["inode"]
        if not isinstance(stamp, str):
            raise TypeError("rotation time must be a string")
        if isinstance(inode, bool) or not isinstance(inode, int):
            raise TypeError("rotation inode must be an integer")
        return cls(time=stamp, inode=inode)


@dataclass
class FileCursor:
    """How far ingest has read one local file."""

    offset: int
    inode: int
    device: int
    generation: int = 0
    rotations: list[Rotation] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "offset": self.offset,
            "inode": self.inode,
            "device": self.device,
            "generation": self.generation,
            "rotations": [item.to_dict() for item in self.rotations],
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> FileCursor:
        offset = data["offset"]
        inode = data["inode"]
        device = data["device"]
        generation = data.get("generation", 0)
        raw_rotations = data.get("rotations", [])
        if isinstance(offset, bool) or not isinstance(offset, int):
            raise TypeError("offset must be an integer")
        if isinstance(inode, bool) or not isinstance(inode, int):
            raise TypeError("inode must be an integer")
        if isinstance(device, bool) or not isinstance(device, int):
            raise TypeError("device must be an integer")
        if isinstance(generation, bool) or not isinstance(generation, int):
            raise TypeError("generation must be an integer")
        if not isinstance(raw_rotations, list):
            raise TypeError("rotations must be a list")
        rotations = [Rotation.from_dict(item) for item in raw_rotations if isinstance(item, dict)]
        if len(rotations) != len(raw_rotations):
            raise TypeError("every rotation must be an object")
        return cls(
            offset=offset,
            inode=inode,
            device=device,
            generation=generation,
            rotations=rotations,
        )


@dataclass
class Case:
    id: str
    events: list[Event]
    files: dict[str, FileCursor] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "events": [event.to_dict() for event in self.events],
            "files": {path: cursor.to_dict() for path, cursor in self.files.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Case:
        raw_events = data["events"]
        if not isinstance(raw_events, list):
            raise TypeError("events must be a list")
        events = [Event.from_dict(item) for item in raw_events if isinstance(item, dict)]
        if len(events) != len(raw_events):
            raise TypeError("every event must be an object")
        raw_files = data.get("files", {})
        if not isinstance(raw_files, dict):
            raise TypeError("files must be an object")
        files: dict[str, FileCursor] = {}
        for path, item in raw_files.items():
            if not isinstance(path, str) or not isinstance(item, dict):
                raise TypeError("files must be an object")
            files[path] = FileCursor.from_dict(item)
        return cls(id=str(data["id"]), events=events, files=files)


def case_home() -> Path:
    """Return the case root. ``CHAINLOG_AI_HOME`` overrides ``~/.chainlog-ai``."""

    override = os.environ.get("CHAINLOG_AI_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".chainlog-ai"


def cases_dir() -> Path:
    return case_home() / "cases"


def validate_case_id(case_id: str) -> str:
    if not _CASE_ID.fullmatch(case_id):
        raise CaseIdError(f"invalid case id: {case_id}")
    return case_id


def case_file(case_id: str) -> Path:
    return cases_dir() / validate_case_id(case_id) / "case.json"


def load_case(case_id: str) -> Case:
    path = case_file(case_id)
    if not path.is_file():
        raise CaseNotFoundError(f"no such case: {case_id}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CaseError(f"cannot read case {case_id}") from exc
    if not isinstance(data, dict):
        raise CaseError(f"cannot read case {case_id}")
    try:
        case = Case.from_dict(data)
    except (KeyError, TypeError, ValueError) as exc:
        raise CaseError(f"cannot read case {case_id}") from exc
    if case.id != case_id:
        raise CaseError(f"cannot read case {case_id}")
    return case


def save_case(case: Case) -> None:
    validate_case_id(case.id)
    directory = cases_dir() / case.id
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "case.json"
    temporary = directory / "case.json.tmp"
    payload = json.dumps(case.to_dict(), indent=2, ensure_ascii=False) + "\n"
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(target)


def allocate_case_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = cases_dir()
    candidate = stamp
    suffix = 2
    while (root / candidate).exists():
        candidate = f"{stamp}-{suffix}"
        suffix += 1
    return candidate


def open_case(case_id: str | None) -> Case:
    if case_id is None:
        return Case(id=allocate_case_id(), events=[])
    validate_case_id(case_id)
    path = cases_dir() / case_id / "case.json"
    if path.is_file():
        return load_case(case_id)
    return Case(id=case_id, events=[])


def append_events(case: Case, events: list[Event]) -> None:
    """Append events. A line already stored at the same path and byte range is kept once."""

    seen = {event.identity() for event in case.events}
    for event in events:
        key = event.identity()
        if key in seen:
            continue
        seen.add(key)
        case.events.append(event)
