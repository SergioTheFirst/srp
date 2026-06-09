"""PyInstaller entry point for srp-agent.exe.

Not used directly -- run the agent as:  python -m client.agent
"""

from client.agent import main

if __name__ == "__main__":
    main()
