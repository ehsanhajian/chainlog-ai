import contextlib
import io
import json
from pathlib import Path

import pytest

from chainlog_ai.case import Event, load_case, save_case
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


def test_an_alert_opens_the_case_and_sets_the_window(home: Path) -> None:
    root = Path(__file__).parents[1] / "examples"
    code, stdout, stderr = run(
        "ingest",
        "--case",
        "alerted",
        "--alert",
        str(root / "alert.json"),
        "--execution",
        str(root / "geth.log"),
    )
    assert code == 0, stderr
    assert stdout == "case alerted\n"
    case = load_case("alerted")
    assert case.since == "2026-10-08T05:14:00.010Z"
    assert case.until == "2026-10-08T06:29:00.010Z"
    alerts = [event for event in case.events if event.source == "alert"]
    assert len(alerts) == 1
    assert alerts[0].failure_class is None
    assert alerts[0].time == "2026-10-08T06:14:00.010Z"
    assert alerts[0].excerpt == "ValidatorMissedAttestation firing"
    code, stdout, stderr = run("timeline", "--case", "alerted")
    assert code == 0, stderr
    assert "2026-10-08T06:14:00.010Z alert - unclassified ValidatorMissedAttestation firing" in stdout
    assert "leveldb: closed" in stdout
    code, stdout, stderr = run("classify", "--case", "alerted")
    assert code == 0, stderr
    assert "ValidatorMissedAttestation " not in stdout


def test_the_alert_is_not_the_cause_when_a_client_failed(home: Path, tmp_path: Path) -> None:
    alert = tmp_path / "alert.json"
    alert.write_text(
        '{"alertname": "ValidatorMissedAttestation", "startsAt": "2026-10-08T06:14:00Z", "status": "firing"}\n',
        encoding="utf-8",
    )
    log = tmp_path / "geth.log"
    log.write_text(
        'ERROR [10-08|06:13:58.883] Failed to persist ancient block err="leveldb: closed"\n',
        encoding="utf-8",
    )
    code, _stdout, stderr = run(
        "ingest",
        "--case",
        "caused",
        "--alert",
        str(alert),
        "--execution",
        str(log),
        "--execution-client",
        "geth",
    )
    assert code == 0, stderr
    code, stdout, stderr = run("why", "--case", "caused")
    assert code == 0, stderr
    trigger = next(line for line in stdout.splitlines() if line.startswith("trigger:"))
    assert "database-closed" in trigger
    assert "ValidatorMissedAttestation" not in trigger


def test_a_saved_alert_and_the_window_hide_lines_outside_it(home: Path, tmp_path: Path) -> None:
    alert = tmp_path / "saved.json"
    alert.write_text(
        '{"alertname": "NodeDiskPressure", "startsAt": "2026-10-08T06:00:00Z", "status": "firing"}\n',
        encoding="utf-8",
    )
    code, _stdout, stderr = run("ingest", "--case", "saved", "--alert", str(alert))
    assert code == 0, stderr
    case = load_case("saved")
    assert case.since == "2026-10-08T05:00:00.000Z"
    assert case.until == "2026-10-08T06:15:00.000Z"
    case.events.append(
        Event(
            source="execution",
            client="geth",
            path="/tmp/outside.log",
            byte_start=0,
            byte_end=1,
            excerpt="OUTSIDE-WINDOW",
            time="2026-10-08T08:00:00Z",
        )
    )
    case.events.append(
        Event(
            source="execution",
            client="geth",
            path="/tmp/inside.log",
            byte_start=0,
            byte_end=1,
            excerpt="INSIDE-WINDOW",
            time="2026-10-08T06:10:00Z",
        )
    )
    save_case(case)
    code, stdout, stderr = run("timeline", "--case", "saved")
    assert code == 0, stderr
    assert "NodeDiskPressure firing" in stdout
    assert "INSIDE-WINDOW" in stdout
    assert "OUTSIDE-WINDOW" not in stdout
    code, stdout, stderr = run(
        "timeline",
        "--case",
        "saved",
        "--since",
        "2026-10-08T07:00:00Z",
        "--until",
        "2026-10-08T09:00:00Z",
    )
    assert code == 0, stderr
    assert "OUTSIDE-WINDOW" in stdout
    assert "INSIDE-WINDOW" not in stdout


def test_the_alert_alone_is_not_the_cause(home: Path) -> None:
    root = Path(__file__).parents[1] / "examples"
    code, _stdout, stderr = run("ingest", "--case", "only-alert", "--alert", str(root / "alert.json"))
    assert code == 0, stderr
    code, stdout, stderr = run("why", "--case", "only-alert")
    assert code == 1, stderr
    assert "trigger:" not in stdout
    assert "ValidatorMissedAttestation firing" in stdout
    code, _stdout, stderr = run("ingest", "--case", "only-alert", "--alert", str(root / "alert.json"))
    assert code == 0, stderr
    case = load_case("only-alert")
    assert len([event for event in case.events if event.source == "alert"]) == 1


def test_two_alerts_span_the_window_from_the_earliest_to_the_latest(home: Path, tmp_path: Path) -> None:
    alert = tmp_path / "pair.json"
    alert.write_text(
        json.dumps(
            {
                "alerts": [
                    {"labels": {"alertname": "DiskFull"}, "startsAt": "2026-10-08T06:00:00Z", "status": "firing"},
                    {
                        "labels": {"alertname": "PeerCountLow"},
                        "startsAt": "2026-10-08T06:20:00.500Z",
                        "status": "firing",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    code, _stdout, stderr = run("ingest", "--case", "pair", "--alert", str(alert))
    assert code == 0, stderr
    case = load_case("pair")
    assert case.since == "2026-10-08T05:00:00.000Z"
    assert case.until == "2026-10-08T06:35:00.500Z"
    code, stdout, stderr = run("timeline", "--case", "pair")
    assert code == 0, stderr
    assert "DiskFull firing" in stdout
    assert "PeerCountLow firing" in stdout


def test_a_bad_alert_file_is_a_usage_error(home: Path, tmp_path: Path) -> None:
    empty = tmp_path / "empty.json"
    empty.write_text("{}\n", encoding="utf-8")
    code, stdout, stderr = run("ingest", "--alert", str(empty))
    assert code == 3
    assert stdout == ""
    assert "alert file has no alert" in stderr
    naive = tmp_path / "naive.json"
    naive.write_text('{"alertname": "DiskFull", "startsAt": "2026-10-08T06:14:00"}\n', encoding="utf-8")
    code, stdout, stderr = run("ingest", "--alert", str(naive))
    assert code == 3
    assert "alert time needs a timezone" in stderr
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    code, stdout, stderr = run("ingest", "--alert", str(broken))
    assert code == 3
    assert stdout == ""
    assert "cannot read alert" in stderr
    code, stdout, stderr = run("ingest", "--alert", str(tmp_path / "missing.json"))
    assert code == 3
    assert stdout == ""
    assert "cannot read" in stderr
