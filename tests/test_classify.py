import contextlib
import io
import json
from pathlib import Path

import pytest

from chainlog_ai.case import load_case
from chainlog_ai.classify import CatalogError, default_catalog, load_catalog
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


def test_default_catalog_rules_have_a_source() -> None:
    rules = default_catalog()
    assert rules
    assert all(rule.document.startswith("https://") for rule in rules)


def test_catalog_without_a_source_fails_to_load(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    path.write_text(
        json.dumps(
            [
                {
                    "id": "bare",
                    "client": "geth",
                    "match": "leveldb: closed",
                    "class": "database-closed",
                }
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(CatalogError, match="has no source"):
        load_catalog(path.read_text(encoding="utf-8"))


def test_classify_counts_matches_and_keeps_unclassified_lines(home: Path) -> None:
    root = Path(__file__).parents[1] / "examples"
    code, stdout, stderr = run(
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
    code, stdout, stderr = run("classify", "--case", "incident")
    assert code == 0, stderr
    assert "database-closed 1" in stdout
    assert "engine-timeout 2" in stdout
    assert "jwt-rejected 1" in stdout
    assert "missed-attestation 1" in stdout
    assert "oom-killed 1" in stdout
    assert "probe-failed 1" in stdout
    assert "unclassified " in stdout
    case = load_case("incident")
    closed = next(event for event in case.events if "leveldb: closed" in event.excerpt)
    assert closed.failure_class == "database-closed"
    assert closed.rule == "geth-database-closed"
    kept = next(event for event in case.events if "Successfully published attestation" in event.excerpt)
    assert kept.failure_class is None
    assert kept.rule is None
    assert "Successfully published attestation" in kept.excerpt


def test_unknown_case_and_bad_catalog_are_usage_errors(home: Path, tmp_path: Path) -> None:
    code, _stdout, stderr = run("classify", "--case", "missing")
    assert code == 3
    assert "no such case" in stderr

    path = tmp_path / "geth.log"
    path.write_text("INFO Starting Geth\n", encoding="utf-8")
    code, _stdout, stderr = run("ingest", "--case", "tiny", "--execution", str(path))
    assert code == 0
    catalog = tmp_path / "catalog.json"
    catalog.write_text("[]", encoding="utf-8")
    broken = tmp_path / "broken.json"
    broken.write_text(
        json.dumps([{"id": "bare", "client": "geth", "match": "Geth", "class": "started"}]),
        encoding="utf-8",
    )
    code, stdout, stderr = run("classify", "--case", "tiny", "--catalog", str(broken))
    assert code == 3
    assert "has no source" in stderr
    assert load_case("tiny").events[0].failure_class is None

    code, stdout, stderr = run("classify", "--case", "tiny", "--catalog", str(catalog))
    assert code == 0, stderr
    assert "unclassified 1" in stdout
