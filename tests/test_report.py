import contextlib
import io
import json
from pathlib import Path

import pytest

from chainlog_ai.case import case_file, load_case
from chainlog_ai.cli import main
from chainlog_ai.timeline import render_timeline

_TOP = {"case", "critical", "cause", "boundary", "citations", "pattern"}
_CAUSE = {"stated", "notice", "gaps", "trigger", "contributors", "symptoms", "window"}
_CITE = {"role", "time", "source", "client", "class", "severity", "excerpt", "path", "slot", "epoch", "block"}
_BOUNDARY = {"last_healthy_slot", "first_bad_slot"}


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


def _ingest_incident() -> None:
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


def _load(case_id: str) -> tuple[str, dict[str, object]]:
    directory = case_file(case_id).parent
    markdown = (directory / "report.md").read_text(encoding="utf-8")
    payload = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    return markdown, payload


def _assert_shape(payload: dict[str, object]) -> None:
    assert set(payload) == _TOP
    cause = payload["cause"]
    assert isinstance(cause, dict)
    assert set(cause) == _CAUSE
    boundary = payload["boundary"]
    assert isinstance(boundary, dict)
    assert set(boundary) == _BOUNDARY
    for item in payload["critical"]:
        assert isinstance(item, dict)
        assert set(item) == _CITE
    for item in payload["citations"]:
        assert isinstance(item, dict)
        assert set(item) == _CITE
    trigger = cause["trigger"]
    if trigger is not None:
        assert isinstance(trigger, dict)
        assert set(trigger) == _CITE
    for key in ("contributors", "symptoms", "gaps"):
        assert isinstance(cause[key], list)
    assert isinstance(payload["pattern"], str)


def test_the_incident_report_leads_with_slashing_and_cites_the_cause(home: Path) -> None:
    _ingest_incident()
    code, stdout, stderr = run("report", "--case", "incident")
    assert code == 0, stderr
    assert stdout == "case incident\nreport.md\nreport.json\n"
    markdown, payload = _load("incident")
    _assert_shape(payload)
    assert payload["case"] == "incident"
    assert payload["cause"]["stated"] is True
    assert markdown.index("## Slashing risk") < markdown.index("## Cause")
    assert "doppelganger" in markdown.split("## Cause", 1)[0]
    assert "slashable" in markdown.split("## Cause", 1)[0]
    trigger = payload["cause"]["trigger"]
    assert isinstance(trigger, dict)
    assert trigger["class"] == "node-not-ready"
    assert "trigger: cluster failed with node-not-ready" in markdown
    boundary = render_timeline(load_case("incident")).splitlines()
    assert boundary[1] in markdown
    assert boundary[2] in markdown
    assert payload["boundary"]["last_healthy_slot"] == int(boundary[1].removeprefix("last healthy slot "))
    assert payload["boundary"]["first_bad_slot"] == int(boundary[2].removeprefix("first bad slot "))
    roles = [item["role"] for item in payload["citations"]]
    assert roles[0] == "critical"
    assert "trigger" in roles
    assert "first time on this machine" in payload["pattern"]
    assert "first time on this machine" in markdown
    assert "hunter2" not in markdown
    assert "hunter2" not in json.dumps(payload)
    path = trigger["path"]
    assert isinstance(path, str) and path
    assert path in markdown


def test_no_paths_drops_local_file_paths(home: Path, tmp_path: Path) -> None:
    _ingest_incident()
    code, _stdout, stderr = run("report", "--case", "incident", "--no-paths")
    assert code == 0, stderr
    markdown, payload = _load("incident")
    _assert_shape(payload)
    stored = {event.path for event in load_case("incident").events}
    blob = markdown + json.dumps(payload)
    for path in stored:
        assert path not in blob
    assert "path:" not in markdown
    trigger = payload["cause"]["trigger"]
    assert isinstance(trigger, dict)
    assert trigger["path"] is None
    assert trigger["excerpt"]


def test_a_thin_case_still_writes_the_same_fields(home: Path) -> None:
    root = Path(__file__).parents[1] / "examples"
    code, _stdout, stderr = run("ingest", "--case", "thin", "--execution", str(root / "geth.log"))
    assert code == 0, stderr
    code, stdout, stderr = run("report", "--case", "thin")
    assert code == 1, stderr
    assert stdout == "case thin\nreport.md\nreport.json\n"
    markdown, payload = _load("thin")
    _assert_shape(payload)
    assert payload["cause"]["stated"] is False
    assert payload["cause"]["notice"] == "evidence is not enough to state a cause"
    assert "missing consensus" in payload["cause"]["gaps"]
    assert payload["cause"]["trigger"] is None
    assert payload["critical"] == []
    assert payload["citations"] == []
    assert payload["boundary"] == {"last_healthy_slot": None, "first_bad_slot": None}
    assert "## Slashing risk" not in markdown
    assert "evidence is not enough to state a cause" in markdown
    assert "boundary unknown" in markdown
    assert "\n- none\n" in markdown or markdown.split("## Citations", 1)[1].startswith("\n\nnone\n")
    _ingest_incident()
    code, _stdout, stderr = run("report", "--case", "incident")
    assert code == 0, stderr
    _stated_md, stated = _load("incident")
    _thin_md, thin = _load("thin")
    assert set(stated) == set(thin)
    assert set(stated["cause"]) == set(thin["cause"])
    assert set(stated["boundary"]) == set(thin["boundary"])


def test_the_pattern_note_names_an_earlier_case(home: Path) -> None:
    root = Path(__file__).parents[1] / "examples" / "geth.log"
    code, _stdout, stderr = run(
        "ingest",
        "--case",
        "earlier",
        "--execution",
        str(root),
        "--execution-client",
        "geth",
    )
    assert code == 0, stderr
    log = Path(__file__).parents[1] / "examples" / "geth.log"
    code, _stdout, stderr = run("ingest", "--case", "later", "--execution", str(log), "--execution-client", "geth")
    assert code == 0, stderr
    code, _stdout, stderr = run("report", "--case", "later")
    assert code == 1, stderr
    markdown, payload = _load("later")
    assert "1 earlier case" in payload["pattern"]
    assert "earlier" in payload["pattern"].splitlines()
    assert "1 earlier case" in markdown
    assert "version geth unknown" in markdown


def test_an_unknown_case_is_a_usage_error(home: Path) -> None:
    code, stdout, stderr = run("report", "--case", "missing")
    assert code == 3
    assert stdout == ""
    assert "missing" in stderr
