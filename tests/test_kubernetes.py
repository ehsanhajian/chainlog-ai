import contextlib
import io
from pathlib import Path

import pytest

from chainlog_ai.case import load_case
from chainlog_ai.cli import main


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


def case_id_from(stdout: str) -> str:
    line = stdout.strip().splitlines()[0]
    assert line.startswith("case ")
    return line.removeprefix("case ")


def test_events_record_pod_container_namespace_and_reason(home: Path) -> None:
    path = Path(__file__).parents[1] / "examples" / "events.json"
    code, stdout, stderr = run("ingest", "--kube", str(path))
    assert code == 0, stderr
    events = load_case(case_id_from(stdout)).events
    reasons = {event.reason for event in events}
    assert reasons == {
        "OOMKilled",
        "Killing",
        "BackOff",
        "Unhealthy",
        "Evicted",
        "FailedMount",
        "FailedScheduling",
        "NodeHasDiskPressure",
        "NodeHasInsufficientMemory",
        "NodeNotReady",
    }
    pod_events = [event for event in events if event.pod == "geth-0"]
    assert pod_events
    assert all(event.namespace == "ethereum" and event.source == "kubernetes" for event in pod_events)
    assert any(event.container == "geth" for event in pod_events)
    pressure = next(event for event in events if event.reason == "NodeHasDiskPressure")
    assert pressure.pod is None
    assert pressure.time == "2026-10-08T06:13:40Z"


def test_previous_container_is_a_separate_source_and_restart_is_counted(home: Path) -> None:
    root = Path(__file__).parents[1] / "examples" / "pods" / "ethereum_geth-0_poduid" / "geth"
    code, stdout, stderr = run(
        "ingest",
        "--kube",
        str(root / "0.log"),
        "--kube",
        str(root / "1.log"),
    )
    assert code == 0, stderr
    events = load_case(case_id_from(stdout)).events
    previous = [event for event in events if event.restart == 0]
    current = [event for event in events if event.restart == 1]
    assert previous and current
    assert {event.source for event in previous} == {"kubernetes-previous"}
    assert {event.source for event in current} == {"kubernetes"}
    assert all(
        event.client == "geth"
        and event.pod == "geth-0"
        and event.container == "geth"
        and event.namespace == "ethereum"
        for event in events
    )
    assert any("leveldb: closed" in event.excerpt for event in previous)
    assert any(event.excerpt.startswith("INFO ") for event in current)
    assert all("stdout" not in event.excerpt for event in events)


def test_flags_name_the_pod_and_mark_the_previous_container(home: Path, tmp_path: Path) -> None:
    path = tmp_path / "beacon.log"
    path.write_text("Oct 08 06:12:00.102 INFO  Lighthouse started\n", encoding="utf-8")
    code, stdout, stderr = run(
        "ingest",
        "--kube",
        str(path),
        "--namespace",
        "ethereum",
        "--pod",
        "beacon-0",
        "--container",
        "lighthouse",
        "--restart",
        "2",
        "--previous",
    )
    assert code == 0, stderr
    event = load_case(case_id_from(stdout)).events[0]
    assert event.source == "kubernetes-previous"
    assert event.client == "lighthouse"
    assert event.pod == "beacon-0"
    assert event.container == "lighthouse"
    assert event.namespace == "ethereum"
    assert event.restart == 2
    assert event.time is not None and event.time.endswith("10-08T06:12:00.102Z")


def test_container_line_without_a_banner_keeps_the_pod(home: Path, tmp_path: Path) -> None:
    path = tmp_path / "app.log"
    path.write_text("connection refused\n", encoding="utf-8")
    code, stdout, stderr = run(
        "ingest",
        "--kube",
        str(path),
        "--pod",
        "geth-0",
        "--namespace",
        "ethereum",
    )
    assert code == 0, stderr
    event = load_case(case_id_from(stdout)).events[0]
    assert event.source == "kubernetes"
    assert event.client is None
    assert event.pod == "geth-0"
    assert event.namespace == "ethereum"
    assert event.excerpt == "connection refused"


def test_kube_flags_need_a_preceding_file(home: Path) -> None:
    code, _stdout, stderr = run("ingest", "--pod", "geth-0")
    assert code == 3
    assert "needs a preceding --kube file" in stderr
