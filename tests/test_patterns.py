import contextlib
import io
import json
import os
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


def _event(source: str, client: str | None, excerpt: str, time: str, index: int) -> Event:
    return Event(
        source=source,
        client=client,
        path=f"/tmp/{source}.log",
        byte_start=index,
        byte_end=index + 1,
        excerpt=excerpt,
        time=time,
    )


def _save(case_id: str, events: list[Event]) -> None:
    save_case(Case(id=case_id, events=events))


def test_a_case_with_no_earlier_history_is_the_first_time(home: Path) -> None:
    _save("only", [_event("execution", "geth", "leveldb: closed", "2026-10-08T06:13:58Z", 0)])
    code, stdout, stderr = run("patterns", "--case", "only")
    assert code == 0, stderr
    assert stdout == "case only\nfirst time on this machine\n"
    assert "leveldb" not in stdout


def test_patterns_lists_an_earlier_case_with_the_same_order(home: Path) -> None:
    shared = [
        ("leveldb: closed", "2026-10-08T06:13:58Z"),
        ("Engine API newPayload context deadline exceeded", "2026-10-08T06:14:01Z"),
    ]
    _save("old", [_event("execution", "geth", text, "2026-10-01T" + stamp[11:], index) for index, (text, stamp) in enumerate(shared)])
    _save("new", [_event("execution", "geth", text, stamp, index) for index, (text, stamp) in enumerate(shared)])
    code, stdout, stderr = run("patterns", "--case", "new")
    assert code == 0, stderr
    assert stdout == "case new\n1 earlier case\nold\n"


def test_an_extra_class_keeps_a_similar_order(home: Path) -> None:
    _save(
        "old",
        [
            _event("execution", "geth", "leveldb: closed", "2026-10-01T06:13:58Z", 0),
            _event("execution", "geth", "Engine API newPayload context deadline exceeded", "2026-10-01T06:14:01Z", 1),
        ],
    )
    _save(
        "new",
        [
            _event("execution", "geth", "leveldb: closed", "2026-10-08T06:13:58Z", 0),
            _event("execution", "geth", "Invalid JWT token", "2026-10-08T06:13:59Z", 1),
            _event("execution", "geth", "Engine API newPayload context deadline exceeded", "2026-10-08T06:14:01Z", 2),
        ],
    )
    code, stdout, stderr = run("patterns", "--case", "new")
    assert code == 0, stderr
    assert stdout == "case new\n1 earlier case\nold\n"


def test_a_reversed_order_does_not_match(home: Path) -> None:
    _save(
        "old",
        [
            _event("execution", "geth", "Engine API newPayload context deadline exceeded", "2026-10-01T06:13:58Z", 0),
            _event("execution", "geth", "leveldb: closed", "2026-10-01T06:14:01Z", 1),
        ],
    )
    _save(
        "new",
        [
            _event("execution", "geth", "leveldb: closed", "2026-10-08T06:13:58Z", 0),
            _event("execution", "geth", "Engine API newPayload context deadline exceeded", "2026-10-08T06:14:01Z", 1),
        ],
    )
    code, stdout, stderr = run("patterns", "--case", "new")
    assert code == 0, stderr
    assert stdout == "case new\n0 earlier cases\n"


def test_a_different_client_does_not_match(home: Path) -> None:
    _save(
        "old",
        [_event("consensus", "lighthouse", "request to execution client timed out", "2026-10-01T06:13:58Z", 0)],
    )
    _save(
        "new",
        [_event("execution", "geth", "Engine API newPayload context deadline exceeded", "2026-10-08T06:14:01Z", 0)],
    )
    code, stdout, stderr = run("patterns", "--case", "new")
    assert code == 0, stderr
    assert stdout == "case new\n0 earlier cases\n"


def test_a_different_version_does_not_match(home: Path) -> None:
    _save(
        "old",
        [
            _event("execution", "geth", "version: Geth/v1.13.15", "2026-10-01T06:00:00Z", 0),
            _event("execution", "geth", "leveldb: closed", "2026-10-01T06:13:58Z", 1),
        ],
    )
    _save(
        "new",
        [
            _event("execution", "geth", "version: Geth/v1.14.0", "2026-10-08T06:00:00Z", 0),
            _event("execution", "geth", "leveldb: closed", "2026-10-08T06:13:58Z", 1),
        ],
    )
    code, stdout, stderr = run("patterns", "--case", "new")
    assert code == 0, stderr
    assert stdout == "case new\n0 earlier cases\n"


def test_the_same_version_matches(home: Path) -> None:
    def events(day: str) -> list[Event]:
        return [
            _event("execution", "geth", "version: Geth/v1.13.15", f"2026-10-{day}T06:00:00Z", 0),
            _event("execution", "geth", "leveldb: closed", f"2026-10-{day}T06:13:58Z", 1),
        ]

    _save("old", events("01"))
    _save("new", events("08"))
    code, stdout, stderr = run("patterns", "--case", "new")
    assert code == 0, stderr
    assert stdout == "case new\n1 earlier case\nold\n"


def test_a_later_case_is_not_an_earlier_match(home: Path) -> None:
    _save("old", [_event("execution", "geth", "leveldb: closed", "2026-10-01T06:13:58Z", 0)])
    _save("new", [_event("execution", "geth", "leveldb: closed", "2026-10-08T06:13:58Z", 0)])
    code, stdout, stderr = run("patterns", "--case", "old")
    assert code == 0, stderr
    assert stdout == "case old\nfirst time on this machine\n"


def test_patterns_names_every_earlier_match(home: Path) -> None:
    for case_id, day in (("bbb", "02"), ("aaa", "01"), ("now", "08")):
        _save(case_id, [_event("execution", "geth", "leveldb: closed", f"2026-10-{day}T06:13:58Z", 0)])
    code, stdout, stderr = run("patterns", "--case", "now")
    assert code == 0, stderr
    assert stdout == "case now\n2 earlier cases\naaa\nbbb\n"


def test_an_unknown_case_is_a_usage_error(home: Path) -> None:
    code, stdout, stderr = run("patterns", "--case", "missing")
    assert code == 3
    assert stdout == ""
    assert "no such case: missing" in stderr


def test_the_sample_log_matches_an_earlier_copy(home: Path) -> None:
    root = Path(__file__).parents[1] / "examples"
    code, _stdout, stderr = run("ingest", "--case", "earlier", "--execution", str(root / "geth.log"))
    assert code == 0, stderr
    code, _stdout, stderr = run("ingest", "--case", "later", "--execution", str(root / "geth.log"))
    assert code == 0, stderr
    os.utime(home / "cases" / "earlier" / "case.json", (1, 1))
    os.utime(home / "cases" / "later" / "case.json", (2, 2))
    code, stdout, stderr = run("patterns", "--case", "later")
    assert code == 0, stderr
    assert stdout == "case later\n1 earlier case\nearlier\n"
    code, stdout, stderr = run("patterns", "--case", "earlier")
    assert code == 0, stderr
    assert stdout == "case earlier\nfirst time on this machine\n"


def test_the_export_leaves_out_the_log_line_and_the_host(home: Path, tmp_path: Path) -> None:
    root = Path(__file__).parents[1] / "examples"
    log = tmp_path / "db-host.internal" / "geth.log"
    log.parent.mkdir()
    log.write_bytes((root / "geth.log").read_bytes())
    code, _stdout, stderr = run("ingest", "--case", "copied", "--execution", str(log))
    assert code == 0, stderr
    destination = tmp_path / "copied.signature.json"
    code, stdout, stderr = run("patterns", "--case", "copied", "--export", str(destination))
    assert code == 0, stderr
    assert stdout == f"case copied\nsignature {destination}\n"
    text = destination.read_text(encoding="utf-8")
    assert "Failed to persist ancient block" not in text
    assert "db-host.internal" not in text
    payload = json.loads(text)
    assert list(payload) == ["streams"]
    geth = next(item for item in payload["streams"] if item["client"] == "geth")
    assert geth["version"] == "unknown"
    assert geth["classes"][0] == "database-closed"
    assert "engine-timeout" in geth["classes"]
    assert list(geth) == ["client", "version", "classes"]


def test_a_signature_matches_a_local_case(home: Path, tmp_path: Path) -> None:
    _save("local", [_event("execution", "geth", "leveldb: closed", "2026-10-08T06:13:58Z", 0)])
    signature = tmp_path / "remote.signature.json"
    signature.write_text(
        json.dumps(
            {"streams": [{"client": "geth", "version": "unknown", "classes": ["database-closed"]}]}
        )
        + "\n",
        encoding="utf-8",
    )
    code, stdout, stderr = run("patterns", "--signature", str(signature))
    assert code == 0, stderr
    assert stdout == "match\nlocal\n"


def test_a_signature_with_no_local_match_says_no_match(home: Path, tmp_path: Path) -> None:
    _save(
        "local",
        [
            _event("execution", "geth", "leveldb: closed", "2026-10-08T06:13:58Z", 0),
            _event("execution", "geth", "Engine API newPayload context deadline exceeded", "2026-10-08T06:14:01Z", 1),
        ],
    )
    signature = tmp_path / "remote.signature.json"
    signature.write_text(
        json.dumps(
            {
                "streams": [
                    {
                        "client": "geth",
                        "version": "unknown",
                        "classes": ["engine-timeout", "database-closed"],
                    }
                ]
            }
        )
        + "\n",
        encoding="utf-8",
    )
    code, stdout, stderr = run("patterns", "--signature", str(signature))
    assert code == 0, stderr
    assert stdout == "no match\n"


def test_a_bad_signature_is_a_usage_error(home: Path, tmp_path: Path) -> None:
    signature = tmp_path / "broken.signature.json"
    signature.write_text("{", encoding="utf-8")
    code, stdout, stderr = run("patterns", "--signature", str(signature))
    assert code == 3
    assert stdout == ""
    assert f"cannot read signature {signature}" in stderr


def test_patterns_needs_a_case_or_a_signature(home: Path) -> None:
    code, stdout, stderr = run("patterns")
    assert code == 3
    assert stdout == ""
    assert "patterns needs a case id or a signature file" in stderr
    code, stdout, stderr = run("patterns", "--case", "only", "--signature", "remote.json")
    assert code == 3
    assert "patterns takes a signature or a case, not both" in stderr
