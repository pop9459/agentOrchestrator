from typer.testing import CliRunner

from ao import __version__
from ao.cli import app

runner = CliRunner()


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == f"ao {__version__}"
