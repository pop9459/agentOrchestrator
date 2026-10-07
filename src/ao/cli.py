"""`ao` command-line entrypoint."""

from typing import Annotated

import typer

from ao import __version__

app = typer.Typer(
    name="ao",
    help="agentOrchestrator: a small, token-efficient company of hireable agents.",
    no_args_is_help=True,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"ao {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_version_callback, is_eager=True, help="Show version and exit."
        ),
    ] = False,
) -> None:
    """agentOrchestrator CLI."""
