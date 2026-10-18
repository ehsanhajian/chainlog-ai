"""Label case events from the sourced rule catalog."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from chainlog_ai.builder import BUILDER_CLIENTS
from chainlog_ai.case import Case, Event, load_case, save_case
from chainlog_ai.consensus import CONSENSUS_CLIENTS
from chainlog_ai.execution import EXECUTION_CLIENTS
from chainlog_ai.style import paint_severity
from chainlog_ai.validator import VALIDATOR_CLIENTS


class CatalogError(Exception):
    """The rule catalog cannot be used."""


SEVERITIES = {"info", "warning", "critical"}

_EXECUTION_HEALTHY = re.compile(r"chain head updated|imported new chain segment", re.IGNORECASE)
_CONSENSUS_HEALTHY = re.compile(r"\bSynced\b|synced to head", re.IGNORECASE)
_VALIDATOR_HEALTHY = re.compile(
    r"successfully published attestation|submitted new attestation|\bPublished attestation\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Rule:
    id: str
    pattern: re.Pattern[str]
    failure_class: str
    document: str
    severity: str
    serving: bool
    client: str | None = None
    on: str | None = None

    def matches(self, event: Event) -> bool:
        if self.client is not None and event.client != self.client:
            return False
        if self.on is not None and not _source_matches(self.on, event):
            return False
        return self.pattern.search(event.excerpt) is not None


_POD_SOURCES = {"kubernetes", "kubernetes-previous"}
_CLIENTS = {
    "execution": EXECUTION_CLIENTS,
    "consensus": CONSENSUS_CLIENTS,
    "validator": VALIDATOR_CLIENTS,
    "builder": BUILDER_CLIENTS,
}


def _source_matches(source: str, event: Event) -> bool:
    if event.source == source or (source == "kubernetes" and event.source == "kubernetes-previous"):
        return True
    if source == "execution" and event.client in EXECUTION_CLIENTS:
        return True
    if event.source not in _POD_SOURCES:
        return False
    clients = _CLIENTS.get(source)
    return clients is not None and event.client in clients


def load_catalog(text: str) -> tuple[Rule, ...]:
    """Read rules from catalog JSON. A rule with no source is refused."""

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CatalogError("catalog is not valid json") from exc
    if not isinstance(raw, list):
        raise CatalogError("catalog must be a list")
    rules: list[Rule] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise CatalogError("every rule must be an object")
        rules.append(_rule(item, seen))
    return tuple(rules)


def default_catalog() -> tuple[Rule, ...]:
    raw = files("chainlog_ai").joinpath("catalog.json").read_text(encoding="utf-8")
    return load_catalog(raw)


def catalog_from_path(path: Path) -> tuple[Rule, ...]:
    file_path = path.expanduser()
    if not file_path.is_file():
        raise CatalogError(f"cannot read catalog {path}")
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CatalogError(f"cannot read catalog {path}: {exc.strerror}") from exc
    return load_catalog(text)


def label(case: Case, rules: tuple[Rule, ...] | list[Rule]) -> dict[str, int]:
    """Set each event's class from the first matching rule. Return the counts."""

    counts: dict[str, int] = {}
    unclassified = 0
    for event in case.events:
        matched = next((rule for rule in rules if rule.matches(event)), None)
        if matched is None:
            event.failure_class = None
            event.rule = None
            event.severity = None
            event.serving = None
            unclassified += 1
            continue
        event.failure_class = matched.failure_class
        event.rule = matched.id
        event.severity = matched.severity
        event.serving = matched.serving
        counts[matched.failure_class] = counts.get(matched.failure_class, 0) + 1
    _note_healthy_lines(case.events)
    _resume_serving(case.events)
    counts["unclassified"] = unclassified
    return counts


def _healthy(event: Event) -> bool:
    if event.source == "execution" and _EXECUTION_HEALTHY.search(event.excerpt):
        return True
    if event.source in {"kubernetes", "kubernetes-previous"} and event.client in EXECUTION_CLIENTS:
        return _EXECUTION_HEALTHY.search(event.excerpt) is not None
    if event.source == "consensus" and _CONSENSUS_HEALTHY.search(event.excerpt):
        return True
    if event.source == "validator" and _VALIDATOR_HEALTHY.search(event.excerpt):
        return True
    return False


def _note_healthy_lines(events: list[Event]) -> None:
    for event in events:
        if event.failure_class is None and _healthy(event):
            event.severity = "info"
            event.serving = True


def _resume_serving(events: list[Event]) -> None:
    """A later synced head or published attestation shows that serving resumed."""

    groups: dict[tuple[str, str | None], list[tuple[int, Event]]] = {}
    for index, event in enumerate(events):
        groups.setdefault((event.source, event.client), []).append((index, event))
    for group in groups.values():
        ordered = sorted(group, key=lambda item: (item[1].time or "", item[0]))
        resumed = False
        for _index, event in reversed(ordered):
            if event.serving is True and _healthy(event):
                resumed = True
                continue
            if resumed and event.failure_class is not None:
                event.serving = True


def classify_case(case_id: str, catalog: str | Path | None = None, *, color: bool = False) -> str:
    """Label ``case_id`` from the catalog and return the count text."""

    rules = default_catalog() if catalog is None else catalog_from_path(Path(catalog))
    case = load_case(case_id)
    counts = label(case, rules)
    save_case(case)
    severities = _class_severity(rules) if color else None
    return format_counts(case.id, counts, severities=severities)


def format_counts(
    case_id: str,
    counts: dict[str, int],
    *,
    severities: dict[str, str] | None = None,
) -> str:
    lines = [f"case {case_id}"]
    for name in sorted(key for key in counts if key != "unclassified" and counts[key]):
        label = paint_severity(name, severities.get(name) if severities else None) if severities else name
        lines.append(f"{label} {counts[name]}")
    lines.append(f"unclassified {counts.get('unclassified', 0)}")
    return "\n".join(lines)


def _class_severity(rules: tuple[Rule, ...] | list[Rule]) -> dict[str, str]:
    rank = {"critical": 0, "warning": 1, "info": 2}
    found: dict[str, str] = {}
    for rule in rules:
        current = found.get(rule.failure_class)
        if current is None or rank[rule.severity] < rank[current]:
            found[rule.failure_class] = rule.severity
    return found


def _rule(item: dict[str, object], seen: set[str]) -> Rule:
    rule_id = _text(item.get("id"))
    if rule_id is None:
        raise CatalogError("rule has no id")
    if rule_id in seen:
        raise CatalogError(f"duplicate rule: {rule_id}")
    seen.add(rule_id)
    document = _text(item.get("source"))
    if document is None:
        raise CatalogError(f"rule {rule_id} has no source")
    failure_class = _text(item.get("class"))
    if failure_class is None:
        raise CatalogError(f"rule {rule_id} has no class")
    match = item.get("match")
    if not isinstance(match, str) or not match.strip():
        raise CatalogError(f"rule {rule_id} has no match")
    client = _text(item.get("client"))
    on = _text(item.get("on"))
    if client is None and on is None:
        raise CatalogError(f"rule {rule_id} needs a client or source")
    severity = _text(item.get("severity"))
    if severity not in SEVERITIES:
        raise CatalogError(f"rule {rule_id} has no severity")
    serving = item.get("serving")
    if not isinstance(serving, bool):
        raise CatalogError(f"rule {rule_id} has no serving flag")
    try:
        pattern = re.compile(match, re.IGNORECASE)
    except re.error as exc:
        raise CatalogError(f"rule {rule_id} has an invalid match") from exc
    return Rule(
        id=rule_id,
        pattern=pattern,
        failure_class=failure_class,
        document=document,
        severity=severity,
        serving=serving,
        client=None if client is None else client.lower(),
        on=None if on is None else on.lower(),
    )


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
