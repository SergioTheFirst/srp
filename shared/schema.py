"""SRP message contract (pydantic v2).

Four message types flow client -> server, each wrapped in an Envelope:

  inventory   - slow-changing identity of the machine (sent on start / daily)
  historical  - the day-1 "machine already contains its own history" scan
  heartbeat   - periodic performance samplers (the live vitals)
  events      - whitelisted Windows event-log batch

Design notes:
  * Absolutes are weak signals; the server derives trends/baselines. The agent
    only reports what it observed. (Part 1 thesis: info lives in derivatives.)
  * Payload models allow extra fields (forward-compatible contract, Part 3 C3.4).
  * Every analytic field is Optional: an office PC may block a source (no kernel
    driver for temp/voltage). Missing != zero -> we send None and flag degraded.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

CONTRACT_VERSION = "0.1.0"

MsgType = Literal["inventory", "historical", "heartbeat", "events", "print_jobs"]


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_version(value: Optional[str]) -> Optional[tuple[int, int, int]]:
    """Parse a strict MAJOR.MINOR.PATCH string into a tuple; None if malformed."""
    if not value or not isinstance(value, str):
        return None
    parts = value.split(".")
    if len(parts) != 3:
        return None
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    return (nums[0], nums[1], nums[2])


def is_contract_compatible(agent_version: Optional[str]) -> bool:
    """True when agent_version shares the server CONTRACT_VERSION's MAJOR (W0.4).

    The contract is additive (optional fields + extra='allow'), so any same-MAJOR
    agent's envelope parses. A different or unreadable MAJOR is flagged
    incompatible -- but the caller MUST still keep the telemetry (UNKNOWN over
    false confidence), never drop it on a version mismatch.
    """
    agent = parse_version(agent_version)
    server = parse_version(CONTRACT_VERSION)
    if agent is None or server is None:
        return False
    return agent[0] == server[0]


class _Base(BaseModel):
    # Forward-compatible: a newer agent may add fields an older server ignores.
    model_config = ConfigDict(extra="allow")


# --------------------------------------------------------------------------- #
# Inventory  (identity / slow-changing)
# --------------------------------------------------------------------------- #
class DiskInfo(_Base):
    model: Optional[str] = None
    media_type: Optional[str] = None  # SSD / HDD / Unspecified
    size_gb: Optional[float] = None
    serial_hash: Optional[str] = None  # hashed, never raw serial
    firmware: Optional[str] = None
    interface: Optional[str] = None  # NVMe / SATA / USB
    bus_type: Optional[str] = None


class MemoryModule(_Base):
    capacity_gb: Optional[float] = None
    speed_mhz: Optional[int] = None
    manufacturer: Optional[str] = None
    part_number: Optional[str] = None


class InventoryPayload(_Base):
    hostname: Optional[str] = None
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    chassis: Optional[str] = None  # desktop / laptop / unknown
    os_caption: Optional[str] = None
    os_build: Optional[str] = None
    os_install_date: Optional[str] = None  # ISO; used to estimate age
    bios_version: Optional[str] = None
    bios_release_date: Optional[str] = None  # ISO; proxy for hardware age
    cpu_name: Optional[str] = None
    cpu_cores: Optional[int] = None
    cpu_logical: Optional[int] = None
    total_ram_gb: Optional[float] = None
    memory_modules: list[MemoryModule] = Field(default_factory=list)
    disks: list[DiskInfo] = Field(default_factory=list)
    driver_problem_count: Optional[int] = None  # PnP ConfigManagerErrorCode<>0
    pending_reboot: Optional[bool] = None


# --------------------------------------------------------------------------- #
# Historical  (day-1 scan: the machine's own past = a free dataset)
# --------------------------------------------------------------------------- #
class StorageReliability(_Base):
    disk: Optional[str] = None
    media_type: Optional[str] = None
    wear_pct: Optional[float] = None  # SSD wear indicator, 0..100 worse
    power_on_hours: Optional[int] = None
    reallocated_sectors: Optional[int] = None  # HDD pending death signal
    read_errors_total: Optional[int] = None
    write_errors_total: Optional[int] = None
    temperature_c: Optional[int] = None  # best-effort, often absent


class BatteryInfo(_Base):
    present: bool = False
    design_capacity_mwh: Optional[int] = None
    full_charge_capacity_mwh: Optional[int] = None
    wear_pct: Optional[float] = None  # 1 - full/design, in %
    cycle_count: Optional[int] = None


class CertInfo(_Base):
    subject: Optional[str] = None
    issuer: Optional[str] = None
    thumbprint: Optional[str] = None
    not_after: Optional[str] = None
    not_before: Optional[str] = None


class NetAdapter(_Base):
    name: Optional[str] = None
    desc: Optional[str] = None
    mac: Optional[str] = None
    kind: Optional[str] = None  # "ethernet" | "wifi" | "other"
    up: Optional[bool] = None
    link_mbps: Optional[float] = None
    ipv4: list[str] = Field(default_factory=list)
    ipv6: list[str] = Field(default_factory=list)
    gateway: Optional[str] = None
    dns: list[str] = Field(default_factory=list)
    dhcp: Optional[bool] = None
    ssid: Optional[str] = None
    signal_pct: Optional[int] = None
    channel: Optional[int] = None


class NetNeighbor(_Base):
    ip: Optional[str] = None
    mac: Optional[str] = None
    state: Optional[str] = None


class NetConnection(_Base):
    local_ip: Optional[str] = None
    local_port: Optional[int] = None
    remote_ip: Optional[str] = None
    remote_port: Optional[int] = None
    state: Optional[str] = None


class NetQuality(_Base):
    target_kind: Optional[str] = None  # "gateway" | "dns"
    target: Optional[str] = None
    latency_ms: Optional[float] = None
    loss_pct: Optional[float] = None
    samples: Optional[int] = None


class HistoricalPayload(_Base):
    reliability_stability_index: Optional[float] = None  # 0..10, latest sample
    kernel_power_41_30d: Optional[int] = None  # unexpected power loss / hang
    dirty_shutdowns_30d: Optional[int] = None  # EventLog 6008
    bugchecks_30d: Optional[int] = None  # BugCheck 1001 (BSOD)
    app_crashes_30d: Optional[int] = None  # Application Error 1000
    whea_errors_30d: Optional[int] = None  # WHEA-Logger (corrected HW err)
    avg_boot_ms: Optional[int] = None  # Diagnostics-Performance 100
    storage: list[StorageReliability] = Field(default_factory=list)
    battery: Optional[BatteryInfo] = None
    observation_days: Optional[int] = None  # how far back the data reaches
    certificates: list[CertInfo] = Field(default_factory=list)
    network_adapters: list[NetAdapter] = Field(default_factory=list)
    network_neighbors: list[NetNeighbor] = Field(default_factory=list)
    network_connections: list[NetConnection] = Field(default_factory=list)
    network_quality: list[NetQuality] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Heartbeat  (live vitals; throttle-residency stands in for "thermal health")
# --------------------------------------------------------------------------- #
class HeartbeatPayload(_Base):
    cpu_pct: Optional[float] = None
    cpu_perf_pct: Optional[float] = None  # % Processor Performance proxy
    mem_avail_mb: Optional[float] = None
    committed_pct: Optional[float] = None
    pagefile_pct: Optional[float] = None
    disk_read_sec: Optional[float] = None  # Avg Disk sec/Read (latency, s)
    disk_write_sec: Optional[float] = None
    disk_queue: Optional[float] = None
    free_space_pct: Optional[float] = None  # system drive
    handle_count_total: Optional[int] = None  # leak proxy
    nic_errors: Optional[int] = None
    user_present: Optional[bool] = None
    uptime_hours: Optional[float] = None


# --------------------------------------------------------------------------- #
# Events  (whitelisted log batch)
# --------------------------------------------------------------------------- #
class EventItem(_Base):
    ts: Optional[str] = None
    log: Optional[str] = None
    source: Optional[str] = None
    event_id: Optional[int] = None
    level: Optional[str] = None  # Critical / Error / Warning
    message: Optional[str] = None


class EventBatchPayload(_Base):
    events: list[EventItem] = Field(default_factory=list)
    window_hours: Optional[float] = None


# --------------------------------------------------------------------------- #
# Source health (collector-trust, per logical source, §5 / §12 of contract)
# --------------------------------------------------------------------------- #
class SourceHealth(_Base):
    """Per-source collector status reported by the agent on every envelope.

    status: one of ok | partial | empty | timeout | blocked | absent
    collected_at: UTC ISO timestamp when the collector ran (None if it never ran).
    """

    status: Literal["ok", "partial", "empty", "timeout", "blocked", "absent"]
    collected_at: Optional[str] = None


# --------------------------------------------------------------------------- #
# Envelope
# --------------------------------------------------------------------------- #
class Envelope(_Base):
    device_id: str
    agent_version: str = CONTRACT_VERSION
    msg_type: MsgType
    ts: str = Field(default_factory=utcnow_iso)
    payload: dict[str, Any] = Field(default_factory=dict)
    # Per-source collection health (Plan 2).  Additive optional field;
    # old servers with extra="allow" silently accept it; missing means no health
    # block from older agents (treated as absent on server side).
    source_health: dict[str, SourceHealth] = Field(default_factory=dict)
    # Site/org identity (W1.1).  Additive optional fields; old agents that omit
    # them produce None here; COALESCE on the server preserves any previously-set
    # value.  CONTRACT_VERSION is deliberately NOT bumped (additive/optional).
    site_code: Optional[str] = None
    site_name: Optional[str] = None
    # Extended org identity (additive optional; COALESCE on server keeps existing values).
    org_code: Optional[str] = None
    dept_code: Optional[str] = None
    comment: Optional[str] = None
    # P1 transport hardening: client-generated UUID4.hex for server-side dedup
    # of retried envelopes.  Additive optional; old agents that omit it are never
    # rejected -- the server just skips dedup for keyless envelopes.
    # max_length=64: UUID4.hex is 32 chars; cap prevents oversized keys from
    # inflating the in-memory dedup dict before the 50k-entry trim fires.
    idempotency_key: Optional[str] = Field(default=None, max_length=64)


class PrintJobRecord(_Base):
    job_id: Optional[int] = None
    ts: str
    printer: str
    pages: int
    size_bytes: Optional[int] = None
    user_name: Optional[str] = None


class PrintJobsPayload(_Base):
    jobs: list[PrintJobRecord] = Field(default_factory=list)
    window_from: Optional[str] = None


_PAYLOAD_MODELS: dict[str, type[_Base]] = {
    "inventory": InventoryPayload,
    "historical": HistoricalPayload,
    "heartbeat": HeartbeatPayload,
    "events": EventBatchPayload,
    "print_jobs": PrintJobsPayload,
}


def parse_payload(msg_type: str, payload: dict[str, Any]) -> _Base:
    """Validate a raw payload dict into its typed model based on msg_type."""
    model = _PAYLOAD_MODELS.get(msg_type)
    if model is None:
        raise ValueError(f"unknown msg_type: {msg_type!r}")
    return model.model_validate(payload)
