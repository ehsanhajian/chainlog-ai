import contextlib
import io
import json
from pathlib import Path

import pytest

from chainlog_ai.case import case_home, load_case
from chainlog_ai.cli import main

EVENTS = """\
{
  "kind": "EventList",
  "items": [
    {
      "reason": "FailedMount",
      "message": "mount failed {not-json}",
      "involvedObject": {"kind": "Pod", "name": "geth"}
    },
    {
      "reason": "OOMKilled",
      "message": "container exceeded memory",
      "involvedObject": {"kind": "Pod", "name": "geth"}
    }
  ]
}
"""


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


def test_default_case_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHAINLOG_AI_HOME", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    assert case_home() == tmp_path / ".chainlog-ai"


def test_home_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "user-home"))
    monkeypatch.setenv("CHAINLOG_AI_HOME", str(tmp_path / "override"))
    assert case_home() == tmp_path / "override"


def test_ingest_writes_case_for_every_source(home: Path, tmp_path: Path) -> None:
    files = {
        "--execution": tmp_path / "geth.log",
        "--consensus": tmp_path / "beacon.log",
        "--validator": tmp_path / "validator.log",
        "--builder": tmp_path / "mev.log",
        "--kube": tmp_path / "events.json",
    }
    files["--execution"].write_text("imported new chain segment\n", encoding="utf-8")
    files["--consensus"].write_text("slot=12 attestation\n", encoding="utf-8")
    files["--validator"].write_text("published attestation\n", encoding="utf-8")
    files["--builder"].write_text("relay timeout\n", encoding="utf-8")
    files["--kube"].write_text(EVENTS, encoding="utf-8")

    argv = ["ingest"]
    for flag, path in files.items():
        argv.extend([flag, str(path)])
    code, stdout, stderr = run(*argv)

    assert code == 0
    assert stderr == ""
    case = load_case(case_id_from(stdout))
    assert (home / "cases" / case.id / "case.json").is_file()
    assert [event.source for event in case.events] == [
        "execution",
        "consensus",
        "validator",
        "builder",
        "kubernetes",
        "kubernetes",
    ]
    assert all(event.client is None for event in case.events)
    execution = case.events[0]
    raw = files["--execution"].read_bytes()
    assert execution.path == str(files["--execution"].resolve())
    assert raw[execution.byte_start : execution.byte_end].decode() == execution.excerpt
    assert execution.excerpt == "imported new chain segment"
    kube = case.events[4]
    event_text = files["--kube"].read_text(encoding="utf-8")
    parsed = json.loads(event_text[kube.byte_start : kube.byte_end])
    assert parsed["message"] == "mount failed {not-json}"
    assert kube.excerpt == "FailedMount: mount failed {not-json}"


def test_ingest_reads_plain_text_json_lines_and_keeps_duplicate_lines(
    home: Path, tmp_path: Path
) -> None:
    text = tmp_path / "client.log"
    text.write_bytes("same\nsame\ncafé\n".encode())
    jsonl = tmp_path / "client.jsonl"
    jsonl.write_text('{"msg":"hello"}\n{"msg":"world"}\n', encoding="utf-8")

    code, stdout, _stderr = run(
        "ingest",
        "--case",
        "textcase",
        "--execution",
        str(text),
        "--consensus",
        str(jsonl),
    )

    assert code == 0
    case = load_case(case_id_from(stdout))
    assert case.id == "textcase"
    excerpts = [event.excerpt for event in case.events]
    assert excerpts == ["same", "same", "café", '{"msg":"hello"}', '{"msg":"world"}']
    raw = text.read_bytes()
    for event in case.events[:3]:
        assert raw[event.byte_start : event.byte_end].decode() == event.excerpt
    assert case.events[2].byte_start == len("same\nsame\n".encode())


def test_second_ingest_appends_and_keeps_stored_lines_once(home: Path, tmp_path: Path) -> None:
    log = tmp_path / "geth.log"
    log.write_text("first line\nsecond line\n", encoding="utf-8")
    code, stdout, _stderr = run("ingest", "--case", "grow", "--execution", str(log))
    assert code == 0
    assert len(load_case("grow").events) == 2

    code, stdout, _stderr = run(
        "ingest",
        "--case",
        "grow",
        "--execution",
        str(log),
        "--execution",
        str(log),
    )
    assert code == 0
    assert case_id_from(stdout) == "grow"
    assert [event.excerpt for event in load_case("grow").events] == ["first line", "second line"]

    log.write_text("first line\nsecond line\nthird line\n", encoding="utf-8")
    extra = tmp_path / "beacon.log"
    extra.write_text("missed slot\n", encoding="utf-8")
    code, _stdout, _stderr = run(
        "ingest",
        "--case",
        "grow",
        "--execution",
        str(log),
        "--consensus",
        str(extra),
    )
    assert code == 0
    case = load_case("grow")
    assert [event.excerpt for event in case.events] == [
        "first line",
        "second line",
        "third line",
        "missed slot",
    ]
    assert [event.source for event in case.events] == [
        "execution",
        "execution",
        "execution",
        "consensus",
    ]


def test_ingest_preserves_flag_order(home: Path, tmp_path: Path) -> None:
    validator = tmp_path / "validator.log"
    execution = tmp_path / "geth.log"
    validator.write_text("validator line\n", encoding="utf-8")
    execution.write_text("execution line\n", encoding="utf-8")
    code, stdout, _stderr = run(
        "ingest",
        "--validator",
        str(validator),
        "--execution",
        str(execution),
    )
    assert code == 0
    case = load_case(case_id_from(stdout))
    assert [(event.source, event.excerpt) for event in case.events] == [
        ("validator", "validator line"),
        ("execution", "execution line"),
    ]


def test_why_accepts_the_ingest_case_id(home: Path, tmp_path: Path) -> None:
    log = tmp_path / "geth.log"
    log.write_text("database closed\n", encoding="utf-8")
    code, stdout, _stderr = run("ingest", "--execution", str(log))
    assert code == 0
    case_id = case_id_from(stdout)

    code, stdout, stderr = run("why", "--case", case_id)
    assert code == 1
    assert stderr == ""
    assert stdout.splitlines() == [
        f"case {case_id}",
        "evidence is not enough to state a cause",
    ]
    assert load_case(case_id).events[0].excerpt == "database closed"


def test_unreadable_input_is_a_usage_error(home: Path, tmp_path: Path) -> None:
    missing = tmp_path / "missing.log"
    present = tmp_path / "geth.log"
    present.write_text("kept\n", encoding="utf-8")
    code, stdout, stderr = run(
        "ingest",
        "--case",
        "partial",
        "--execution",
        str(present),
        "--consensus",
        str(missing),
    )
    assert code == 3
    assert stdout == ""
    assert "cannot read" in stderr
    assert not (home / "cases").exists()


def test_invalid_case_id_and_unknown_case(home: Path) -> None:
    code, _stdout, stderr = run("ingest", "--case", "../secret", "--execution", "geth.log")
    assert code == 3
    assert "invalid case id" in stderr

    code, _stdout, stderr = run("why", "--case", "missingcase")
    assert code == 3
    assert "no such case" in stderr

    code, _stdout, stderr = run("why")
    assert code == 3
    assert stderr != ""


def test_example_logs_open_a_case(home: Path) -> None:
    root = Path(__file__).parents[1] / "examples"
    code, stdout, stderr = run(
        "ingest",
        "--execution",
        str(root / "geth.log"),
        "--consensus",
        str(root / "beacon.log"),
        "--validator",
        str(root / "validator.log"),
        "--builder",
        str(root / "mev-boost.log"),
        "--kube",
        str(root / "events.json"),
    )
    assert code == 0
    assert stderr == ""
    case = load_case(case_id_from(stdout))
    counts: dict[str, int] = {}
    for event in case.events:
        counts[event.source] = counts.get(event.source, 0) + 1
    assert counts == {
        "execution": 20,
        "consensus": 13,
        "validator": 11,
        "builder": 7,
        "kubernetes": 8,
    }
    excerpts = [event.excerpt for event in case.events]
    blob = "\n".join(excerpts)
    assert "leveldb: closed" in blob
    assert "Unhealthy: Readiness probe failed: connection refused" in excerpts
    assert "hunter2" not in blob
    assert "abandon abandon" not in blob
    assert "eyJnot-a-jwt" not in blob
    assert "0x1111111111111111111111111111111111111111111111111111111111111111" not in blob
    assert "kkkkkkkkkkkkkkkkkkkkkkkk" not in blob
    assert any("[redacted]" in excerpt for excerpt in excerpts)
    assert any("0xefefefefefefefefefefefefefefefefefefefefefefefefefefefefefefefefefefefefefefefefefefefefefefefefefefefef" in excerpt for excerpt in excerpts)
    assert any("12D3KooWAbcdefghijkmnopqrstuvwxyz123456789" in excerpt for excerpt in excerpts)
    assert any("0xcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd" in excerpt for excerpt in excerpts)
    assert any(excerpt == "FailedMount: mount failed password=[redacted]" for excerpt in excerpts)
    execution = [event for event in case.events if event.source == "execution"]
    assert {event.client for event in execution} == {"geth"}
    assert all(event.time is not None and event.time.endswith("Z") for event in execution)
    assert any(event.time is not None and event.time.endswith("10-08T06:12:12.004Z") for event in execution)
    consensus = [event for event in case.events if event.source == "consensus"]
    assert {event.client for event in consensus} == {"lighthouse"}
    assert any(
        event.slot == 10274304 and event.epoch == 321072 and event.block == "0xabababababababab"
        for event in consensus
    )
    assert any(event.slot is None and event.epoch is None and event.block is None for event in consensus)
    validator = [event for event in case.events if event.source == "validator"]
    assert {event.client for event in validator} == {"lighthouse"}
    assert all(event.time is not None and event.time.endswith("Z") for event in validator)
    builder = [event for event in case.events if event.source == "builder"]
    assert {event.client for event in builder} == {"mev-boost"}
    assert all(event.time is not None and event.time.endswith("Z") for event in builder)
    assert any("relay" in event.excerpt for event in builder)
    assert any("builder timeout" in event.excerpt for event in builder)
    kube = [event for event in case.events if event.source == "kubernetes"]
    assert {event.reason for event in kube} == {
        "Unhealthy",
        "Killing",
        "FailedMount",
        "OOMKilled",
        "BackOff",
        "Evicted",
        "FailedScheduling",
        "NodeHasDiskPressure",
    }
    assert any(
        event.pod == "geth-0" and event.namespace == "ethereum" and event.container == "geth"
        for event in kube
    )
    pressure = next(event for event in kube if event.reason == "NodeHasDiskPressure")
    assert pressure.pod is None
    assert all(event.client is None and event.time is not None and event.time.endswith("Z") for event in kube)


def test_json_log_line_stays_one_excerpt(home: Path, tmp_path: Path) -> None:
    path = tmp_path / "geth.jsonl"
    line = '{"msg":"imported new chain segment","lvl":"info"}'
    path.write_text(line + "\n", encoding="utf-8")
    code, stdout, _stderr = run("ingest", "--execution", str(path))
    assert code == 0
    event = load_case(case_id_from(stdout)).events[0]
    assert event.source == "execution"
    assert event.excerpt == line
    assert path.read_bytes()[event.byte_start : event.byte_end].decode() == line


def test_kubernetes_event_array_uses_note(home: Path, tmp_path: Path) -> None:
    path = tmp_path / "events.json"
    path.write_text(
        json.dumps(
            [
                {
                    "reason": "BackOff",
                    "note": "restarting container",
                    "regarding": {"kind": "Pod", "name": "beacon"},
                }
            ]
        ),
        encoding="utf-8",
    )
    code, stdout, _stderr = run("ingest", "--kube", str(path))
    assert code == 0
    event = load_case(case_id_from(stdout)).events[0]
    assert event.excerpt == "BackOff: restarting container"
    assert json.loads(path.read_bytes()[event.byte_start : event.byte_end])["note"] == (
        "restarting container"
    )


def test_single_kubernetes_event(home: Path, tmp_path: Path) -> None:
    path = tmp_path / "event.json"
    path.write_text(
        json.dumps(
            {
                "kind": "Event",
                "reason": "Unhealthy",
                "message": "probe failed café",
                "involvedObject": {"kind": "Pod", "name": "beacon"},
            }
        ),
        encoding="utf-8",
    )
    code, stdout, _stderr = run("ingest", "--kube", str(path))
    assert code == 0
    event = load_case(case_id_from(stdout)).events[0]
    assert event.source == "kubernetes"
    assert event.excerpt == "Unhealthy: probe failed café"
    assert json.loads(path.read_bytes()[event.byte_start : event.byte_end])["reason"] == "Unhealthy"
