"""Brother vendor driver (Phase 5): generic Printer-MIB + Brother overlay.

Overlay OIDs live in ``oids.VENDOR["brother"]`` (empty until verified on hardware).
Brother exposes some counters as packed hex strings rather than INTEGERs; when
those OIDs are added, a value parser belongs here. See drivers/vendor.py.
"""

from server.printers.drivers.vendor import make_vendor_reader

read = make_vendor_reader("brother")
