import contextlib
import io
from pathlib import Path

import pytest

from chainlog_ai.case import Case, Event, save_case
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


def _event(
    source: str,
    client: str | None,
    excerpt: str,
    time: str,
    index: int,
    *,
    slot: int | None = None,
) -> Event:
    return Event(
        source=source,
        client=client,
        path=f"/tmp/{source}.log",
        byte_start=index,
        byte_end=index + 1,
        excerpt=excerpt,
        time=time,
        slot=slot,
    )


def test_ask_cites_the_cause_and_the_runbook(home: Path) -> None:
    save_case(
        Case(
            id="db",
            events=[
                _event(
                    "execution",
                    "geth",
                    'err="leveldb: closed"',
                    "2026-10-08T06:13:58Z",
                    0,
                )
            ],
        )
    )
    code, stdout, stderr = run("ask", "--case", "db", "why did it fail")
    assert code == 0, stderr
    assert "case db\n" in stdout
    assert "trigger:" in stdout
    assert "database-closed" in stdout
    assert 'err="leveldb: closed"' in stdout
    assert "runbook database-closed: The execution client closed its database." in stdout
    assert "https://github.com/syndtr/goleveldb/blob/master/leveldb/errors.go" in stdout
    assert "model" not in stdout.lower()


def test_ask_says_the_case_does_not_contain_that(home: Path) -> None:
    save_case(
        Case(
            id="db",
            events=[_event("execution", "geth", 'err="leveldb: closed"', "2026-10-08T06:13:58Z", 0)],
        )
    )
    code, stdout, stderr = run("ask", "--case", "db", "what was the weather in paris")
    assert code == 1, stderr
    assert stdout == "case db\nthe case does not contain that\n"
    assert "leveldb" not in stdout


def test_the_slot_before_the_miss_cites_that_slot(home: Path) -> None:
    save_case(
        Case(
            id="slots",
            events=[
                _event(
                    "consensus",
                    "lighthouse",
                    "Attestation included slot: 9",
                    "2026-10-08T06:13:00Z",
                    0,
                    slot=9,
                ),
                _event(
                    "consensus",
                    "lighthouse",
                    "Missed attestation slot: 10",
                    "2026-10-08T06:14:00Z",
                    1,
                    slot=10,
                ),
            ],
        )
    )
    code, stdout, stderr = run("ask", "--case", "slots", "what happened in the slot before the miss?")
    assert code == 0, stderr
    assert "slot=9" in stdout
    assert "Attestation included slot: 9" in stdout
    assert "Missed attestation" not in stdout
    code, stdout, stderr = run("ask", "--case", "slots", "what happened in the slot before the miss on an empty chain")
    assert code == 0, stderr
    assert "slot=9" in stdout


def test_a_miss_with_no_previous_slot_is_not_in_the_case(home: Path) -> None:
    save_case(
        Case(
            id="miss",
            events=[
                _event(
                    "consensus",
                    "lighthouse",
                    "Missed attestation slot: 10",
                    "2026-10-08T06:14:00Z",
                    0,
                    slot=10,
                )
            ],
        )
    )
    code, stdout, stderr = run("ask", "--case", "miss", "what happened in the slot before the miss?")
    assert code == 1, stderr
    assert stdout == "case miss\nthe case does not contain that\n"


def test_a_named_slot_cites_that_row(home: Path) -> None:
    save_case(
        Case(
            id="slots",
            events=[
                _event("consensus", "lighthouse", "Missed attestation slot: 10", "2026-10-08T06:14:00Z", 0, slot=10),
                _event("consensus", "lighthouse", "Synced slot: 11", "2026-10-08T06:14:12Z", 1, slot=11),
            ],
        )
    )
    code, stdout, stderr = run("ask", "--case", "slots", "what happened at slot 10")
    assert code == 0, stderr
    assert "slot=10" in stdout
    assert "Missed attestation slot: 10" in stdout
    assert "slot=11" not in stdout
    assert "runbook missed-attestation: An attestation was missed." in stdout


def test_an_unknown_case_is_a_usage_error(home: Path) -> None:
    code, stdout, stderr = run("ask", "--case", "missing", "why did it fail")
    assert code == 3
    assert stdout == ""
    assert "no such case: missing" in stderr


def test_the_sample_log_answers_why_and_refuses_a_missing_slot(home: Path) -> None:
    root = Path(__file__).parents[1] / "examples"
    code, _stdout, stderr = run("ingest", "--case", "sample", "--execution", str(root / "geth.log"))
    assert code == 0, stderr
    code, stdout, stderr = run("ask", "--case", "sample", "why did it fail")
    assert code == 0, stderr
    assert "database-closed" in stdout
    assert "leveldb: closed" in stdout
    assert "https://github.com/syndtr/goleveldb/blob/master/leveldb/errors.go" in stdout
    code, stdout, stderr = run("ask", "--case", "sample", "what happened in the slot before the miss?")
    assert code == 1, stderr
    assert stdout == "case sample\nthe case does not contain that\n"
