import contextlib
import io
import os
import re
import shutil
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from chainlog_ai.case import Case, Event, load_case
from chainlog_ai.cli import _cmd_why, main
from chainlog_ai.timeline import render_timeline
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


def test_the_trigger_is_the_earliest_class_that_explains_the_later_lines() -> None:
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
    assert lines[1] == "trigger: cluster failed with node-not-ready at 2026-10-08T06:13:42Z."
    assert "2026-10-08T06:13:42Z" in lines[2]
    assert "node-not-ready" in lines[2]
    assert "NodeNotReady: node down" in lines[2]
    assert "symptom: consensus failed with missed-attestation at 2026-10-08T06:10:00Z." in text
    assert "Missed attestation slot: 1" in text
    assert not any(line.startswith("trigger:") and "missed-attestation" in line for line in lines)
    assert "leveldb: closed" not in text
    assert lines[-1] == "window: 2026-10-08T06:10:00Z .. 2026-10-08T06:13:42Z"


def test_disk_pressure_before_a_database_error_is_a_contributor() -> None:
    case = Case(
        id="disk",
        events=[
            _event(
                "kubernetes",
                "NodeHasDiskPressure: slow disk",
                time="2026-10-08T06:13:40Z",
                failure_class="disk-pressure",
                severity="warning",
            ),
            _event(
                "execution",
                'err="leveldb: closed"',
                time="2026-10-08T06:13:58Z",
                client="geth",
                failure_class="database-closed",
                severity="critical",
            ),
            _event(
                "consensus",
                "Missed attestation slot: 10",
                time="2026-10-08T06:14:00Z",
                client="lighthouse",
                failure_class="missed-attestation",
                severity="warning",
            ),
            _event(
                "kubernetes",
                "NodeHasDiskPressure: after",
                time="2026-10-08T06:14:30Z",
                failure_class="disk-pressure",
                severity="warning",
            ),
        ],
    )
    text, cited = explain(case)
    assert cited
    assert "trigger: execution failed with database-closed at 2026-10-08T06:13:58Z." in text
    assert "contributor: cluster failed with disk-pressure at 2026-10-08T06:13:40Z." in text
    assert "symptom: consensus failed with missed-attestation at 2026-10-08T06:14:00Z." in text
    assert "after" not in text
    assert "window: 2026-10-08T06:13:40Z .. 2026-10-08T06:14:00Z" in text


def test_a_warning_is_the_trigger_when_nothing_explains_it_sooner() -> None:
    case = Case(
        id="warn",
        events=[
            _event(
                "execution",
                "Chain head updated",
                time="2026-10-08T06:12:00Z",
                client="geth",
            ),
            _event(
                "consensus",
                "request to execution client timed out",
                time="2026-10-08T06:14:00Z",
                client="lighthouse",
                failure_class="engine-timeout",
                severity="warning",
            )
        ],
    )
    text, cited = explain(case)
    assert cited
    assert "trigger: consensus failed with engine-timeout at 2026-10-08T06:14:00Z." in text


def test_a_slashing_risk_is_named_critical_before_the_trigger() -> None:
    case = Case(
        id="slash",
        events=[
            _event(
                "execution",
                'err="leveldb: closed"',
                time="2026-10-08T06:13:58Z",
                client="geth",
                failure_class="database-closed",
                severity="critical",
            ),
            _event(
                "validator",
                "Doppelganger(s) detected indices [42]",
                time="2026-10-08T06:14:20.200Z",
                client="lighthouse",
                failure_class="doppelganger",
                severity="critical",
            ),
            _event(
                "validator",
                "Slashable attestation prevented slot: 10274311",
                time="2026-10-08T06:14:20.500Z",
                client="lighthouse",
                failure_class="slashable",
                severity="critical",
            ),
        ],
    )
    text, cited = explain(case)
    assert cited
    lines = text.splitlines()
    assert lines[1] == "critical: validator failed with doppelganger at 2026-10-08T06:14:20.200Z."
    assert lines[3] == "critical: validator failed with slashable at 2026-10-08T06:14:20.500Z."
    assert lines[5] == "trigger: execution failed with database-closed at 2026-10-08T06:13:58Z."
    assert "window: 2026-10-08T06:13:58Z .. 2026-10-08T06:14:20.500Z" in text
    timeline = render_timeline(case)
    timed = [line for line in timeline.splitlines() if line.startswith("2026-")]
    assert "database-closed" in timed[0]
    assert "doppelganger" in timed[1]
    assert "slashable" in timed[2]
    colored, _cited = explain(case, color=True)
    assert "\033[31mcritical: \033[0m" in colored
    assert "\033[31mdoppelganger\033[0m" in colored


def test_a_window_without_slashing_risk_omits_the_critical_block() -> None:
    case = Case(
        id="quiet",
        events=[
            _event(
                "execution",
                'err="leveldb: closed"',
                time="2026-10-08T06:13:58Z",
                client="geth",
                failure_class="database-closed",
                severity="critical",
            )
        ],
    )
    text, cited = explain(case)
    assert cited
    assert "critical:" not in text
    assert text.splitlines()[1].startswith("trigger:")


def test_a_slashable_duty_alone_is_the_critical_block() -> None:
    case = Case(
        id="only-slash",
        events=[
            _event(
                "validator",
                "Slashable attestation prevented",
                time="2026-10-08T06:14:20Z",
                client="lighthouse",
                failure_class="slashable",
                severity="critical",
            )
        ],
    )
    text, cited = explain(case)
    assert cited
    assert "critical: validator failed with slashable at 2026-10-08T06:14:20Z." in text
    assert "trigger:" not in text
    assert "evidence is not enough" not in text


def test_a_miss_alone_is_not_the_trigger() -> None:
    case = Case(
        id="miss",
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
    assert not cited
    assert "trigger:" not in text
    assert "symptom:" not in text
    assert "evidence is not enough to state a cause" in text
    assert "missing a class that explains the failure" in text
    assert "Missed attestation slot: 4" in text


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
    assert "trigger: cluster failed with killed-externally." in text
    assert "window: unknown" in text
    assert "Killing: Stopping container geth" in text


def test_a_terminal_colors_the_cause_and_the_cited_row() -> None:
    case = Case(
        id="color",
        events=[
            _event(
                "kubernetes",
                "mount failed password=[redacted]",
                time="2026-10-08T06:13:48Z",
                failure_class="volume-mount-failed",
                severity="critical",
            )
        ],
    )
    text, cited = explain(case, color=True, width=200)
    assert cited
    assert "\033[1mtrigger: \033[0m" in text
    assert "\033[34mcluster\033[0m" in text
    assert "\033[31mvolume-mount-failed\033[0m" in text
    assert "\033[34mkubernetes\033[0m" in text
    assert "password=\033[1;31m[redacted]\033[0m" in text
    plain = re.sub(r"\033\[[0-9;]*m", "", text)
    assert "trigger: cluster failed with volume-mount-failed at 2026-10-08T06:13:48Z." in plain
    assert "password=[redacted]" in plain
    assert "\033" not in explain(case)[0]


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_why_colors_a_terminal(home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    log = tmp_path / "geth.log"
    log.write_text(
        "INFO Starting Geth on Ethereum mainnet\n"
        'ERROR [10-08|06:13:58.883] Failed to persist ancient block err="leveldb: closed"\n',
        encoding="utf-8",
    )
    code, stdout, stderr = run("ingest", "--case", "colored", "--execution", str(log))
    assert code == 0, stderr
    assert "\033" not in stdout
    buf = _Tty()
    monkeypatch.setattr(sys, "stdout", buf)
    monkeypatch.setattr(shutil, "get_terminal_size", lambda fallback=(80, 24): os.terminal_size((80, 24)))
    code = _cmd_why(Namespace(case="colored", inputs=[]))
    assert code == 0
    text = buf.getvalue()
    assert "\033[36mexecution\033[0m" in text
    assert "\033[31mdatabase-closed\033[0m" in text
    assert "leveldb: closed" in re.sub(r"\033\[[0-9;]*m", "", text)


def test_an_engine_timeout_without_execution_names_the_missing_source() -> None:
    case = Case(
        id="engine",
        events=[
            _event(
                "consensus",
                "request to execution client timed out slot: 10",
                time="2026-10-08T06:13:58Z",
                client="lighthouse",
                failure_class="engine-timeout",
                severity="warning",
            )
        ],
    )
    text, cited = explain(case)
    assert not cited
    assert "evidence is not enough to state a cause" in text
    assert "missing execution" in text
    assert "trigger:" not in text
    assert "request to execution client timed out" in text
    assert "probably" not in text


def test_a_window_that_misses_the_first_bad_slot_stays_a_gap() -> None:
    case = Case(
        id="narrow",
        events=[
            _event(
                "execution",
                "Chain head updated",
                time="2026-10-08T06:00:00Z",
                client="geth",
            ),
            _event(
                "consensus",
                "request to execution client timed out slot: 10",
                time="2026-10-08T06:13:58Z",
                client="lighthouse",
                failure_class="engine-timeout",
                severity="warning",
            ),
            _event(
                "execution",
                "Engine API newPayload context deadline exceeded slot: 10",
                time="2026-10-08T06:14:01Z",
                client="geth",
                failure_class="engine-timeout",
                severity="warning",
            ),
        ],
    )
    text, cited = explain(case, until="2026-10-08T06:00:00Z")
    assert not cited
    assert "window does not cover slot 10" in text
    assert "trigger:" not in text
    assert "Chain head updated" in text
    assert "newPayload" not in text
    whole, whole_cited = explain(case)
    assert whole_cited
    assert "trigger:" in whole
    assert "window does not cover" not in whole


def test_unclassified_lines_are_not_enough() -> None:
    case = Case(
        id="thin",
        events=[_event("execution", "database closed", time="2026-10-08T06:14:00Z", client="geth")],
    )
    text, cited = explain(case)
    assert not cited
    assert text.splitlines()[0] == "case thin"
    assert "evidence is not enough to state a cause" in text
    assert "missing a classified failure" in text
    assert "database closed" in text
    assert "trigger:" not in text


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
    cause = next(line for line in stdout.splitlines() if line.startswith("trigger: "))
    assert cause.startswith("trigger: execution failed with database-closed at ")
    when = cause.removeprefix("trigger: execution failed with database-closed at ").removesuffix(".")
    row = next(line for line in stdout.splitlines() if "leveldb: closed" in line)
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
    assert stdout.splitlines()[1] == "critical: validator failed with doppelganger at 2026-10-08T06:14:20.200Z."
    assert "critical: validator failed with slashable at 2026-10-08T06:14:20.500Z." in stdout
    assert "trigger: cluster failed with node-not-ready at 2026-10-08T06:13:42Z." in stdout
    assert "contributor: cluster failed with cpu-pressure at 2026-10-08T06:10:00Z." in stdout
    assert "contributor: cluster failed with disk-pressure at 2026-10-08T06:13:40Z." in stdout
    assert "contributor: cluster failed with memory-pressure at 2026-10-08T06:13:41Z." in stdout
    assert "symptom: consensus failed with missed-attestation at 2026-10-08T06:14:00.010Z." in stdout
    assert "symptom: cluster failed with probe-failed at 2026-10-08T06:13:50Z." in stdout
    assert "symptom: cluster failed with crash-loop at 2026-10-08T06:14:05Z." in stdout
    assert not any(line.startswith("trigger:") and "missed-" in line for line in stdout.splitlines())
    assert "window: 2026-10-08T06:10:00Z .. " in stdout
    assert "NodeNotReady: Node ethereum-1 status is now: NodeNotReady" in stdout
    assert "hunter2" not in stdout
