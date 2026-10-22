import subprocess
import sys

from chainlog_ai import __version__


def test_package_version() -> None:
    assert __version__ == "0.3.0"


def test_cli_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "chainlog_ai", "--version"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "chainlog-ai 0.3.0"
