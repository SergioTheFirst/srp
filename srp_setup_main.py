"""PyInstaller entry point for srp-setup.exe (the one-command installer).

Not used directly -- run the installer as:  python -m client.deploy.setup
"""

import sys

from client.deploy.setup import main

if __name__ == "__main__":
    sys.exit(main())
