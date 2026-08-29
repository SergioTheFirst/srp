"""Test package marker.

The suite imports shared fixtures as ``from tests.conftest import ...``. Without
this file ``tests`` is only a namespace package, and ANY regular ``tests`` package
installed into the interpreter's site-packages (some wheels ship one by mistake)
wins the import and breaks collection of the whole suite. An explicit package here
makes the repo's own ``tests`` authoritative regardless of what is installed.
"""
