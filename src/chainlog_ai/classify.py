"""Label case events from the sourced rule catalog."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from chainlog_ai.case import Case, Event, load_case, save_case
from chainlog_ai.execution import EXECUTION_CLIENTS


class CatalogError(Exception):
    """The rule catalog cannot be used."""


@dataclass(frozen=True)
class Rule:
    id: str
    pattern: re.Pattern[str]
    failure_class: str
    document: str
    client: str | None = None
    on: str | None = None

    def matches(self, event: Event) -> bool:
        if self.client is not None and event.client != self.client:
            return False
        if self.on is not None and not _source_matches(self.on, event):
            return False
        return self.pattern.search(event.excerpt) is not None


def _source_matches(source: str, event: Event) -> bool:
    if event.source == source:
        return True
    return source == "execution" and event.client in EXECUTION_CLIENTS


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
            unclassified += 1
            continue
        event.failure_class = matched.failure_class
        event.rule = matched.id
        counts[matched.failure_class] = counts.get(matched.failure_class, 0) + 1
    counts["unclassified"] = unclassified
    return counts


def classify_case(case_id: str, catalog: str | Path | None = None) -> str:
    """Label ``case_id`` from the catalog and return the count text."""

    rules = default_catalog() if catalog is None else catalog_from_path(Path(catalog))
    case = load_case(case_id)
    counts = label(case, rules)
    save_case(case)
    return format_counts(case.id, counts)


def format_counts(case_id: str, counts: dict[str, int]) -> str:
    lines = [f"case {case_id}"]
    for name in sorted(key for key in counts if key != "unclassified" and counts[key]):
        lines.append(f"{name} {counts[name]}")
    lines.append(f"unclassified {counts.get('unclassified', 0)}")
    return "\n".join(lines)


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
    try:
        pattern = re.compile(match, re.IGNORECASE)
    except re.error as exc:
        raise CatalogError(f"rule {rule_id} has an invalid match") from exc
    return Rule(
        id=rule_id,
        pattern=pattern,
        failure_class=failure_class,
        document=document,
        client=None if client is None else client.lower(),
        on=None if on is None else on.lower(),
    )


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
