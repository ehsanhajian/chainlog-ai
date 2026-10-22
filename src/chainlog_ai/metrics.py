"""Read a Prometheus snapshot or range file into the case window."""

from __future__ import annotations

import json
import math
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from importlib.resources import files
from pathlib import Path

from chainlog_ai.case import Case, Event, append_events
from chainlog_ai.ingest import InputError
from chainlog_ai.redact import redact_excerpt

_HIDDEN = {"instance", "hostname", "nodename", "node", "host", "job"}
_COMPARES = {
    "available-percent-below",
    "utilization-percent-above",
    "below",
    "increase-at-least",
    "rate-above",
    "stale-seconds",
    "success-ratio-below",
}


@dataclass(frozen=True)
class Sample:
    at: datetime
    value: float


@dataclass(frozen=True)
class Series:
    name: str
    labels: dict[str, str]
    samples: tuple[Sample, ...]


@dataclass(frozen=True)
class MetricRule:
    id: str
    failure_class: str
    phrase: str
    compare: str
    threshold: float
    metrics: tuple[str, ...]
    severity: str
    serving: bool
    source: str


@dataclass(frozen=True)
class _Hit:
    at: datetime
    names: tuple[str, ...]
    measurement: str
    labels: dict[str, str]


def read_metrics(case: Case, path: Path) -> None:
    """Store each series that crosses a metric rule inside the case window."""

    file_path = path.expanduser()
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc.strerror}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InputError(f"cannot read metrics {path}") from exc
    found = _series(_result(data, path))
    if not found:
        raise InputError(f"metrics file has no series: {path}")
    start, end = _bounds(case)
    windowed = [
        Series(item.name, item.labels, tuple(_clip(item.samples, start, end)))
        for item in found
        if _clip(item.samples, start, end)
    ]
    events: list[Event] = []
    resolved = str(file_path.resolve())
    for rule in metric_rules():
        for hit in _apply(rule, windowed):
            events.append(_event(rule, hit, resolved))
    if events:
        append_events(case, events)


@lru_cache(maxsize=1)
def metric_rules() -> tuple[MetricRule, ...]:
    raw = files("chainlog_ai").joinpath("metrics.json").read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InputError("cannot read metric rules") from exc
    if not isinstance(data, list):
        raise InputError("cannot read metric rules")
    return tuple(_rule(item) for item in data)


def metric_phrase(failure_class: str) -> str:
    """Return the sentence documented for this metric class."""

    for rule in metric_rules():
        if rule.failure_class == failure_class:
            return rule.phrase
    return failure_class


def _rule(item: object) -> MetricRule:
    if not isinstance(item, dict):
        raise InputError("cannot read metric rules")
    rule_id = _text(item.get("id"))
    failure_class = _text(item.get("class"))
    phrase = _text(item.get("phrase"))
    compare = _text(item.get("compare"))
    document = _text(item.get("source"))
    severity = _text(item.get("severity"))
    names = item.get("metrics")
    threshold = item.get("threshold")
    serving = item.get("serving")
    if (
        rule_id is None
        or failure_class is None
        or phrase is None
        or compare not in _COMPARES
        or document is None
        or severity not in {"info", "warning", "critical"}
        or not isinstance(names, list)
        or not names
        or not all(isinstance(name, str) and name.strip() for name in names)
        or isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not isinstance(serving, bool)
    ):
        raise InputError("cannot read metric rules")
    return MetricRule(
        id=rule_id,
        failure_class=failure_class,
        phrase=phrase,
        compare=compare,
        threshold=float(threshold),
        metrics=tuple(name.strip() for name in names),
        severity=severity,
        serving=serving,
        source=document,
    )


def _result(data: object, path: Path) -> list[object]:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        raise InputError(f"cannot read metrics {path}")
    if data.get("status") == "error":
        raise InputError(f"cannot read metrics {path}")
    body = data.get("data", data)
    if isinstance(body, dict) and "result" in body:
        result = body["result"]
    elif "result" in data:
        result = data["result"]
    else:
        raise InputError(f"cannot read metrics {path}")
    if not isinstance(result, list):
        raise InputError(f"cannot read metrics {path}")
    return result


def _series(result: list[object]) -> list[Series]:
    found: list[Series] = []
    for item in result:
        if not isinstance(item, dict):
            continue
        metric = item.get("metric")
        labels: dict[str, str] = {}
        name = ""
        if isinstance(metric, dict):
            raw_name = metric.get("__name__")
            if isinstance(raw_name, str):
                name = raw_name.strip()
            for key, value in metric.items():
                if key == "__name__" or not isinstance(key, str) or not isinstance(value, str):
                    continue
                labels[key] = value
        if not name:
            named = item.get("name")
            if isinstance(named, str):
                name = named.strip()
        points: list[Sample] = []
        one = _point(item.get("value"))
        if one is not None:
            points.append(one)
        raw_values = item.get("values")
        if isinstance(raw_values, list):
            points.extend(point for raw in raw_values if (point := _point(raw)) is not None)
        samples = _dedupe(points)
        if name and samples:
            found.append(Series(name, labels, tuple(samples)))
    return found


def _point(item: object) -> Sample | None:
    if not isinstance(item, (list, tuple)) or len(item) < 2:
        return None
    at = _at(item[0])
    value = _number(item[1])
    if at is None or value is None:
        return None
    return Sample(at, value)


def _dedupe(points: list[Sample]) -> list[Sample]:
    by_time: dict[int, Sample] = {}
    for point in points:
        by_time[_millis(point.at)] = point
    return [by_time[key] for key in sorted(by_time)]


def _apply(rule: MetricRule, series: list[Series]) -> list[_Hit]:
    grouped: dict[tuple[tuple[str, str], ...], list[Series]] = {}
    for item in series:
        if item.name not in rule.metrics:
            continue
        key = tuple(sorted(item.labels.items()))
        grouped.setdefault(key, []).append(item)
    hits: list[_Hit] = []
    for key, items in grouped.items():
        labels = dict(key)
        for at, names, measurement in _evaluate(rule, items):
            hits.append(_Hit(at, names, measurement, labels))
    return hits


def _evaluate(rule: MetricRule, items: list[Series]) -> list[tuple[datetime, tuple[str, ...], str]]:
    by_name: dict[str, Series] = {}
    for item in items:
        by_name.setdefault(item.name, item)
    if rule.compare == "available-percent-below":
        return _ratio(rule, by_name, available=True)
    if rule.compare == "utilization-percent-above":
        return _ratio(rule, by_name, available=False)
    if rule.compare == "success-ratio-below":
        return _success(rule, by_name)
    found: list[tuple[datetime, tuple[str, ...], str]] = []
    for item in items:
        if rule.compare == "below":
            found.extend(_crossed(item, lambda value: value < rule.threshold, "value"))
        elif rule.compare == "increase-at-least":
            found.extend(_increase(item, rule.threshold))
        elif rule.compare == "rate-above":
            found.extend(_rate(item, rule.threshold))
        elif rule.compare == "stale-seconds":
            found.extend(_stale(item, rule.threshold))
    return found


def _ratio(
    rule: MetricRule,
    by_name: dict[str, Series],
    *,
    available: bool,
) -> list[tuple[datetime, tuple[str, ...], str]]:
    if len(rule.metrics) < 2:
        return []
    left = by_name.get(rule.metrics[0])
    right = by_name.get(rule.metrics[1])
    if left is None or right is None:
        return []
    names = rule.metrics
    hits: list[tuple[datetime, tuple[str, ...], str]] = []
    active = False
    for numerator, denominator in _aligned(left, right):
        if denominator.value == 0:
            active = False
            continue
        percent = 100 * numerator.value / denominator.value
        if not available:
            percent = 100 - percent
        crossed = percent < rule.threshold if available else percent > rule.threshold
        label = "available" if available else "utilization"
        if crossed and not active:
            hits.append((numerator.at, names, f"{label} {_num(percent)}%"))
            active = True
        elif not crossed:
            active = False
    return hits


def _success(
    rule: MetricRule,
    by_name: dict[str, Series],
) -> list[tuple[datetime, tuple[str, ...], str]]:
    if len(rule.metrics) < 2:
        return []
    hit = by_name.get(rule.metrics[0])
    miss = by_name.get(rule.metrics[1])
    if hit is None or miss is None or len(hit.samples) < 2 or len(miss.samples) < 2:
        return []
    hit_delta = hit.samples[-1].value - hit.samples[0].value
    miss_delta = miss.samples[-1].value - miss.samples[0].value
    total = hit_delta + miss_delta
    if hit_delta < 0 or miss_delta < 0 or total <= 0:
        return []
    ratio = hit_delta / total
    if ratio >= rule.threshold:
        return []
    when = max(hit.samples[-1].at, miss.samples[-1].at)
    return [(when, rule.metrics, f"effectiveness {_num(ratio)}")]


def _crossed(
    series: Series,
    predicate,
    word: str,
) -> list[tuple[datetime, tuple[str, ...], str]]:
    hits: list[tuple[datetime, tuple[str, ...], str]] = []
    active = False
    for sample in series.samples:
        crossed = predicate(sample.value)
        if crossed and not active:
            hits.append((sample.at, (series.name,), f"{word} {_num(sample.value)}"))
            active = True
        elif not crossed:
            active = False
    return hits


def _increase(series: Series, threshold: float) -> list[tuple[datetime, tuple[str, ...], str]]:
    if len(series.samples) < 2:
        return []
    base = series.samples[0].value
    hits: list[tuple[datetime, tuple[str, ...], str]] = []
    active = False
    for sample in series.samples[1:]:
        delta = sample.value - base
        if delta >= threshold and not active:
            hits.append((sample.at, (series.name,), f"increase {_num(delta)}"))
            active = True
        elif delta < threshold:
            active = False
    return hits


def _rate(series: Series, threshold: float) -> list[tuple[datetime, tuple[str, ...], str]]:
    hits: list[tuple[datetime, tuple[str, ...], str]] = []
    active = False
    previous: Sample | None = None
    for sample in series.samples:
        if previous is not None:
            elapsed = (sample.at - previous.at).total_seconds()
            delta = sample.value - previous.value
            rate = delta / elapsed if elapsed > 0 and delta >= 0 else None
            if rate is not None and rate > threshold and not active:
                hits.append((sample.at, (series.name,), f"rate {_num(rate)}"))
                active = True
            elif rate is None or rate <= threshold:
                active = False
        previous = sample
    return hits


def _stale(series: Series, threshold: float) -> list[tuple[datetime, tuple[str, ...], str]]:
    if len(series.samples) < 2:
        return []
    anchor = series.samples[0]
    hits: list[tuple[datetime, tuple[str, ...], str]] = []
    active = False
    for sample in series.samples[1:]:
        if sample.value > anchor.value:
            anchor = sample
            active = False
            continue
        elapsed = (sample.at - anchor.at).total_seconds()
        if elapsed > threshold and not active:
            hits.append((sample.at, (series.name,), f"stalled {_num(elapsed)}s"))
            active = True
    return hits


def _aligned(left: Series, right: Series) -> list[tuple[Sample, Sample]]:
    paired = {_millis(sample.at): sample for sample in right.samples}
    return [
        (sample, paired[_millis(sample.at)])
        for sample in left.samples
        if _millis(sample.at) in paired
    ]


def _event(rule: MetricRule, hit: _Hit, path: str) -> Event:
    shown = [f"{key}={hit.labels[key]}" for key in sorted(hit.labels) if key not in _HIDDEN and hit.labels[key]]
    excerpt = " ".join(part for part in ("/".join(hit.names), " ".join(shown), hit.measurement) if part)
    identity = ",".join(f"{key}={hit.labels[key]}" for key in sorted(hit.labels))
    token = zlib.crc32(f"{rule.id}|{identity}|{_iso(hit.at)}".encode()) & 0x7FFFFFFF
    return Event(
        source="metric",
        client=None,
        path=path,
        byte_start=token,
        byte_end=token,
        excerpt=redact_excerpt(excerpt),
        time=_iso(hit.at),
        failure_class=rule.failure_class,
        rule=rule.id,
        severity=rule.severity,
        serving=rule.serving,
    )


def _clip(samples: tuple[Sample, ...], start: datetime | None, end: datetime | None) -> list[Sample]:
    kept: list[Sample] = []
    for sample in samples:
        if start is not None and sample.at < start:
            continue
        if end is not None and sample.at > end:
            continue
        kept.append(sample)
    return kept


def _bounds(case: Case) -> tuple[datetime | None, datetime | None]:
    return _parse_iso(case.since), _parse_iso(case.until)


def _parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    raw = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _at(raw: object) -> datetime | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return None
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        if text.endswith("Z") or "T" in text:
            return _parse_iso(text)
        try:
            number = float(text)
        except ValueError:
            return None
    else:
        number = float(raw)
    if not math.isfinite(number):
        return None
    if number > 10**12:
        number /= 1000.0
    return datetime.fromtimestamp(number, timezone.utc)


def _number(raw: object) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    return value


def _millis(stamp: datetime) -> int:
    return int(stamp.timestamp() * 1000)


def _num(value: float) -> str:
    text = f"{value:.2f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _iso(stamp: datetime) -> str:
    return stamp.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
