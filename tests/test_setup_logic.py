"""One-command installer logic (tray spec §6).

Pure, off-Windows tests for ``client.deploy.setup``: argument parsing + auto-quiet,
org/dept validation and exit codes, the config merge (device_id preserved, params
beat the org template, password stored as a PBKDF2 hash -- never plaintext), the
UTF-8-no-BOM write, and the exact privileged command argv (robocopy/icacls/
schtasks/reg/wevtutil) -- compared as data, never executed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from client.config import verify_password
from client.deploy import setup as su

# --------------------------------------------------------------------------- #
# argument parsing + auto-quiet
# --------------------------------------------------------------------------- #


def test_parse_args_full_command() -> None:
    opts = su.parse_args(
        [
            "--server",
            "http://192.168.1.10:8000",
            "--org",
            "101",
            "--dept",
            "7",
            "--password",
            "Zx9!",
            "--token",
            "sek",
            "--helpdesk",
            "IT: 1234",
            "--comment",
            "reception PC",
        ]
    )
    assert opts.server == "http://192.168.1.10:8000"
    assert opts.org == "101" and opts.dept == "7"
    assert opts.password == "Zx9!" and opts.token == "sek"
    assert opts.helpdesk == "IT: 1234" and opts.comment == "reception PC"
    assert opts.quiet is True  # server + org both present -> auto-quiet


def test_parse_args_auto_quiet_needs_both_server_and_org() -> None:
    assert su.parse_args(["--server", "http://x", "--org", "1"]).quiet is True
    assert su.parse_args(["--server", "http://x"]).quiet is False  # no org
    assert su.parse_args(["--org", "1"]).quiet is False  # no server


def test_parse_args_flags() -> None:
    opts = su.parse_args(["--server", "http://x", "--org", "1", "--no-tray", "--allow-offline"])
    assert opts.no_tray is True and opts.allow_offline is True


def test_parse_args_uninstall_purge() -> None:
    opts = su.parse_args(["--uninstall", "--purge"])
    assert opts.uninstall is True and opts.purge is True


# --------------------------------------------------------------------------- #
# validation + exit codes
# --------------------------------------------------------------------------- #


def test_validate_accepts_good_codes() -> None:
    su.validate(su.parse_args(["--server", "http://x", "--org", "ACME-01", "--dept", "7"]))


def test_validate_missing_org_is_exit_2() -> None:
    with pytest.raises(su.SetupError) as ei:
        su.validate(su.parse_args(["--server", "http://x"]))
    assert ei.value.code == su.EXIT_BAD_PARAMS


@pytest.mark.parametrize("bad", ["", "has space", "toolongcode_01234", "semi;colon", 'quote"x'])
def test_validate_rejects_bad_org(bad: str) -> None:
    with pytest.raises(su.SetupError) as ei:
        su.validate(su.SetupOptions(server="http://x", org=bad))
    assert ei.value.code == su.EXIT_BAD_PARAMS


def test_validate_rejects_bad_dept_but_allows_empty() -> None:
    su.validate(su.SetupOptions(server="http://x", org="1", dept=""))  # empty dept ok
    with pytest.raises(su.SetupError):
        su.validate(su.SetupOptions(server="http://x", org="1", dept="bad dept"))


def test_validate_server_required_unless_template_supplies_it() -> None:
    with pytest.raises(su.SetupError) as ei:
        su.validate(su.SetupOptions(org="1"))  # no server, no template
    assert ei.value.code == su.EXIT_BAD_PARAMS
    su.validate(su.SetupOptions(org="1"), template_has_server=True)  # template provides it


def test_validate_uninstall_needs_no_params() -> None:
    su.validate(su.SetupOptions(uninstall=True))  # must not raise


# --------------------------------------------------------------------------- #
# config merge
# --------------------------------------------------------------------------- #


def _template() -> dict:
    return {
        "heartbeat_interval_sec": 300,
        "tray_cert_warn_days": 14,
        "tray_notify_hours": 4,
        "helpdesk_contact": "IT default",
        "site_code": "HQ",
    }


def test_merge_preserves_device_id_and_applies_params() -> None:
    existing = {"device_id": "agent-abc123", "server_url": "http://old"}
    opts = su.SetupOptions(server="http://new:8000", org="101", dept="7", comment="lab")
    cfg = su.merge_config(_template(), existing, opts)
    assert cfg["device_id"] == "agent-abc123"  # identity preserved
    assert cfg["server_url"] == "http://new:8000"  # param beats existing + template
    assert cfg["org_code"] == "101" and cfg["dept_code"] == "7"
    assert cfg["comment"] == "lab"
    assert cfg["tray_cert_warn_days"] == 14  # org policy from template survives


def test_merge_hashes_password_never_plaintext() -> None:
    cfg = su.merge_config({}, {}, su.SetupOptions(server="http://x", org="1", password="S3cr!t"))
    assert "S3cr!t" not in json.dumps(cfg)  # plaintext never stored
    assert cfg["config_password_hash"].startswith("pbkdf2:")
    assert verify_password("S3cr!t", cfg["config_password_hash"])


def test_merge_helpdesk_param_overrides_template() -> None:
    cfg = su.merge_config(
        _template(), {}, su.SetupOptions(server="http://x", org="1", helpdesk="IT: 555")
    )
    assert cfg["helpdesk_contact"] == "IT: 555"


def test_merge_keeps_template_helpdesk_when_param_absent() -> None:
    cfg = su.merge_config(_template(), {}, su.SetupOptions(server="http://x", org="1"))
    assert cfg["helpdesk_contact"] == "IT default"


def test_merge_token_only_set_when_given() -> None:
    cfg = su.merge_config({}, {"ingest_token": "keep"}, su.SetupOptions(server="http://x", org="1"))
    assert cfg["ingest_token"] == "keep"  # existing token not wiped by empty param


# --------------------------------------------------------------------------- #
# template_has_server (wires the spec §6 "--server may come from the template")
# --------------------------------------------------------------------------- #


def _share_with_template(tmp_path: Path, template: dict) -> Path:
    """Build a share/payload layout and return the payload dir (template at root)."""
    payload = tmp_path / "share" / "payload"
    payload.mkdir(parents=True)
    (tmp_path / "share" / "config.template.json").write_text(json.dumps(template), encoding="utf-8")
    return payload


def test_template_has_server_true_when_baked(tmp_path: Path) -> None:
    payload = _share_with_template(tmp_path, {"server_url": "http://hq:8000"})
    assert su.template_has_server(payload) is True


def test_template_has_server_false_when_blank_or_absent(tmp_path: Path) -> None:
    assert (
        su.template_has_server(_share_with_template(tmp_path / "a", {"server_url": "  "})) is False
    )
    assert (
        su.template_has_server(_share_with_template(tmp_path / "b", {"site_code": "HQ"})) is False
    )


def test_template_has_server_false_when_file_missing(tmp_path: Path) -> None:
    payload = tmp_path / "share" / "payload"
    payload.mkdir(parents=True)  # no config.template.json written
    assert su.template_has_server(payload) is False


# --------------------------------------------------------------------------- #
# _config_loadable -- gates SYSTEM-autostart registration (no orphan tasks)
# --------------------------------------------------------------------------- #


def test_config_loadable_true_with_server_url(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    su.write_config_no_bom(p, {"server_url": "http://x:8000", "org_code": "1"})
    assert su._config_loadable(p) is True


def test_config_loadable_false_without_server_url(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    su.write_config_no_bom(p, {"org_code": "1"})  # no server_url -> agent can't start
    assert su._config_loadable(p) is False


def test_config_loadable_false_on_missing_or_corrupt_file(tmp_path: Path) -> None:
    assert su._config_loadable(tmp_path / "nope.json") is False
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json", encoding="utf-8")
    assert su._config_loadable(bad) is False


# --------------------------------------------------------------------------- #
# UTF-8 no BOM write
# --------------------------------------------------------------------------- #


def test_write_config_no_bom(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    su.write_config_no_bom(p, {"server_url": "http://x", "comment": "Ромашка"})
    raw = p.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")  # no BOM (agent json.loads would choke)
    assert json.loads(raw.decode("utf-8"))["comment"] == "Ромашка"  # round-trips Cyrillic


# --------------------------------------------------------------------------- #
# privileged command argv (data, never executed)
# --------------------------------------------------------------------------- #


def test_icacls_closes_user_write() -> None:
    cmd = su.icacls_cmd(r"C:\SRP")
    assert cmd[0] == "icacls" and r"C:\SRP" in cmd
    assert "/inheritance:r" in cmd  # drop the inherited C:\ ACL (Users can write by default)
    joined = " ".join(cmd)
    assert "SYSTEM:" in joined and "Administrators:" in joined
    users = [a for a in cmd if a.startswith("Users:")][0]
    assert "RX" in users and "F" not in users and "W" not in users and "M" not in users


def test_icacls_spool_grants_authenticated_users_write_on_subdir_only() -> None:
    cmd = su.icacls_spool_cmd(r"C:\SRP")
    assert cmd[0] == "icacls"
    assert cmd[1].endswith("spool") and cmd[1] != r"C:\SRP"  # the subdir, not the locked root
    assert "/grant" in cmd and "/grant:r" not in cmd  # ADD an ACE, don't replace the root's
    assert any("S-1-5-11" in a for a in cmd)  # Authenticated Users SID
    assert any("(OI)(CI)M" in a for a in cmd)  # Modify, inheritable


def test_robocopy_does_not_mirror_delete_config() -> None:
    cmd = su.robocopy_cmd(r"\\srv\srp$\payload", r"C:\SRP")
    assert cmd[0] == "robocopy"
    assert "/MIR" not in cmd  # mirror would delete config.json/logs on re-run
    assert "config.json" in cmd  # excluded from the copy


def test_schtasks_uses_embedded_xml() -> None:
    cmd = su.schtasks_create_cmd(r"C:\SRP\task_template.xml", "SRP Agent")
    assert cmd[:2] == ["schtasks", "/create"]
    assert "/xml" in cmd and r"C:\SRP\task_template.xml" in cmd
    assert "/tn" in cmd and "SRP Agent" in cmd and "/f" in cmd


def test_reg_run_key_points_at_tray() -> None:
    cmd = su.reg_add_run_cmd(r"C:\SRP\srp-tray.exe")
    assert cmd[:2] == ["reg", "add"]
    assert any("Run" in a for a in cmd) and r"C:\SRP\srp-tray.exe" in cmd
    assert su.reg_delete_run_cmd()[:2] == ["reg", "delete"]


def test_wevtutil_enables_print_log() -> None:
    cmd = su.wevtutil_enable_cmd()
    assert cmd[0] == "wevtutil" and "/e:true" in cmd
    assert any("PrintService/Operational" in a for a in cmd)


def test_uninstall_commands_present() -> None:
    assert su.schtasks_delete_cmd()[:2] == ["schtasks", "/delete"]
    assert su.taskkill_tray_cmd()[0] == "taskkill"
    assert "srp-tray.exe" in su.taskkill_tray_cmd()


# --------------------------------------------------------------------------- #
# shipped org template is valid policy (no secrets / no identity)
# --------------------------------------------------------------------------- #


def test_config_template_is_policy_only() -> None:
    path = Path(__file__).resolve().parents[1] / "client" / "deploy" / "config.template.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "tray_cert_warn_days" in data and "tray_notify_hours" in data
    # policy only: no machine identity, no secrets baked into the shared template
    for forbidden in ("device_id", "ingest_token", "config_password_hash"):
        assert forbidden not in data or data[forbidden] == ""
