import json
from pathlib import Path

import pytest

from chainlog_ai.recognize import RecognizeError, recognize

_EXAMPLES = Path(__file__).parents[1] / "examples"


@pytest.mark.parametrize(
    ("name", "source", "client"),
    [
        ("geth.log", "execution", "geth"),
        ("beacon.log", "consensus", "lighthouse"),
        ("prysm.log", "consensus", "prysm"),
        ("validator.log", "validator", "lighthouse"),
        ("prysm-validator.log", "validator", "prysm"),
        ("web3signer.log", "validator", "web3signer"),
        ("mev-boost.log", "builder", "mev-boost"),
        ("events.json", "kubernetes", None),
        ("alert.json", "alert", None),
        ("metrics.json", "metrics", None),
        ("pods/ethereum_geth-0_poduid/geth/0.log", "kubernetes", None),
    ],
)
def test_a_sample_file_has_one_source(name: str, source: str, client: str | None) -> None:
    found = recognize(_EXAMPLES / name)
    assert found.source == source
    assert found.client == client


def test_an_unknown_file_is_named(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("nothing to see here\n", encoding="utf-8")
    with pytest.raises(RecognizeError, match="cannot tell what") as raised:
        recognize(path)
    assert raised.value.kind == "unknown"
    assert path.name in str(raised.value)


def test_two_sources_are_named(tmp_path: Path) -> None:
    path = tmp_path / "mixed.log"
    path.write_text(
        "\n".join(
            [
                "INFO Starting Geth on Ethereum mainnet",
                "Oct 08 06:00:00.001 INFO  Lighthouse started                      version: Lighthouse/v5.3.0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(RecognizeError, match="matches consensus and execution") as raised:
        recognize(path)
    assert raised.value.kind == "ambiguous"
    assert path.name in str(raised.value)


def test_a_validator_banner_and_a_beacon_banner_are_two_sources(tmp_path: Path) -> None:
    path = tmp_path / "both.log"
    path.write_text(
        "\n".join(
            [
                "Oct 08 06:00:00.001 INFO  Lighthouse started                      version: Lighthouse/v5.3.0",
                "Oct 08 06:00:01.001 INFO  Lighthouse validator client started    version: Lighthouse/v5.3.0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(RecognizeError, match="matches consensus and validator"):
        recognize(path)


def test_a_missing_file_cannot_be_read(tmp_path: Path) -> None:
    path = tmp_path / "gone.log"
    with pytest.raises(RecognizeError, match="cannot read") as raised:
        recognize(path)
    assert raised.value.kind == "read"


def test_alert_json_is_not_a_metric(tmp_path: Path) -> None:
    path = tmp_path / "one.json"
    path.write_text(
        json.dumps({"labels": {"alertname": "ValidatorMissedAttestation"}, "startsAt": "2026-10-08T06:14:00.010Z"}),
        encoding="utf-8",
    )
    assert recognize(path).source == "alert"
