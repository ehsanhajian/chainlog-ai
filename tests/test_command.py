import io
import shutil
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from chainlog_ai.case import case_file
from chainlog_ai.cli import main

_EXAMPLES = Path(__file__).parents[1] / "examples"


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
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(list(argv))
    return code, stdout.getvalue(), stderr.getvalue()


def test_files_print_the_cause_and_write_the_report(home: Path) -> None:
    code, stdout, stderr = run(
        str(_EXAMPLES / "geth.log"),
        str(_EXAMPLES / "beacon.log"),
        str(_EXAMPLES / "validator.log"),
        str(_EXAMPLES / "mev-boost.log"),
        str(_EXAMPLES / "events.json"),
    )
    assert code == 0, stderr
    lines = stdout.splitlines()
    assert lines[0] == "critical: validator failed with doppelganger at 2026-10-08T06:14:20.200Z."
    assert "critical: validator failed with slashable at 2026-10-08T06:14:20.500Z." in stdout
    assert "trigger: cluster failed with node-not-ready at 2026-10-08T06:13:42Z." in stdout
    assert "contributor: cluster failed with cpu-pressure at 2026-10-08T06:10:00Z." in stdout
    assert "symptom: consensus failed with missed-attestation at 2026-10-08T06:14:00.010Z." in stdout
    assert lines[-2].startswith("case ")
    assert lines[-1] == "report.md"
    assert "hunter2" not in stdout
    case_id = lines[-2].removeprefix("case ")
    report = case_file(case_id).parent / "report.md"
    assert report.is_file()
    assert "node-not-ready" in report.read_text(encoding="utf-8")


def test_a_thin_log_still_writes_the_report(home: Path) -> None:
    code, stdout, stderr = run(str(_EXAMPLES / "geth.log"))
    assert code == 1, stderr
    assert "evidence is not enough to state a cause" in stdout
    assert stdout.splitlines()[-1] == "report.md"
    case_id = stdout.splitlines()[-2].removeprefix("case ")
    assert (case_file(case_id).parent / "report.md").is_file()


def test_an_unrecognized_file_stops_the_command(home: Path, tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("nothing to see here\n", encoding="utf-8")
    code, stdout, stderr = run(str(path))
    assert code == 3
    assert stdout == ""
    assert path.name in stderr
    assert "cannot tell what" in stderr


def test_a_directory_skips_a_file_it_cannot_recognize(home: Path, tmp_path: Path) -> None:
    folder = tmp_path / "incident"
    folder.mkdir()
    shutil.copy(_EXAMPLES / "geth.log", folder / "geth.log")
    (folder / "notes.txt").write_text("operator notes\n", encoding="utf-8")
    code, stdout, stderr = run(str(folder))
    assert code == 1, stderr
    assert "skipped notes.txt" in stderr
    assert "evidence is not enough to state a cause" in stdout
    assert stdout.splitlines()[-1] == "report.md"


def test_an_empty_directory_fails(home: Path, tmp_path: Path) -> None:
    folder = tmp_path / "empty"
    folder.mkdir()
    code, stdout, stderr = run(str(folder))
    assert code == 3
    assert stdout == ""
    assert "directory is empty" in stderr


def test_a_directory_of_unrecognized_files_fails(home: Path, tmp_path: Path) -> None:
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "a.txt").write_text("hello\n", encoding="utf-8")
    code, stdout, stderr = run(str(folder))
    assert code == 3
    assert stdout == ""
    assert "skipped a.txt" in stderr
    assert "no recognized file" in stderr


def test_ingest_still_prints_only_the_case_id(home: Path) -> None:
    code, stdout, stderr = run("ingest", "--execution", str(_EXAMPLES / "geth.log"))
    assert code == 0, stderr
    assert stdout.startswith("case ")
    assert "report.md" not in stdout
    assert "trigger:" not in stdout


def test_help_opens_with_the_bare_command() -> None:
    code, stdout, stderr = run("--help")
    assert code == 0
    assert stderr == ""
    assert "chainlog-ai examples/geth.log examples/beacon.log examples/validator.log examples/mev-boost.log examples/events.json" in stdout
    body = stdout.split("\n\n", 1)[1]
    assert body.index("examples/geth.log") < body.index("The commands below")
