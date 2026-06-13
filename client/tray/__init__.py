"""SRP per-user tray client (tray spec §1-§4).

A second plane next to the SYSTEM agent: the agent collects + writes
``status.json``; this tray, running in each user's session, reads it, checks the
user's personal certificate, and shows a system-tray icon + panel. Pure stdlib
(ctypes + tkinter), zero third-party deps -- the agent invariant holds here too.

All decidable logic lives in :mod:`client.tray.state` (pure functions, unit
tested off-Windows); :mod:`client.tray.icon` and :mod:`client.tray.panel` are
thin Windows-only adapters.
"""
