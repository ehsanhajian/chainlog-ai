import contextlib
import io
from pathlib import Path

import pytest

from chainlog_ai.case import Case, Event, load_case
from chainlog_ai.cli import main
from chainlog_ai.why import explain


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
    severity: str | None = None,
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
        severity=severity,
    )


def test_the_cause_cites_the_earliest_critical_timeline_row() -> None:
    case = Case(
        id="rank",
        events=[
            _event(
                "consensus",
                "Missed attestation slot: 1",
                time="2026-10-08T06:10:00Z",
                client="lighthouse",
                failure_class="missed-attestation",
                severity="warning",
            ),
            _event(
                "kubernetes",
                "NodeNotReady: node down",
                time="2026-10-08T06:13:42Z",
                failure_class="node-not-ready",
                severity="critical",
            ),
            _event(
                "execution",
                'err="leveldb: closed"',
                time="2026-10-08T06:13:58.883Z",
                client="geth",
                failure_class="database-closed",
                severity="critical",
            ),
        ],
    )
    text, cited = explain(case)
    assert cited
    lines = text.splitlines()
    assert lines[0] == "case rank"
    assert lines[1] == "cause: cluster failed with node-not-ready at 2026-10-08T06:13:42Z."
    assert lines[2] == "window: 2026-10-08T06:13:42Z .. 2026-10-08T06:13:42Z"
    assert "2026-10-08T06:13:42Z" in lines[3]
    assert "node-not-ready" in lines[3]
    assert "NodeNotReady: node down" in lines[3]
    assert "leveldb: closed" not in text


def test_a_warning_is_the_cause_when_nothing_is_critical() -> None:
    case = Case(
        id="warn",
        events=[
            _event(
                "validator",
                "Missed attestation slot: 4",
                time="2026-10-08T06:14:00Z",
                client="lighthouse",
                failure_class="missed-attestation",
                severity="warning",
            )
        ],
    )
    text, cited = explain(case)
    assert cited
    assert "cause: validator failed with missed-attestation at 2026-10-08T06:14:00Z." in text


def test_an_undated_failure_has_an_unknown_window() -> None:
    case = Case(
        id="undated",
        events=[
            _event(
                "kubernetes-previous",
                "Killing: Stopping container geth",
                time=None,
                failure_class="killed-externally",
                severity="critical",
            )
        ],
    )
    text, cited = explain(case)
    assert cited
    assert "cause: cluster failed with killed-externally." in text
    assert "window: unknown" in text
    assert "Killing: Stopping container geth" in text


def test_unclassified_lines_are_not_enough() -> None:
    case = Case(
        id="thin",
        events=[_event("execution", "database closed", time="2026-10-08T06:14:00Z", client="geth")],
    )
    text, cited = explain(case)
    assert not cited
    assert text.splitlines() == ["case thin", "evidence is not enough to state a cause"]


def test_why_reads_the_same_files_as_ingest(home: Path, tmp_path: Path) -> None:
    log = tmp_path / "geth.log"
    log.write_text(
        "\n".join(
            [
                "INFO Starting Geth on Ethereum mainnet",
                'ERROR [10-08|06:13:58.883] Failed to persist ancient block err="leveldb: closed"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    code, stdout, stderr = run("why", "--execution", str(log))
    assert code == 0, stderr
    cause = next(line for line in stdout.splitlines() if line.startswith("cause: "))
    assert cause.startswith("cause: execution failed with database-closed at ")
    when = cause.removeprefix("cause: execution failed with database-closed at ").removesuffix(".")
    row = stdout.splitlines()[-1]
    assert when in row
    assert "database-closed" in row
    assert "leveldb: closed" in row
    assert f"window: {when} .. {when}" in stdout
    case_id = stdout.splitlines()[0].removeprefix("case ")
    assert load_case(case_id).events[-1].failure_class == "database-closed"


def test_the_sample_incident_names_the_cluster(home: Path) -> None:
    root = Path(__file__).parents[1] / "examples"
    code, stdout, stderr = run(
        "why",
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
    assert stdout.splitlines()[1] == "cause: cluster failed with node-not-ready at 2026-10-08T06:13:42Z."
    assert "window: 2026-10-08T06:13:42Z .. 2026-10-08T06:13:42Z" in stdout
    assert "NodeNotReady: Node ethereum-1 status is now: NodeNotReady" in stdout
    assert "hunter2" not in stdout
