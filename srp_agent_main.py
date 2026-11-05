"""PyInstaller entry point for srp-agent.exe.

Not used directly -- run the agent as:  python -m client.agent
"""

import logging
import sys


def run() -> None:
    """main() under a last-resort guard (RG-2): srp-agent.exe is a WINDOWED build,
    and PyInstaller shows an unhandled exception as a traceback window -- in
    session 0 nobody can close it, so the process hung forever holding the
    instance mutex and fooled the post-update liveness probe. Exit 1 instead.
    SystemExit (deliberate codes 2/3) passes through untouched."""
    try:
        from client.agent import main

        main()
    except Exception:  # noqa: BLE001 -- see docstring
        logging.getLogger("srp.agent").exception("fatal: unhandled exception")
        sys.exit(1)


if __name__ == "__main__":
    run()
