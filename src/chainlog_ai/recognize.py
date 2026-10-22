"""Tell an execution, consensus, validator, builder, cluster, alert, or metrics file apart."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from chainlog_ai.builder import detect_builder_client
from chainlog_ai.consensus import CONSENSUS_CLIENTS, detect_consensus_client
from chainlog_ai.execution import detect_execution_client
from chainlog_ai.ingest import kubernetes_events
from chainlog_ai.kubernetes import pod_log_path
from chainlog_ai.validator import detect_validator_client

_VALIDATOR_WORD = re.compile(r"\bvalidator\b", re.IGNORECASE)
_METRIC_TYPES = {"matrix", "vector", "scalar", "string"}


class RecognizeError(Exception):
    """A path could not be classified as one source."""

    def __init__(self, message: str, *, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class Recognized:
    source: str
    path: Path
    client: str | None


def recognize(path: Path) -> Recognized:
    """Return the one source for ``path``.

    A kubelet log path is Kubernetes. A banner selects the client. A consensus
    client named on a line that says validator, and Web3Signer, are the
    validator source. Alert JSON and Prometheus JSON keep their existing
    shapes. A file that matches nothing, or more than one source, is named.
    """

    file_path = path.expanduser()
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RecognizeError(f"cannot read {path}: {exc.strerror}", kind="read") from exc
    except UnicodeError as exc:
        raise RecognizeError(f"cannot read {path}", kind="read") from exc
    if pod_log_path(file_path) is not None:
        return Recognized("kubernetes", file_path, None)
    sources = _json_sources(text)
    if not sources:
        sources = _banner_sources(text)
    if not sources:
        raise RecognizeError(f"cannot tell what {path} is", kind="unknown")
    if len(sources) > 1:
        raise RecognizeError(f"{path} matches {_listed(sorted(sources))}", kind="ambiguous")
    source = next(iter(sources))
    return Recognized(source, file_path, sources[source])


def _json_sources(text: str) -> dict[str, str | None]:
    found: dict[str, str | None] = {}
    if kubernetes_events(text) is not None:
        found["kubernetes"] = None
    value = _json_value(text)
    if value is None:
        return found
    if _is_alert(value):
        found["alert"] = None
    if _is_metrics(value):
        found["metrics"] = None
    return found


def _banner_sources(text: str) -> dict[str, str | None]:
    found: dict[str, str | None] = {}
    execution = detect_execution_client(text)
    if execution is not None:
        found["execution"] = execution
    builder = detect_builder_client(text)
    if builder is not None:
        found["builder"] = builder
    roles: list[tuple[str, str]] = []
    for line in text.splitlines():
        named = detect_validator_client(line)
        if named is None:
            named = detect_consensus_client(line)
        if named is None:
            continue
        if named == "web3signer" or _VALIDATOR_WORD.search(line):
            roles.append(("validator", named))
        elif named in CONSENSUS_CLIENTS:
            roles.append(("consensus", named))
    role_names = {source for source, _client in roles}
    if len(role_names) > 1:
        found["consensus"] = None
        found["validator"] = None
    elif roles:
        source, client = roles[0]
        found[source] = client
    return found


def _json_value(text: str) -> object | None:
    stripped = text.lstrip()
    if not stripped.startswith(("{", "[")):
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _is_alert(value: object) -> bool:
    if isinstance(value, list):
        items = [item for item in value if isinstance(item, dict)]
        return bool(items) and all(_alert_item(item) for item in items)
    if not isinstance(value, dict):
        return False
    raw = value.get("alerts")
    if isinstance(raw, list):
        items = [item for item in raw if isinstance(item, dict)]
        return bool(items) and all(_alert_item(item) for item in items)
    return _alert_item(value)


def _alert_item(item: dict[str, object]) -> bool:
    labels = item.get("labels")
    named = labels.get("alertname") if isinstance(labels, dict) else None
    if not isinstance(named, str) or not named.strip():
        named = item.get("alertname")
    starts = item.get("startsAt")
    return isinstance(named, str) and bool(named.strip()) and isinstance(starts, str) and bool(starts.strip())


def _is_metrics(value: object) -> bool:
    if isinstance(value, list):
        return any(_metric_row(item) for item in value)
    if not isinstance(value, dict):
        return False
    if value.get("status") == "error":
        return True
    body = value.get("data")
    if isinstance(body, dict):
        if body.get("resultType") in _METRIC_TYPES:
            return True
        result = body.get("result")
        return isinstance(result, list) and any(_metric_row(item) for item in result)
    result = value.get("result")
    return isinstance(result, list) and any(_metric_row(item) for item in result)


def _metric_row(value: object) -> bool:
    return isinstance(value, dict) and isinstance(value.get("metric"), dict)


def _listed(names: list[str]) -> str:
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f", and {names[-1]}"
