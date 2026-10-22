from pathlib import Path

_PAGE = Path(__file__).parents[1] / "docs" / "index.html"


def test_the_landing_page_states_the_split_and_the_install() -> None:
    text = _PAGE.read_text(encoding="utf-8")
    assert "why did my infrastructure fail?" in text
    assert "ValidatorPulse raises the alert." in text
    assert "ChainDiff decides whether an upgrade is safe." in text
    assert "pip install chainlog-ai" in text
    assert 'href="https://github.com/ehsanhajian/chainlog-ai#install"' in text
