"""Deep SMART: ATA attributes via CIM (Tier A) + NVMe health log via IOCTL (Tier B).

Two independent, best-effort tiers layered onto the existing per-disk storage
rows (matched by ``serial_hash``, falling back to positional index). Each tier
has its own try/except: a USB enclosure, a RAID controller, or a VM disk that
answers neither tier simply contributes no overlay fields -- never an
exception, never a crash of the wider historical collector (see K7: a dead
observation channel is a gap, not a health verdict).

Tier A reads the raw ``FailurePredictData`` VendorSpecific blob (the classic
ATA SMART attribute table Windows already exposes over WMI) and decodes only
the documented attribute core the storage engine judges on. Tier B speaks
``IOCTL_STORAGE_QUERY_PROPERTY`` directly -- there is no built-in cmdlet for
the NVMe Health Information Log page.
"""

from __future__ import annotations

import base64
import ctypes
import struct
import threading
from ctypes import wintypes
from typing import Any, Callable, Optional

from client.collectors.inventory import hash_serial
from client.collectors.ps import as_list, run_ps

_SMART_SCRIPT = r"""
$m=@(); foreach ($d in Get-CimInstance Win32_DiskDrive) {
  try { $m += @{pnp="$($d.PNPDeviceID)"; index=[int]$d.Index; serial="$($d.SerialNumber)".Trim()} } catch {} }
$st=@(); foreach ($p in Get-CimInstance -Namespace root\wmi -Class MSStorageDriver_FailurePredictStatus -ErrorAction SilentlyContinue) {
  try { $st += @{inst="$($p.InstanceName)"; predict=[bool]$p.PredictFailure} } catch {} }
$da=@(); foreach ($p in Get-CimInstance -Namespace root\wmi -Class MSStorageDriver_FailurePredictData -ErrorAction SilentlyContinue) {
  try { $da += @{inst="$($p.InstanceName)"; blob=[Convert]::ToBase64String($p.VendorSpecific)} } catch {} }
@{disks=$m; status=$st; data=$da} | ConvertTo-Json -Depth 5 -Compress
"""

# Documented ATA SMART attribute core the storage engine scores (§1.4/T2.2);
# the rest of the 30-slot table is ignored, not just unscored -- undocumented
# vendor attributes are noise the model must not react to (K8).
_ATTR_WHITELIST = {
    1,
    5,
    9,
    10,
    175,
    177,
    181,
    182,
    184,
    187,
    188,
    194,
    196,
    197,
    198,
    199,
    231,
    233,
    235,
    241,
    242,
}
# A few raw fields carry vendor bits above the documented counter width;
# mask them down before trusting the value.
_RAW_MASK = {9: 2**32 - 1, 188: 2**32 - 1, 194: 2**16 - 1}


def parse_ata_smart(blob: bytes) -> dict[str, int]:
    """Decode a raw FailurePredictData VendorSpecific blob into {attr_id: raw}.

    Layout: attribute table starts at offset 2, 30 slots x 12 bytes each
    (id, status x2, current, worst, raw x6, reserved). id==0 is an empty slot.
    """
    if len(blob) < 362:
        return {}
    out: dict[str, int] = {}
    for i in range(30):
        off = 2 + i * 12
        attr_id = blob[off]
        if attr_id == 0 or attr_id not in _ATTR_WHITELIST:
            continue
        raw = int.from_bytes(blob[off + 5 : off + 11], "little")
        mask = _RAW_MASK.get(attr_id)
        if mask is not None:
            raw &= mask
        out[str(attr_id)] = raw
    return out


# --------------------------------------------------------------------------- #
# Tier B: NVMe Health Information Log (IOCTL_STORAGE_QUERY_PROPERTY)
# --------------------------------------------------------------------------- #

IOCTL_STORAGE_QUERY_PROPERTY = 0x2D1400
_PROP_ID, _NVME, _LOG_PAGE = 50, 3, 2
_QUERY_OFF, _SPSD_LEN, _DATA_LEN = 8, 28, 512
_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_OPEN_EXISTING = 3
_INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value


def _u128_clamped(buf: bytes, off: int) -> int:
    return min(int.from_bytes(buf[off : off + 16], "little"), 2**63 - 1)


def _parse_nvme_log(log: bytes) -> dict[str, int]:
    out: dict[str, int] = {
        "nvme_critical_warning": log[0],
        "nvme_spare_pct": log[3],
        "nvme_spare_threshold_pct": log[4],
        "nvme_percentage_used": log[5],
        "nvme_data_units_written": _u128_clamped(log, 48),
        "nvme_power_cycles": _u128_clamped(log, 112),
        "power_on_hours": _u128_clamped(log, 128),
        "nvme_unsafe_shutdowns": _u128_clamped(log, 144),
        "nvme_media_errors": _u128_clamped(log, 160),
        "nvme_error_log_entries": _u128_clamped(log, 176),
    }
    temp_k = int.from_bytes(log[1:3], "little")
    if temp_k > 0:  # 0 means the controller left the field unpopulated -- don't fabricate -273C
        out["temperature_c"] = temp_k - 273
    return out


def _win32_ioctl(disk_index: int, buf: bytearray) -> Optional[bytes]:
    """Real Win32 transport: open the physical disk, issue the IOCTL, close it.

    Only reached in production on real Windows -- tests always inject
    ``ioctl_fn`` instead, so ``ctypes.windll`` is never touched off-Windows.
    """

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.DeviceIoControl.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    kernel32.DeviceIoControl.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = kernel32.CreateFileW(
        f"\\\\.\\PhysicalDrive{disk_index}",
        _GENERIC_READ,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        None,
        _OPEN_EXISTING,
        0,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        return None
    try:
        returned = wintypes.DWORD(0)
        c_buf = (ctypes.c_ubyte * len(buf)).from_buffer(buf)
        ok = kernel32.DeviceIoControl(
            handle,
            IOCTL_STORAGE_QUERY_PROPERTY,
            c_buf,
            len(buf),
            c_buf,
            len(buf),
            ctypes.byref(returned),
            None,
        )
        return bytes(buf) if ok else None
    finally:
        kernel32.CloseHandle(handle)


# A wedged DeviceIoControl on a flaky controller must not hang the whole
# collector; the join timeout is a module constant so tests can shrink it.
_IOCTL_JOIN_TIMEOUT_SEC = 5.0


def read_nvme_health(
    disk_index: int, ioctl_fn: Optional[Callable[[int, bytearray], Optional[bytes]]] = None
) -> Optional[dict[str, int]]:
    """Read the NVMe SMART/Health Information Log (page 02h) for one disk.

    ``ioctl_fn`` is the transport injection point for tests: given
    (disk_index, request buffer), it returns the driver's response buffer (or
    None on failure). Runs in a daemon thread with a bounded join -- see
    ``_IOCTL_JOIN_TIMEOUT_SEC`` -- so a wedged call returns None to the caller
    promptly. Known ceiling: a truly (not just slowly) wedged DeviceIoControl
    leaks that one thread + disk handle rather than being forcibly cancelled;
    true cancellation needs overlapped I/O + CancelIoEx, out of scope here.
    """
    buf = bytearray(_QUERY_OFF + _SPSD_LEN + _DATA_LEN)
    struct.pack_into("<2L", buf, 0, _PROP_ID, 0)
    struct.pack_into("<7L", buf, _QUERY_OFF, _NVME, _LOG_PAGE, 0x02, 0, _SPSD_LEN, _DATA_LEN, 0)

    transport = ioctl_fn or _win32_ioctl
    outcome: dict[str, Optional[bytes]] = {"buf": None}

    def _run() -> None:
        try:
            outcome["buf"] = transport(disk_index, buf)
        except OSError:
            outcome["buf"] = None

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(_IOCTL_JOIN_TIMEOUT_SEC)
    out = outcome["buf"]
    if t.is_alive() or out is None or len(out) < _QUERY_OFF + 20:
        return None
    off = struct.unpack_from("<L", out, _QUERY_OFF + 16)[0]
    start = _QUERY_OFF + off
    log = out[start : start + _DATA_LEN]
    if len(log) < 192:
        return None
    return _parse_nvme_log(log)


# --------------------------------------------------------------------------- #
# Wiring: merge both tiers onto the base per-disk rows from historical._SCRIPT
# --------------------------------------------------------------------------- #


def _norm_inst(value: Any) -> str:
    s = str(value or "").strip()
    if s.lower().endswith("_0"):
        s = s[:-2]
    return s.lower()


def _merge_overlay(
    base: list[dict[str, Any]],
    by_hash: dict[str, dict[str, Any]],
    by_index: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    merged = []
    for i, row in enumerate(base):
        row = dict(row)
        sh = row.get("serial_hash")
        overlay = by_hash.get(sh) if sh else None
        if overlay is None:
            overlay = by_index.get(i)
        if overlay:
            row.update(overlay)
        merged.append(row)
    return merged


def collect_smart(base_storage: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    """Layer Tier A + Tier B onto *base_storage* rows; return (merged, status).

    status: "ok" if at least one tier produced a reading for at least one
    disk, "partial" if the Tier A script ran but nothing merged, or the raw
    ``run_ps`` status (timeout/blocked/absent) if the script never ran.
    """
    result = run_ps(_SMART_SCRIPT, timeout=60)
    if result.status != "ok" or not isinstance(result.data, dict):
        return list(base_storage), (result.status if result.status != "ok" else "partial")

    disks = as_list(result.data.get("disks"))
    predict_by_inst = {
        _norm_inst(s["inst"]): bool(s.get("predict"))
        for s in as_list(result.data.get("status"))
        if isinstance(s, dict) and s.get("inst")
    }
    blob_by_inst = {
        _norm_inst(d["inst"]): d.get("blob")
        for d in as_list(result.data.get("data"))
        if isinstance(d, dict) and d.get("inst")
    }

    by_hash: dict[str, dict[str, Any]] = {}
    by_index: dict[int, dict[str, Any]] = {}
    any_hit = False

    for d in disks:
        if not isinstance(d, dict):
            continue
        index = d.get("index")
        pnp = _norm_inst(d.get("pnp"))
        overlay: dict[str, Any] = {}

        if pnp in predict_by_inst:
            overlay["smart_predict_fail"] = predict_by_inst[pnp]
            any_hit = True

        blob_b64 = blob_by_inst.get(pnp)
        if blob_b64:
            try:
                attrs = parse_ata_smart(base64.b64decode(blob_b64))
            except ValueError:
                attrs = {}
            if attrs:
                overlay["smart_attrs"] = attrs
                any_hit = True

        if isinstance(index, int):
            nvme = read_nvme_health(index)
            if nvme:
                overlay.update(nvme)
                any_hit = True

        if not overlay:
            continue
        disk_hash = hash_serial(d.get("serial"))
        if disk_hash:
            by_hash[disk_hash] = overlay
        elif isinstance(index, int):
            by_index[index] = overlay

    merged = _merge_overlay(base_storage, by_hash, by_index)
    return merged, ("ok" if any_hit else "partial")
