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
    assert "database-corrupt 1" in stdout
    assert "database-lock 1" in stdout
    assert "disk-full 1" in stdout
    assert "permission-denied 1" in stdout
    assert "invalid-payload 1" in stdout
    assert "capability-mismatch 1" in stdout
    assert "peer-banned 1" in stdout
    assert "discovery-failed 1" in stdout
    assert "no-peers 1" in stdout
    assert "sync-stalled 1" in stdout
    assert "freezer-rewind 1" in stdout
    assert "engine-timeout 2" in stdout
    assert "jwt-rejected 1" in stdout
    assert "missed-attestation 2" in stdout
    assert "missed-proposal 2" in stdout
    assert "checkpoint-sync-failed 1" in stdout
    assert "weak-subjectivity 1" in stdout
    assert "fork-choice 1" in stdout
    assert "reorg 1" in stdout
    assert "clock-not-synced 1" in stdout
    assert "doppelganger 1" in stdout
    assert "signer-timeout 1" in stdout
    assert "signer-refused 1" in stdout
    assert "relay-timeout 2" in stdout
    assert "builder-timeout 1" in stdout
    assert "oom-killed 1" in stdout
    assert "probe-failed 1" in stdout
    assert "killed-externally 1" in stdout
    assert "memory-pressure 1" in stdout
    assert "cpu-pressure 1" in stdout
    assert "disk-pressure 1" in stdout
    assert "evicted 1" in stdout
    assert "crash-loop 1" in stdout
    assert "volume-mount-failed 1" in stdout
    assert "node-not-ready 1" in stdout
    assert "unclassified " in stdout
    case = load_case("incident")
    closed = next(event for event in case.events if "leveldb: closed" in event.excerpt)
    assert closed.failure_class == "database-closed"
    assert closed.rule == "geth-database-closed"
    kept = next(event for event in case.events if "Successfully published attestation" in event.excerpt)
    assert kept.failure_class is None
    assert kept.rule is None
    assert "Successfully published attestation" in kept.excerpt


def test_each_execution_client_covers_the_incident_failures(home: Path) -> None:
    root = Path(__file__).parents[1] / "examples"
    classes = {
        "database-corrupt",
        "database-lock",
        "disk-full",
        "permission-denied",
        "invalid-payload",
        "engine-timeout",
        "capability-mismatch",
        "peer-banned",
        "discovery-failed",
        "no-peers",
        "sync-stalled",
        "freezer-rewind",
        "jwt-rejected",
    }
    rules = {
        "nethermind": ("Corruption:", "nethermind-database-corrupt"),
        "erigon": ("MDBX_BUSY", "erigon-database-lock"),
        "besu": ("StorageException", "besu-database-corrupt"),
        "reth": ("Database write error", "reth-database-corrupt"),
    }
    for name in ("geth", "nethermind", "erigon", "besu", "reth"):
        code, _stdout, stderr = run("ingest", "--case", name, "--execution", str(root / f"{name}.log"))
        assert code == 0, stderr
        code, stdout, stderr = run("classify", "--case", name)
        assert code == 0, stderr
        case = load_case(name)
        found = {event.failure_class for event in case.events if event.failure_class}
        assert classes <= found, name
        assert "unclassified " in stdout
        if name in rules:
            phrase, rule_id = rules[name]
            matched = next(event for event in case.events if phrase in event.excerpt)
            assert matched.rule == rule_id


def test_consensus_and_validator_clients_cover_their_failures(home: Path, tmp_path: Path) -> None:
    root = Path(__file__).parents[1] / "examples"
    consensus = {
        "missed-attestation",
        "missed-proposal",
        "checkpoint-sync-failed",
        "weak-subjectivity",
        "fork-choice",
        "reorg",
        "clock-not-synced",
    }
    validator = {
        "missed-attestation",
        "missed-proposal",
        "doppelganger",
        "signer-timeout",
        "signer-refused",
    }
    rules = {
        "beacon": ("Missed attestation", "lighthouse-missed-attestation"),
        "prysm": ("weak-subjectivity-checkpoint", "prysm-weak-subjectivity"),
        "nimbus": ("Chain reorg", "nimbus-reorg"),
        "lodestar": ("System clock is not in sync", "lodestar-clock"),
        "validator": ("Doppelganger(s) detected", "lighthouse-doppelganger"),
        "teku-validator": ("External signer failed to sign", "teku-signer-refused"),
        "web3signer": ("Failed to sign", "web3signer-signer-refused"),
        "mev-boost": ("error calling getHeader on relay", "mev-boost-relay-timeout"),
    }
    samples = {
        "beacon": ("consensus", "beacon.log", consensus),
        "prysm": ("consensus", "prysm.log", consensus),
        "teku": ("consensus", "teku.log", consensus),
        "nimbus": ("consensus", "nimbus.log", consensus),
        "lodestar": ("consensus", "lodestar.log", consensus),
        "validator": ("validator", "validator.log", validator),
        "prysm-validator": ("validator", "prysm-validator.log", validator),
        "teku-validator": ("validator", "teku-validator.log", validator),
        "nimbus-validator": ("validator", "nimbus-validator.log", validator),
        "lodestar-validator": ("validator", "lodestar-validator.log", validator),
        "web3signer": ("validator", "web3signer.log", {"doppelganger", "signer-timeout", "signer-refused"}),
        "mev-boost": ("builder", "mev-boost.log", {"relay-timeout", "builder-timeout"}),
    }
    for case_id, (flag, name, classes) in samples.items():
        code, _stdout, stderr = run("ingest", "--case", case_id, f"--{flag}", str(root / name))
        assert code == 0, stderr
        code, stdout, stderr = run("classify", "--case", case_id)
        assert code == 0, stderr
        case = load_case(case_id)
        found = {event.failure_class for event in case.events if event.failure_class}
        assert classes <= found, case_id
        assert "unclassified " in stdout
        if case_id in rules:
            phrase, rule_id = rules[case_id]
            matched = next(event for event in case.events if phrase in event.excerpt)
            assert matched.rule == rule_id, case_id
    builder = next(event for event in load_case("mev-boost").events if "builder timeout" in event.excerpt)
    assert builder.rule == "mev-boost-builder-timeout"

    path = tmp_path / "beacon.log"
    path.write_text(
        "Oct 08 06:12:00.102 INFO  Lighthouse started\n"
        "Oct 08 06:14:20.300 INFO  Beacon chain re-org\n",
        encoding="utf-8",
    )
    code, _stdout, stderr = run(
        "ingest",
        "--case",
        "pod",
        "--kube",
        str(path),
        "--namespace",
        "ethereum",
        "--pod",
        "beacon-0",
        "--container",
        "lighthouse",
        "--previous",
    )
    assert code == 0, stderr
    code, _stdout, stderr = run("classify", "--case", "pod")
    assert code == 0, stderr
    reorg = next(event for event in load_case("pod").events if "re-org" in event.excerpt)
    assert reorg.source == "kubernetes-previous"
    assert reorg.failure_class == "reorg"
    assert reorg.rule == "consensus-reorg"


def test_kubernetes_events_name_the_cluster_cause(home: Path, tmp_path: Path) -> None:
    root = Path(__file__).parents[1] / "examples" / "events.json"
    code, _stdout, stderr = run("ingest", "--case", "kube", "--kube", str(root))
    assert code == 0, stderr
    code, stdout, stderr = run("classify", "--case", "kube")
    assert code == 0, stderr
    rules = {
        "OOMKilled": "kube-oom-killed",
        "Unhealthy": "kube-probe-failed",
        "Killing": "kube-killed-externally",
        "NodeHasInsufficientMemory": "kube-memory-pressure",
        "Insufficient cpu": "kube-cpu-pressure",
        "NodeHasDiskPressure": "kube-disk-pressure",
        "Evicted": "kube-evicted",
        "BackOff": "kube-crash-loop",
        "FailedMount": "kube-volume-mount-failed",
        "NodeNotReady": "kube-node-not-ready",
    }
    case = load_case("kube")
    for phrase, rule_id in rules.items():
        matched = next(event for event in case.events if phrase in event.excerpt)
        assert matched.rule == rule_id, phrase
        assert matched.failure_class is not None
    assert "unclassified " in stdout
    killing = next(event for event in case.events if event.reason == "Killing")
    assert killing.failure_class == "killed-externally"

    path = tmp_path / "0.log"
    path.write_text(
        "2026-10-08T06:13:55.000000000Z stderr F WARN Killing container from outside the client\n",
        encoding="utf-8",
    )
    code, _stdout, stderr = run(
        "ingest",
        "--case",
        "previous",
        "--kube",
        str(path),
        "--namespace",
        "ethereum",
        "--pod",
        "geth-0",
        "--container",
        "geth",
        "--restart",
        "0",
        "--previous",
    )
    assert code == 0, stderr
    code, _stdout, stderr = run("classify", "--case", "previous")
    assert code == 0, stderr
    previous = load_case("previous").events[0]
    assert previous.source == "kubernetes-previous"
    assert previous.failure_class == "killed-externally"
    assert previous.rule == "kube-killed-externally"


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
