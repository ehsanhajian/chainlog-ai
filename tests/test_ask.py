import contextlib
import io
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from chainlog_ai.ask import _parse_runbook, send_remote
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
    assert "check: Open the execution client log and find the leveldb: closed line." in stdout
    assert "check: Look at the filesystem availability metric for the execution data volume." in stdout
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


def test_a_check_without_a_source_is_left_out() -> None:
    parsed = _parse_runbook(
        [
            {
                "class": "database-closed",
                "text": "The execution client closed its database.",
                "source": "https://example.com/database",
                "checks": [
                    {"text": "Open the execution client log.", "source": "https://example.com/log"},
                    {"text": "Restart the pod."},
                    {"text": "Edit the client flag.", "source": ""},
                ],
            }
        ]
    )
    _sentence, _source, checks = parsed["database-closed"]
    assert checks == (("Open the execution client log.", "https://example.com/log"),)


def test_every_primary_class_has_a_sourced_read_only_check() -> None:
    root = Path(__file__).parents[1] / "src" / "chainlog_ai"
    catalog = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
    classes = {item["class"] for item in catalog}
    parsed = _parse_runbook(json.loads((root / "runbook.json").read_text(encoding="utf-8")))
    forbidden = re.compile(
            r"\brestart (the|a|this|your)\b|\bedit (the|a|this|your)\b|\bdelete\b"
            r"|run a signer|run the signer|sign with|kubectl apply|kubectl delete",
            re.IGNORECASE,
        )
    assert classes <= set(parsed)
    for name in classes:
        _sentence, source, checks = parsed[name]
        assert source
        assert checks
        for text, check_source in checks:
            assert check_source
            assert forbidden.search(text) is None


def test_ask_sends_nothing_unless_this_invocation_passes_remote(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []

    def explode(*_args: object, **_kwargs: object) -> None:
        calls.append(True)
        raise AssertionError("urlopen")

    monkeypatch.setattr("urllib.request.urlopen", explode)
    save_case(
        Case(
            id="db",
            events=[_event("execution", "geth", 'err="leveldb: closed"', "2026-10-08T06:13:58Z", 0)],
        )
    )
    code, stdout, stderr = run("ask", "--case", "db", "why did it fail")
    assert code == 0, stderr
    assert calls == []
    assert "remote call" not in stdout
    assert "remote call" not in stderr


def test_remote_sends_the_question_rows_and_runbook(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, dict[str, object]]] = []

    def spy(url: str, payload: dict[str, object]) -> None:
        assert "remote call about to happen: question, cited rows, and runbook text" in sys.stdout.getvalue()
        sent.append((url, payload))

    monkeypatch.setattr("chainlog_ai.cli.send_remote", spy)
    monkeypatch.setenv("CHAINLOG_AI_REMOTE_URL", "http://127.0.0.1:9/model")
    save_case(
        Case(
            id="db",
            events=[
                _event(
                    "execution",
                    "geth",
                    'err="leveldb: closed" password=[redacted]',
                    "2026-10-08T06:13:58Z",
                    0,
                )
            ],
        )
    )
    code, stdout, stderr = run("ask", "--remote", "--case", "db", "why did it fail")
    assert code == 0, stderr
    assert stdout.startswith("remote call about to happen: question, cited rows, and runbook text\n")
    assert len(sent) == 1
    url, payload = sent[0]
    assert url == "http://127.0.0.1:9/model"
    assert list(payload) == ["question", "rows", "runbook"]
    assert payload["question"] == "why did it fail"
    encoded = json.dumps(payload)
    assert "leveldb: closed" in encoded
    assert "password=[redacted]" in encoded
    assert "runbook database-closed:" in encoded
    assert "/tmp/execution.log" not in encoded
    assert "hunter2" not in encoded


def test_remote_is_refused_when_redaction_did_not_run(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("urlopen")

    monkeypatch.setattr("urllib.request.urlopen", explode)
    monkeypatch.setenv("CHAINLOG_AI_REMOTE_URL", "http://127.0.0.1:9/model")
    save_case(
        Case(
            id="db",
            events=[
                _event(
                    "execution",
                    "geth",
                    'err="leveldb: closed" password=hunter2',
                    "2026-10-08T06:13:58Z",
                    0,
                )
            ],
        )
    )
    code, stdout, stderr = run("ask", "--remote", "--case", "db", "why did it fail")
    assert code == 3
    assert stdout == ""
    assert "remote call refused: redaction did not run" in stderr
    assert "hunter2" not in stdout
    assert "hunter2" not in stderr


def test_remote_is_refused_when_no_model_is_configured(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("urlopen")

    monkeypatch.setattr("urllib.request.urlopen", explode)
    monkeypatch.delenv("CHAINLOG_AI_REMOTE_URL", raising=False)
    save_case(
        Case(
            id="db",
            events=[_event("execution", "geth", 'err="leveldb: closed"', "2026-10-08T06:13:58Z", 0)],
        )
    )
    code, stdout, stderr = run("ask", "--remote", "--case", "db", "why did it fail")
    assert code == 3
    assert "remote call refused: no remote model configured" in stderr
    assert "remote call about to happen" not in stdout
    assert "database-closed" in stdout


def test_send_remote_posts_json() -> None:
    received: dict[str, object] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            received["body"] = self.rfile.read(length)
            received["type"] = self.headers.get("Content-Type")
            self.send_response(204)
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    payload = {"question": "why did it fail", "rows": ["leveldb: closed"], "runbook": ["runbook database-closed: x"]}
    try:
        port = server.server_address[1]
        send_remote(f"http://127.0.0.1:{port}/", payload)
    finally:
        thread.join(timeout=5)
        server.server_close()
    assert received["type"] == "application/json"
    assert json.loads(received["body"]) == payload


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
