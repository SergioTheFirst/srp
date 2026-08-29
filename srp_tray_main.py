"""PyInstaller entry point for srp-tray.exe (windowed).

Not used directly -- run the tray as:  python -m client.tray
"""

import sys

from client.tray.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
