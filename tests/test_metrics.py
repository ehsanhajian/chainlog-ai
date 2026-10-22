import contextlib
import io
import json
from pathlib import Path

import pytest

from chainlog_ai.case import Case, Event, load_case
from chainlog_ai.cli import main
from chainlog_ai.timeline import render_timeline


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    user_home = tmp_path / "user-home"
    user_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: user_home))
    root = tmp_path / "chainlog-home"
    monkeypatch.setenv("CHAINLOG_AI_HOME", str(root))
    return root


def run(*argv: str) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(list(argv))
    return code, stdout.getvalue(), stderr.getvalue()


def _log(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "INFO [10-08|06:12:00.000] Imported new chain segment number=1",
                'ERROR [10-08|06:13:58.883] Failed to persist ancient block err="leveldb: closed"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _matrix(series: list[dict[str, object]]) -> str:
    return json.dumps({"status": "success", "data": {"resultType": "matrix", "result": series}})


def _series(name: str, samples: list[list[object]], **labels: str) -> dict[str, object]:
    metric = {"__name__": name, **labels}
    return {"metric": metric, "values": samples}


def test_metric_moves_in_the_window_are_contributors(home: Path, tmp_path: Path) -> None:
    log = tmp_path / "geth.log"
    _log(log)
    root = Path(__file__).parents[1] / "examples" / "metrics.json"
    code, stdout, stderr = run(
        "why",
        "--case",
        "metrics",
        "--execution",
        str(log),
        "--execution-client",
        "geth",
        "--metrics",
        str(root),
    )
    assert code == 0, stderr
    trigger = next(line for line in stdout.splitlines() if line.startswith("trigger:"))
    assert "database-closed" in trigger
    assert "metric" not in trigger
    for phrase in (
        "disk usage fell below 5% available",
        "disk io queue rose above 10",
        "memory utilization rose above 90%",
        "restart count rose by at least 1",
        "peer count fell below 1",
        "execution head stopped advancing for more than 12s",
        "consensus head stopped advancing for more than 12s",
        "attestation effectiveness fell below 1",
    ):
        assert f"contributor: metric {phrase}" in stdout
    assert "mountpoint=/var" not in stdout
    assert "instance=" not in stdout
    code, stdout, stderr = run("classify", "--case", "metrics")
    assert code == 0, stderr
    assert "disk-usage 1" in stdout
    assert "peer-count 1" in stdout
    code, _stdout, stderr = run("why", "--case", "metrics", "--metrics", str(root))
    assert code == 0, stderr
    case = load_case("metrics")
    assert len([event for event in case.events if event.source == "metric"]) == 8


def test_a_snapshot_and_a_healthy_series_stay_within_the_rules(home: Path, tmp_path: Path) -> None:
    log = tmp_path / "geth.log"
    _log(log)
    metrics = tmp_path / "snapshot.json"
    metrics.write_text(
        json.dumps(
            {
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [
                        {
                            "metric": {
                                "__name__": "node_filesystem_avail_bytes",
                                "mountpoint": "/data",
                                "instance": "validator.example",
                            },
                            "value": [1791439980, "4000000000"],
                        },
                        {
                            "metric": {"__name__": "node_filesystem_size_bytes", "mountpoint": "/data", "instance": "validator.example"},
                            "value": [1791439980, "100000000000"],
                        },
                        {
                            "metric": {"__name__": "node_memory_MemAvailable_bytes"},
                            "value": [1791439980, "100"],
                        },
                        {
                            "metric": {"__name__": "node_memory_MemTotal_bytes"},
                            "value": [1791439980, "1000"],
                        },
                        {"metric": {"__name__": "p2p_peers"}, "value": [1791439980, "1"]},
                        {"metric": {"__name__": "libp2p_peers"}, "value": [1791439980, "0"]},
                        {"metric": {"__name__": "kube_pod_container_status_restarts_total"}, "value": [1791439980, "4"]},
                        {"metric": {"__name__": "chain_head_block"}, "value": [1791439980, "10"]},
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    code, stdout, stderr = run(
        "why",
        "--case",
        "snapshot",
        "--execution",
        str(log),
        "--execution-client",
        "geth",
        "--metrics",
        str(metrics),
    )
    assert code == 0, stderr
    assert "disk usage fell below 5% available" in stdout
    assert "peer count fell below 1" in stdout
    assert "memory utilization rose above 90%" not in stdout
    assert "restart count rose" not in stdout
    assert "execution head stopped" not in stdout
    assert "validator.example" not in stdout
    assert "mountpoint=/data" in stdout


def test_samples_outside_the_log_span_are_not_the_contributor(home: Path, tmp_path: Path) -> None:
    log = tmp_path / "geth.log"
    _log(log)
    metrics = tmp_path / "early.json"
    metrics.write_text(
        _matrix(
            [
                _series(
                    "node_filesystem_avail_bytes",
                    [[1791432000, "1000000000"], [1791432000, "1000000000"]],
                    mountpoint="/old",
                ),
                _series(
                    "node_filesystem_size_bytes",
                    [[1791432000, "100000000000"], [1791432000, "100000000000"]],
                    mountpoint="/old",
                ),
            ]
        ),
        encoding="utf-8",
    )
    code, stdout, stderr = run(
        "why",
        "--case",
        "early",
        "--execution",
        str(log),
        "--execution-client",
        "geth",
        "--metrics",
        str(metrics),
    )
    assert code == 0, stderr
    assert "contributor:" not in stdout
    assert "disk usage" not in stdout
    code, stdout, stderr = run("timeline", "--case", "early")
    assert code == 0, stderr
    assert "2026-10-08T04:00:00.000Z" in stdout
    assert "mountpoint=/old" in stdout


def test_alert_then_metrics_uses_the_case_window(home: Path, tmp_path: Path) -> None:
    alert = tmp_path / "alert.json"
    alert.write_text(
        '{"alertname": "ValidatorMissedAttestation", "startsAt": "2026-10-08T06:14:00Z", "status": "firing"}\n',
        encoding="utf-8",
    )
    metrics = tmp_path / "disk.json"
    metrics.write_text(
        _matrix(
            [
                _series(
                    "node_filesystem_avail_bytes",
                    [[1791432000, "1000000000"], [1791439980, "4000000000"]],
                    mountpoint="/data",
                ),
                _series(
                    "node_filesystem_size_bytes",
                    [[1791432000, "100000000000"], [1791439980, "100000000000"]],
                    mountpoint="/data",
                ),
            ]
        ),
        encoding="utf-8",
    )
    log = tmp_path / "geth.log"
    _log(log)
    code, _stdout, stderr = run(
        "ingest",
        "--case",
        "window",
        "--alert",
        str(alert),
        "--execution",
        str(log),
        "--execution-client",
        "geth",
        "--metrics",
        str(metrics),
    )
    assert code == 0, stderr
    case = load_case("window")
    disks = [event for event in case.events if event.failure_class == "disk-usage"]
    assert len(disks) == 1
    assert disks[0].time == "2026-10-08T06:13:00.000Z"
    code, stdout, stderr = run("why", "--case", "window")
    assert code == 0, stderr
    assert "disk usage fell below 5% available at 2026-10-08T06:13:00.000Z." in stdout
    assert "2026-10-08T04:00:00.000Z" not in stdout
    trigger = next(line for line in stdout.splitlines() if line.startswith("trigger:"))
    assert "database-closed" in trigger


def test_a_metric_between_clocks_does_not_hide_the_gap(home: Path) -> None:
    case_events = [
        Event(
            source="execution",
            client="geth",
            path="/tmp/geth.log",
            byte_start=0,
            byte_end=1,
            excerpt="Imported new chain segment",
            time="2026-10-08T06:00:00Z",
        ),
        Event(
            source="metric",
            client=None,
            path="/tmp/metrics.json",
            byte_start=0,
            byte_end=0,
            excerpt="p2p_peers value 0",
            time="2026-10-08T06:10:00Z",
            failure_class="peer-count",
        ),
        Event(
            source="kubernetes",
            client=None,
            path="/tmp/events.json",
            byte_start=0,
            byte_end=1,
            excerpt="NodeHasDiskPressure",
            time="2026-10-08T06:20:00Z",
        ),
    ]
    text = render_timeline(Case(id="gap", events=case_events))
    assert "clock gap 1200s between execution and kubernetes" in text
    assert "between metric" not in text
    assert "and metric" not in text


def test_a_bad_metrics_file_is_a_usage_error(home: Path, tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    code, stdout, stderr = run("ingest", "--metrics", str(broken))
    assert code == 3
    assert stdout == ""
    assert "cannot read metrics" in stderr
    empty = tmp_path / "empty.json"
    empty.write_text('{"status":"success","data":{"resultType":"vector","result":[]}}\n', encoding="utf-8")
    code, stdout, stderr = run("ingest", "--metrics", str(empty))
    assert code == 3
    assert "metrics file has no series" in stderr
    code, stdout, stderr = run("ingest", "--metrics", str(tmp_path / "missing.json"))
    assert code == 3
    assert stdout == ""
    assert "cannot read" in stderr
