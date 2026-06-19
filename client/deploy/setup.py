r"""SRP one-command installer (tray spec §6) -- the brain of ``srp-setup.exe``.

A technician double-clicks a per-department ``.bat`` (UAC elevates via the exe
manifest) and the agent + tray are deployed to ``C:\SRP`` with zero questions:
copy payload -> lock down the ACL -> merge config -> validate a live pass ->
register a SYSTEM scheduled task + the tray Run key -> start.

The pure, testable parts (argument parsing, validation, config merge, and the
exact privileged command argv) live up top; the Windows-only orchestration that
shells out to robocopy/icacls/schtasks/reg/wevtutil is a thin shell at the
bottom. The agent/tray code stays pure stdlib -- PyInstaller is a build-only dep.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import shutil

# subprocess: every call uses a static argv list (shell=False) built from
# validated codes / fixed literals -- no user string is ever shell-interpreted.
import subprocess  # nosec B404
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

from client.config import hash_password

# Exit codes (spec §6 diagnostics -- read by RMM/GPO).
EXIT_OK = 0
EXIT_OTHER = 1
EXIT_BAD_PARAMS = 2
EXIT_SERVER_UNREACHABLE = 3
EXIT_COPY_ACL = 4
EXIT_TASK_AUTOSTART = 5

DEST = r"C:\SRP"
SPOOL_DIR = "spool"  # user-writable subdir for the tray's personal-cert spool (stage 8)
TASK_NAME = "SRP Agent"
RUN_KEY = r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "SRP-Tray"
PRINT_LOG = "Microsoft-Windows-PrintService/Operational"
AGENT_EXE = "srp-agent.exe"
TRAY_EXE = "srp-tray.exe"
TASK_XML = "task_template.xml"
TEMPLATE = "config.template.json"
CONFIG = "config.json"
INSTALL_LOG = "install.log"
_ROBOCOPY_OK_MAX = 7  # robocopy returns a bitmask; 0-7 are success, >=8 is failure

_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,16}$")
# config.json files robocopy must never overwrite/delete (not part of the payload)
_KEEP_FILES = (CONFIG, INSTALL_LOG, "srp-agent.log", "tray.log")


@dataclass(frozen=True)
class SetupOptions:
    server: str = ""
    org: str = ""
    dept: str = ""
    password: str = ""
    token: str = ""
    helpdesk: str = ""
    comment: str = ""
    no_tray: bool = False
    allow_offline: bool = False
    quiet: bool = False
    uninstall: bool = False
    purge: bool = False


class SetupError(Exception):
    """A validation failure carrying the spec §6 exit code to return."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# argument parsing + auto-quiet
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str]) -> SetupOptions:
    p = argparse.ArgumentParser(prog="srp-setup", description="SRP one-command installer")
    p.add_argument("--server", default="")
    p.add_argument("--org", default="")
    p.add_argument("--dept", default="")
    p.add_argument("--password", default="")
    p.add_argument("--token", default="")
    p.add_argument("--helpdesk", default="")
    p.add_argument("--comment", default="")
    p.add_argument("--no-tray", action="store_true", dest="no_tray")
    p.add_argument("--allow-offline", action="store_true", dest="allow_offline")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--uninstall", action="store_true")
    p.add_argument("--purge", action="store_true")
    a = p.parse_args(argv)
    # Auto-quiet: a fully-specified command runs unattended (spec §6).
    quiet = a.quiet or (bool(a.server) and bool(a.org))
    return SetupOptions(
        server=a.server,
        org=a.org,
        dept=a.dept,
        password=a.password,
        token=a.token,
        helpdesk=a.helpdesk,
        comment=a.comment,
        no_tray=a.no_tray,
        allow_offline=a.allow_offline,
        quiet=quiet,
        uninstall=a.uninstall,
        purge=a.purge,
    )


def validate(opts: SetupOptions, *, template_has_server: bool = False) -> None:
    """Raise :class:`SetupError` (exit 2) on bad/incomplete install parameters."""
    if opts.uninstall:
        return
    if not _CODE_RE.match(opts.org):
        raise SetupError(
            EXIT_BAD_PARAMS, "код организации обязателен (формат ^[A-Za-z0-9_-]{1,16}$)"
        )
    if opts.dept and not _CODE_RE.match(opts.dept):
        raise SetupError(EXIT_BAD_PARAMS, "код подразделения неверного формата")
    if not opts.server and not template_has_server:
        raise SetupError(
            EXIT_BAD_PARAMS, "укажите --server (или server_url в config.template.json)"
        )


# --------------------------------------------------------------------------- #
# config merge + write
# --------------------------------------------------------------------------- #


def merge_config(template: dict, existing: dict, opts: SetupOptions) -> dict:
    """org-policy template < prior machine config < explicit parameters.

    ``device_id`` and any other prior field survive (idempotent upgrades); the
    password is stored only as a PBKDF2 hash, never in plaintext.
    """
    cfg = dict(template)
    cfg.update(existing)  # prior machine values (device_id, ...) beat policy defaults
    if opts.server:
        cfg["server_url"] = opts.server
    cfg["org_code"] = opts.org
    if opts.dept:
        cfg["dept_code"] = opts.dept
    if opts.token:
        cfg["ingest_token"] = opts.token
    if opts.helpdesk:
        cfg["helpdesk_contact"] = opts.helpdesk
    if opts.comment:
        cfg["comment"] = opts.comment
    if opts.password:
        cfg["config_password_hash"] = hash_password(opts.password)
    return cfg


def write_config_no_bom(path: Path, cfg: dict) -> None:
    """Write config.json as UTF-8 *without* a BOM (the agent's json.loads chokes on one)."""
    Path(path).write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------- #
# privileged command argv (pure data -- the orchestration runs these, shell=False)
# --------------------------------------------------------------------------- #


def robocopy_cmd(payload: str, dest: str = DEST) -> list[str]:
    # /E copies the tree but NOT /MIR -- mirroring would delete config.json + logs
    # (which are not in the payload) on the second run. Logs/config are excluded.
    return [
        "robocopy",
        payload,
        dest,
        "/E",
        "/MT",
        "/R:2",
        "/W:2",
        "/NP",
        "/NJH",
        "/NJS",
        "/XF",
        *_KEEP_FILES,
    ]


def icacls_cmd(dest: str = DEST) -> list[str]:
    # The C:\ default ACL grants ordinary Users WRITE; drop inheritance and grant
    # explicit ACEs so only SYSTEM/Admins can write -- the real protection for
    # config.json (the tray password is only a UI-layer guard).
    #
    # Use well-known SIDs, NOT English names: on a localised Windows (e.g. Russian)
    # "Administrators"/"Users" don't resolve and icacls fails (-> exit 4). SIDs are
    # language-independent. SYSTEM=S-1-5-18, Administrators=S-1-5-32-544,
    # Users=S-1-5-32-545.
    return [
        "icacls",
        dest,
        "/inheritance:r",
        "/grant:r",
        "*S-1-5-18:(OI)(CI)F",
        "*S-1-5-32-544:(OI)(CI)F",
        "*S-1-5-32-545:(OI)(CI)RX",
    ]


def icacls_spool_cmd(dest: str = DEST) -> list[str]:
    # Stage 8: the per-user tray (non-admin) must write its personal-cert spool into
    # C:\SRP\spool, which inherits Users:RX from the locked-down root. ADD (not /grant:r)
    # an Authenticated-Users Modify ACE on this ONE subdir so the tray can write; the
    # rest of C:\SRP stays read-only to users. The agent treats the spool as hostile
    # input and strictly validates it (client/collectors/user_certs.py).
    return ["icacls", str(Path(dest) / SPOOL_DIR), "/grant", "*S-1-5-11:(OI)(CI)M"]


def schtasks_create_cmd(xml_path: Path, task_name: str = TASK_NAME) -> list[str]:
    return ["schtasks", "/create", "/tn", task_name, "/xml", str(xml_path), "/f"]


def schtasks_start_cmd(task_name: str = TASK_NAME) -> list[str]:
    return ["schtasks", "/run", "/tn", task_name]


def schtasks_stop_cmd(task_name: str = TASK_NAME) -> list[str]:
    return ["schtasks", "/end", "/tn", task_name]


def schtasks_delete_cmd(task_name: str = TASK_NAME) -> list[str]:
    return ["schtasks", "/delete", "/tn", task_name, "/f"]


def reg_add_run_cmd(tray_exe: str) -> list[str]:
    return ["reg", "add", RUN_KEY, "/v", RUN_VALUE, "/t", "REG_SZ", "/d", tray_exe, "/f"]


def reg_delete_run_cmd() -> list[str]:
    return ["reg", "delete", RUN_KEY, "/v", RUN_VALUE, "/f"]


def wevtutil_enable_cmd() -> list[str]:
    return ["wevtutil", "sl", PRINT_LOG, "/e:true"]


def taskkill_tray_cmd() -> list[str]:
    return ["taskkill", "/im", TRAY_EXE, "/f"]


# --------------------------------------------------------------------------- #
# Windows-only orchestration (thin; not unit-tested -- it touches the real OS)
# --------------------------------------------------------------------------- #


def _log(dest: str, msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line)
    try:
        with open(Path(dest) / INSTALL_LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _run(cmd: list[str], *, dest: Optional[str] = None, label: str = "") -> int:
    """Run a fixed argv (shell=False); on failure log stderr when *dest* is given.

    robocopy/icacls/schtasks/reg/wevtutil never echo the token or password, so
    logging their stderr is safe and turns a silent exit code into real triage.
    """
    proc = subprocess.run(cmd, capture_output=True, text=True)  # nosec B603
    if proc.returncode != 0 and dest is not None:
        err = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")[:300]
        _log(dest, f"{label or cmd[0]} rc={proc.returncode}: {err}")
    return proc.returncode


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def template_has_server(payload_dir: Path) -> bool:
    """True if the org template (share root, next to setup.exe) bakes in a server_url.

    Lets ``--server`` be optional when policy already carries it (spec §6 table);
    ``merge_config`` then flows that template ``server_url`` into config.json.
    """
    template = _load_json(payload_dir.parent / TEMPLATE)
    return bool(str(template.get("server_url", "")).strip())


def _config_loadable(path: Path) -> bool:
    """True if the written config parses and carries a server_url (local, no network).

    Gates autostart registration on an offline install so we never leave a SYSTEM
    task pointing at a config the agent could not even start from.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and bool(str(data.get("server_url", "")).strip())


def reencode_task_xml_utf16(text: str) -> str:
    """schtasks /create /xml needs UTF-16; flip the shipped UTF-8 declaration.

    Pure: callers write the result with a UTF-16 codec (BOM). Without this,
    schtasks fails with "failed to switch encoding" on a UTF-8 task file.
    """
    return text.replace('encoding="UTF-8"', 'encoding="UTF-16"', 1)


def _write_task_xml_utf16(path: Path) -> None:
    path.write_text(reencode_task_xml_utf16(path.read_text(encoding="utf-8")), encoding="utf-16")


def _payload_dir() -> Path:
    base = (
        Path(sys.executable).parent
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parents[2]
    )
    return base / "payload"


def run_install(opts: SetupOptions, *, payload: Path, dest: str = DEST) -> int:
    destp = Path(dest)
    destp.mkdir(parents=True, exist_ok=True)
    _log(dest, f"install start org={opts.org} dept={opts.dept or '-'} no_tray={opts.no_tray}")

    if _run(robocopy_cmd(str(payload), dest)) > _ROBOCOPY_OK_MAX:
        _log(dest, "robocopy failed")
        return EXIT_COPY_ACL
    if _run(icacls_cmd(dest)) != 0:
        _log(dest, "icacls failed")
        return EXIT_COPY_ACL

    # Stage 8 spool dir: best-effort (supplementary feature must not fail the install).
    try:
        (destp / SPOOL_DIR).mkdir(parents=True, exist_ok=True)
        if _run(icacls_spool_cmd(dest)) != 0:
            _log(dest, "spool ACL grant failed (personal-cert spool disabled)")
    except OSError as exc:
        _log(dest, f"spool dir create failed: {exc}")

    # config.template.json sits at the share root (next to setup.exe), so a
    # technician can edit org policy without rebuilding; payload.parent = that root.
    template = _load_json(payload.parent / TEMPLATE) or _load_json(destp / TEMPLATE)
    cfg = merge_config(template, _load_json(destp / CONFIG), opts)
    write_config_no_bom(destp / CONFIG, cfg)
    _log(dest, "config.json written (UTF-8, no BOM)")

    _run(wevtutil_enable_cmd())  # print-log: best effort, never fatal

    if not opts.allow_offline:
        # Live collect+send check. Runs in THIS elevated-admin context, not SYSTEM
        # (accepted gap): the common failures -- wrong URL, network down -- reproduce
        # here; a per-user proxy/VPN edge is the only difference. The agent EXE is
        # windowed (no console -> stderr is None), so route its diagnostics to a log
        # file and read a redacted tail from there for RMM/GPO triage (the agent
        # already redacts the URL and never prints the token).
        val_log = destp / "validate.log"
        proc = subprocess.run(  # nosec B603 -- fixed path, no user-controlled argv
            [str(destp / AGENT_EXE), "--once", "--log-file", str(val_log)],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            tail = ""
            with contextlib.suppress(OSError):
                tail = val_log.read_text(encoding="utf-8", errors="replace")
            tail = (tail or proc.stderr or proc.stdout or "").strip().replace("\n", " ")[-300:]
            _log(dest, f"validation pass failed rc={proc.returncode}: {tail}")
            return EXIT_SERVER_UNREACHABLE

    # Never register a SYSTEM autostart pointing at a config the agent cannot start
    # from (no server_url) -- this applies to offline installs too (offline != no URL).
    if not _config_loadable(destp / CONFIG):
        _log(dest, "config.json has no server_url -- autostart not registered")
        return EXIT_BAD_PARAMS

    try:
        _write_task_xml_utf16(destp / TASK_XML)  # schtasks /xml requires UTF-16
    except OSError as exc:
        _log(dest, f"task XML re-encode failed: {exc}")
    if _run(schtasks_create_cmd(destp / TASK_XML)) != 0:
        _log(dest, "schtasks create failed")
        return EXIT_TASK_AUTOSTART
    if not opts.no_tray and _run(reg_add_run_cmd(str(destp / TRAY_EXE))) != 0:
        _log(dest, "Run key failed -- rolling back the scheduled task")
        _run(schtasks_delete_cmd())  # no orphan SYSTEM autostart on partial failure
        return EXIT_TASK_AUTOSTART

    _run(schtasks_start_cmd())
    if not opts.no_tray:
        subprocess.Popen([str(destp / TRAY_EXE)])  # nosec B603 -- launch tray in this session
    _report(dest, opts)
    return EXIT_OK


def run_uninstall(opts: SetupOptions, *, dest: str = DEST) -> int:
    _run(schtasks_stop_cmd())
    _run(taskkill_tray_cmd())
    _run(schtasks_delete_cmd())
    _run(reg_delete_run_cmd())
    if opts.purge:
        shutil.rmtree(dest, ignore_errors=True)
    _log(dest, f"uninstall done (purge={opts.purge})")
    return EXIT_OK


def _report(dest: str, opts: SetupOptions) -> None:
    _log(dest, "установка завершена")
    print(f"  сервер: {opts.server or '(из шаблона)'}")
    print(f"  организация: {opts.org}" + (f" / отдел {opts.dept}" if opts.dept else ""))
    print("  агент: задача планировщика (SYSTEM, при старте)")
    print("  трей: " + ("отключён (--no-tray)" if opts.no_tray else "запущен в этой сессии"))


def _interactive(opts: SetupOptions) -> SetupOptions:
    """Fallback when run with no parameters: ask the two required values."""
    try:
        server = opts.server or input("Адрес сервера (например http://192.168.1.10:8000): ").strip()
        org = opts.org or input("Код организации: ").strip()
    except EOFError:
        return opts
    return replace(opts, server=server, org=org)


def main(argv: Optional[list[str]] = None) -> int:
    opts = parse_args(sys.argv[1:] if argv is None else argv)
    if not opts.quiet and not opts.uninstall and (not opts.server or not opts.org):
        opts = _interactive(opts)
    payload = _payload_dir()
    try:
        validate(opts, template_has_server=template_has_server(payload))
    except SetupError as exc:
        print(f"[setup] {exc.message}", file=sys.stderr)
        return exc.code
    if opts.uninstall:
        return run_uninstall(opts)
    try:
        return run_install(opts, payload=payload)
    except OSError as exc:
        print(f"[setup] {exc}", file=sys.stderr)
        return EXIT_OTHER


if __name__ == "__main__":
    sys.exit(main())
