"""Kyocera vendor driver (Phase 5): generic Printer-MIB + Kyocera overlay.

Overlay OIDs live in ``oids.VENDOR["kyocera"]`` (empty until verified on hardware).
See drivers/vendor.py for the supplementary-overlay rules.
"""

from server.printers.drivers.vendor import make_vendor_reader

read = make_vendor_reader("kyocera")
