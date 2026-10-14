import contextlib
import io
from datetime import datetime
from pathlib import Path

import pytest

from chainlog_ai.case import Case, Event
from chainlog_ai.cli import main
from chainlog_ai.timeline import TimelineError, render_timeline


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


def _event(
    source: str,
    excerpt: str,
    *,
    time: str | None,
    client: str | None = None,
    failure_class: str | None = None,
) -> Event:
    return Event(
        source=source,
        client=client,
        path=f"/tmp/{source}.log",
        byte_start=0,
        byte_end=len(excerpt),
        excerpt=excerpt,
        time=time,
        failure_class=failure_class,
    )


def test_same_timestamp_stays_in_source_order() -> None:
    stamp = "2026-10-08T06:14:00Z"
    case = Case(
        id="order",
        events=[
            _event("builder", "builder timeout", time=stamp, client="mev-boost"),
            _event("validator", "Missed attestation", time=stamp, client="lighthouse"),
            _event("execution", "leveldb: closed", time=stamp, client="geth"),
            _event("consensus", "Missed attestation", time=stamp, client="lighthouse"),
            _event("kubernetes-previous", "previous container", time=stamp, client="geth"),
            _event("kubernetes", "OOMKilled: memory", time=stamp, failure_class="oom-killed"),
        ],
    )
    lines = render_timeline(case).splitlines()[1:]
    sources = [line.split()[1] for line in lines]
    assert sources == [
        "kubernetes",
        "kubernetes-previous",
        "execution",
        "consensus",
        "validator",
        "builder",
    ]
    assert lines[0].startswith(f"{stamp} kubernetes - oom-killed ")
    assert "clock gap " not in render_timeline(case)


def test_clock_gap_is_called_out_only_between_a_client_and_the_cluster() -> None:
    case = Case(
        id="gap",
        events=[
            _event("execution", "Imported new chain segment", time="2026-10-08T06:00:00Z", client="geth"),
            _event("execution", "Chain head updated", time="2026-10-08T06:12:00Z", client="geth"),
            _event("kubernetes", "FailedScheduling: 0/3 nodes are available", time="2026-10-08T06:10:00Z"),
            _event("kubernetes", "OOMKilled: memory", time="2026-10-08T06:13:58Z"),
            _event("execution", "leveldb: closed", time="2026-10-08T06:13:59Z", client="geth", failure_class="database-closed"),
        ],
    )
    text = render_timeline(case)
    assert "clock gap 600s between execution and kubernetes" in text
    assert "clock gap 120s between kubernetes and execution" in text
    assert "clock gap 1s" not in text
    assert "between execution and execution" not in text


def test_since_and_until_keep_the_inclusive_window() -> None:
    case = Case(
        id="window",
        events=[
            _event("execution", "early", time="2026-10-08T06:00:00Z", client="geth"),
            _event("execution", "inside", time="2026-10-08T06:13:58Z", client="geth"),
            _event("execution", "late", time="2026-10-08T06:14:20Z", client="geth"),
            _event("execution", "undated", time=None, client="geth"),
        ],
    )
    text = render_timeline(case, since="2026-10-08T06:13:58Z", until="2026-10-08T06:13:58Z")
    assert "inside" in text
    assert "early" not in text
    assert "late" not in text
    assert "undated" not in text
    whole = render_timeline(case)
    assert whole.strip().splitlines()[-1].endswith(" undated")


def test_example_incident_timeline(home: Path) -> None:
    root = Path(__file__).parents[1] / "examples"
    code, _stdout, stderr = run(
        "ingest",
        "--case",
        "incident",
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
    assert code == 0, stderr
    code, stdout, stderr = run("timeline", "--case", "incident")
    assert code == 0, stderr
    assert stdout.startswith("case incident\n")
    assert "unclassified" in stdout
    assert "hunter2" not in stdout
    assert "[redacted]" in stdout
    rows = [line for line in stdout.splitlines()[1:] if not line.startswith("clock gap ")]
    times = [
        datetime.fromisoformat(line.split()[0].replace("Z", "+00:00"))
        for line in rows
        if line.split()[0] != "-"
    ]
    assert times == sorted(times)
    assert any("clock gap " in line for line in stdout.splitlines())

    code, stdout, stderr = run("classify", "--case", "incident")
    assert code == 0, stderr
    code, stdout, stderr = run(
        "timeline",
        "--case",
        "incident",
        "--since",
        "2026-10-08T06:13:50Z",
        "--until",
        "2026-10-08T06:14:00Z",
    )
    assert code == 0, stderr
    assert "database-closed" in stdout
    assert "Starting Geth" not in stdout
    assert "Slashable attestation" not in stdout
    assert "hunter2" not in stdout


def test_a_reversed_window_is_rejected() -> None:
    case = Case(id="window", events=[_event("execution", "inside", time="2026-10-08T06:13:58Z", client="geth")])
    with pytest.raises(TimelineError, match="after --until"):
        render_timeline(case, since="2026-10-08T06:14:00Z", until="2026-10-08T06:13:00Z")


def test_unknown_case_and_bad_window_are_usage_errors(home: Path) -> None:
    code, _stdout, stderr = run("timeline", "--case", "missing")
    assert code == 3
    assert "no such case" in stderr
    code, _stdout, stderr = run("timeline", "--case", "missing", "--since", "yesterday")
    assert code == 3
    code, _stdout, stderr = run(
        "timeline",
        "--case",
        "missing",
        "--since",
        "2026-10-08T06:14:00Z",
        "--until",
        "2026-10-08T06:13:00Z",
    )
    assert code == 3
