import contextlib
import io
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
    slot: int | None = None,
    epoch: int | None = None,
    block: str | None = None,
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
        slot=slot,
        epoch=epoch,
        block=block,
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
    lines = [line for line in render_timeline(case).splitlines() if line.startswith(stamp)]
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


def test_a_row_shows_the_slot_epoch_and_block_the_line_prints() -> None:
    case = Case(
        id="fields",
        events=[
            _event(
                "consensus",
                "Synced slot: 10274304, epoch: 321072, block: 0xabab",
                time="2026-10-08T06:12:00.102Z",
                client="lighthouse",
            ),
            _event(
                "validator",
                "Successfully published attestation slot: 10274304",
                time="2026-10-08T06:12:00.180Z",
                client="lighthouse",
            ),
            _event(
                "consensus",
                "head advanced",
                time="2026-10-08T06:12:01Z",
                client="lighthouse",
                slot=8,
                epoch=1,
                block="0xcd",
            ),
        ],
    )
    text = render_timeline(case)
    assert "slot=10274304 epoch=321072 block=0xabab" in text
    assert "slot=10274304 inferred" not in text
    assert "slot=8 epoch=1 block=0xcd" in text
    published = next(line for line in text.splitlines() if "Successfully published" in line)
    assert "slot=10274304" in published
    assert "epoch=" not in published
    assert "inferred" not in published


def test_a_missing_slot_is_inferred_and_rows_in_that_slot_stay_together() -> None:
    case = Case(
        id="group",
        events=[
            _event("execution", "early head", time="2026-10-08T06:12:00Z", client="geth"),
            _event(
                "consensus",
                "Synced slot: 10, epoch: 2, block: 0xaaaa",
                time="2026-10-08T06:13:58.900Z",
                client="lighthouse",
            ),
            _event("execution", "leveldb: closed", time="2026-10-08T06:13:58.883Z", client="geth"),
            _event("builder", "getHeader slot=10", time="2026-10-08T06:14:00Z", client="mev-boost"),
            _event("validator", "later duty", time="2026-10-08T06:14:20Z", client="lighthouse"),
            _event(
                "consensus",
                "Missed attestation slot: 11",
                time="2026-10-08T06:14:20.010Z",
                client="lighthouse",
            ),
        ],
    )
    rows = [line for line in render_timeline(case).splitlines()[1:] if not line.startswith("clock gap ")]
    closed = next(line for line in rows if "leveldb: closed" in line)
    assert "slot=10 inferred" in closed
    assert "epoch=" not in closed
    assert "block=" not in closed
    header = next(line for line in rows if "getHeader" in line)
    assert "slot=10" in header
    assert "inferred" not in header
    slots = ["slot=10" in line for line in rows]
    first = slots.index(True)
    last = len(slots) - 1 - slots[::-1].index(True)
    assert all(slots[first : last + 1])
    assert rows[first].split()[1] == "execution"
    assert "leveldb: closed" in rows[first]


def test_disagreeing_consensus_lines_leave_the_slot_blank() -> None:
    case = Case(
        id="disagree",
        events=[
            _event("consensus", "Synced slot: 1", time="2026-10-08T06:14:00Z", client="lighthouse"),
            _event("execution", "no slot here", time="2026-10-08T06:14:02Z", client="geth"),
            _event("consensus", "Synced slot: 2", time="2026-10-08T06:14:04Z", client="lighthouse"),
        ],
    )
    text = render_timeline(case)
    blank = next(line for line in text.splitlines() if "no slot here" in line)
    assert "slot=" not in blank
    assert "slot=1" in text
    assert "slot=2" in text


def test_the_timeline_names_the_last_healthy_slot_and_the_first_bad_slot() -> None:
    case = Case(
        id="boundary",
        events=[
            _event("execution", "INFO Starting Geth on Ethereum mainnet version", time="2026-10-08T06:00:00Z", client="geth"),
            _event(
                "validator",
                "Successfully published attestation slot: 4",
                time="2026-10-08T06:12:00Z",
                client="lighthouse",
            ),
            _event("kubernetes", "OOMKilled: memory", time="2026-10-08T06:13:54Z", failure_class="oom-killed"),
            _event(
                "consensus",
                "request to execution client timed out slot: 9",
                time="2026-10-08T06:13:58Z",
                client="lighthouse",
            ),
            _event(
                "consensus",
                "Missed attestation slot: 10",
                time="2026-10-08T06:14:00Z",
                client="lighthouse",
            ),
            _event(
                "validator",
                "Successfully published attestation slot: 11",
                time="2026-10-08T06:14:12Z",
                client="lighthouse",
            ),
        ],
    )
    lines = render_timeline(case).splitlines()
    assert lines[1] == "last healthy slot 11"
    assert lines[2] == "first bad slot 9"
    assert any("Starting Geth" in line and line.split()[0] == "2026-10-08T06:00:00Z" for line in lines)


def test_a_case_without_duty_lines_says_the_boundary_is_unknown() -> None:
    case = Case(
        id="quiet",
        events=[
            _event("execution", "INFO Starting Geth version", time="2026-10-08T06:00:00Z", client="geth"),
            _event("execution", "Chain head updated", time="2026-10-08T06:12:00Z", client="geth"),
        ],
    )
    text = render_timeline(case)
    assert "boundary unknown" in text
    assert "last healthy slot" not in text
    assert "Starting Geth" in text


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
    assert "last healthy slot 10274311\n" in stdout
    assert "first bad slot 10274310\n" in stdout
    assert "Starting Geth" in stdout
    assert "unclassified" in stdout
    assert "hunter2" not in stdout
    assert "[redacted]" in stdout
    assert "slot=10274304 epoch=321072 block=0xabababababababab" in stdout
    assert "slot=10274310 inferred" in stdout
    rows = [line for line in stdout.splitlines()[1:] if not line.startswith("clock gap ")]
    slot_rows = [line for line in rows if "slot=10274310" in line]
    assert slot_rows
    assert rows.index(slot_rows[0]) + len(slot_rows) - 1 == rows.index(slot_rows[-1])
    printed = next(line for line in rows if "Missed attestation" in line and "consensus" in line)
    assert "slot=10274310" in printed
    assert "inferred" not in printed
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
    assert "slot=10274310 inferred" in stdout
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
