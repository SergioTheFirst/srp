"""tkinter panel + password dialog (tray spec §2 + §4), run as a child process.

The tray icon (a Win32 message loop) and tkinter's ``mainloop`` cannot share a
thread, so the panel and the password prompt are launched as a separate
``srp-tray --panel`` / ``--ask-password`` process: a crash here never drops the
icon. All text is decided by :mod:`client.tray.state`; this module only places it
into widgets. The certificate row is filled by stage 5 (``certs.py``); until then
it shows a neutral placeholder.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from client.status_writer import candidate_ips
from client.tray import state as st


def read_config_bits(config_path: Path) -> tuple[str, str]:
    """(password_hash, helpdesk_contact) read-only -- never writes config.json.

    The tray must not trigger config.py's device_id-persist side effect, and the
    file may be ACL-restricted; a read failure degrades to empty strings.
    """
    try:
        data = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ("", "")
    if not isinstance(data, dict):
        return ("", "")
    return (str(data.get("config_password_hash", "")), str(data.get("helpdesk_contact", "")))


def _server_url(config_path: Path) -> str:
    try:
        data = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(data.get("server_url", "")) if isinstance(data, dict) else ""


def live_ip(config_path: Path, view: st.StatusView) -> str:
    """Panel IP, discovered live so it is right even if the agent is dead (spec §2).

    Reuses the agent's RFC1918-only ``candidate_ips`` (no new egress, no public
    address can leak); falls back to the agent's last-written IP if discovery
    finds nothing (e.g. the interface is down).
    """
    found = candidate_ips(_server_url(config_path))
    return found[0] if found else st.fmt_ip(view)


def _panel_rows(
    view: st.StatusView, helpdesk: str, *, cert_text: str, ip: str
) -> list[tuple[str, str, bool]]:
    """(label, value, warn?) rows for the panel, in spec §2 order."""
    disk_text, disk_warn = st.fmt_disk(view)
    up_text, up_hint = st.fmt_uptime(view)
    return [
        ("Организация / отдел", st.fmt_org_dept(view), False),
        ("IP в сети", ip, False),
        ("Сертификат", cert_text, False),
        ("Связь", st.fmt_link(view, now=time.time()), bool(view.last_error)),
        ("Печать", st.fmt_print(view), False),
        ("Диск C:", disk_text, disk_warn),
        ("Без перезагрузки", up_text, up_hint),
        ("Агент", f"версия {view.agent_version}", False),
        ("Поддержка", helpdesk or "—", False),
    ]


def run_panel(*, status_path: Path, config_path: Path, cert_text: str = "—") -> None:
    """Show the single-page status panel (blocks until the window is closed)."""
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title("SRP — состояние компьютера")
    root.resizable(False, False)
    outer = ttk.Frame(root, padding=16)
    outer.grid()
    body = ttk.Frame(outer)
    body.grid(row=0, column=0)

    def render() -> None:
        for child in body.winfo_children():
            child.destroy()
        view = st.read_status(status_path, now=time.time())
        _, helpdesk = read_config_bits(config_path)
        if view is None:
            ttk.Label(body, text="Агент ещё не запускался или не отвечает.").grid(sticky="w")
            return
        ip = live_ip(config_path, view)
        rows = _panel_rows(view, helpdesk, cert_text=cert_text, ip=ip)
        for i, (label, value, warn) in enumerate(rows):
            ttk.Label(body, text=label + ":", width=20, anchor="w").grid(
                row=i, column=0, sticky="w", pady=2
            )
            value_label = ttk.Label(body, text=value, anchor="w")
            if warn:
                value_label.configure(foreground="#b00020")
            value_label.grid(row=i, column=1, sticky="w", pady=2)

    def copy_support() -> None:
        view = st.read_status(status_path, now=time.time())
        if view is None:
            return
        root.clipboard_clear()
        root.clipboard_append(
            st.support_clipboard(view, cert_text=cert_text, ip=live_ip(config_path, view))
        )

    render()
    btns = ttk.Frame(outer)
    btns.grid(row=1, column=0, sticky="e", pady=(14, 0))
    ttk.Button(btns, text="Скопировать для поддержки", command=copy_support).grid(
        row=0, column=0, padx=4
    )
    ttk.Button(btns, text="Обновить", command=render).grid(row=0, column=1, padx=4)
    root.mainloop()


def run_password_prompt(
    *, config_path: Path, tray_state_path: Path, reason: str = "Выход из SRP"
) -> int:
    """Modal password prompt. Returns 0 if the correct password was entered, else 1.

    No password configured -> 0 (nothing to guard). Honours the persisted 3x/5-min
    lockout (spec §4). Verification + lockout bookkeeping is pure
    (:func:`state.check_password`); this only renders the dialog.
    """
    password_hash, _ = read_config_bits(config_path)
    if not password_hash:
        return 0  # no password set -> allow

    import tkinter as tk
    from tkinter import messagebox, ttk

    result = {"code": 1}
    root = tk.Tk()
    root.title("SRP")
    frame = ttk.Frame(root, padding=16)
    frame.grid()
    ttk.Label(frame, text=f"{reason}. Введите пароль:").grid(
        row=0, column=0, columnspan=2, sticky="w"
    )
    entry = ttk.Entry(frame, show="•", width=28)
    entry.grid(row=1, column=0, columnspan=2, pady=10)
    entry.focus_set()

    def submit() -> None:
        gate = st.load_gate(tray_state_path)
        locked = st.is_locked(gate, now=time.time())
        if locked is not None:
            messagebox.showwarning(
                "SRP", f"Слишком много попыток. Подождите {int(locked // 60) + 1} мин."
            )
            return
        ok, gate = st.check_password(password_hash, entry.get(), gate, now=time.time())
        st.save_gate(tray_state_path, gate)
        if ok:
            result["code"] = 0
            root.destroy()
            return
        entry.delete(0, tk.END)
        if st.is_locked(gate, now=time.time()) is not None:
            messagebox.showwarning("SRP", "Неверный пароль. Ввод заблокирован на 5 минут.")
        else:
            messagebox.showerror("SRP", "Неверный пароль.")

    ttk.Button(frame, text="OK", command=submit).grid(row=2, column=0, sticky="e", padx=4)
    ttk.Button(frame, text="Отмена", command=root.destroy).grid(row=2, column=1, sticky="w", padx=4)
    root.bind("<Return>", lambda _evt: submit())
    root.mainloop()
    return result["code"]
