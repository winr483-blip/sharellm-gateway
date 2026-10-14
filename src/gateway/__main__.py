"""``python -m gateway`` entry point.

Running ``python -m gateway`` executes :func:`gateway.main.run_cli`, which
parses the CLI arguments, resolves and loads the configuration, prints the
startup banner and then hands the application to uvicorn.

``python -m gateway --help`` prints usage and exits **without** binding a
socket: argparse handles ``--help`` (raising ``SystemExit``) before
:func:`gateway.main.run_cli` ever reaches ``uvicorn.run``.
"""

from __future__ import annotations

from gateway.main import run_cli

if __name__ == "__main__":
    run_cli()
