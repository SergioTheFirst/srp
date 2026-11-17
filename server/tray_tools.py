"""Пункты меню значка сервера: пароли, копия базы, обновление, Caddy — без командной строки.

План: docs/superpowers/plans/2026-11-07-tray-tools-plan.md; батники остаются запасным путём.
Окно паролей — отдельный процесс (`python -m server.tray_tools`): трей не грузит server.db
(pydantic мешал бы pip при обновлении, test_tray_imports_nothing_heavy) и не замирает, пока
окно открыто. Вверху модуля только stdlib: tkinter грузит лишь процесс окна.
"""

from __future__ import annotations

import ctypes
import os
import subprocess  # nosec B404 -- свои модули и батники: постоянный argv, без shell
import threading
from pathlib import Path
from typing import Any, Callable, Optional

ID_USERS, ID_BACKUP, ID_UPDATE, ID_CADDY = range(6, 10)  # 1-5 — команды server/tray.py
ITEMS = (
    (ID_USERS, "Пароли для входа из интернета…"),
    (ID_BACKUP, "Резервная копия базы"),
    (ID_UPDATE, "Обновить сервер…"),
    (ID_CADDY, "Вход из интернета (Caddy)…"),
)
_BATS = {ID_UPDATE: "update.bat", ID_CADDY: "install-caddy.bat"}
TITLE = "Пароли для входа из интернета"


def items(root: Path) -> list[tuple[int, str]]:
    """Пункты ITEMS для меню; нет своего батника (публичная сборка без update.bat) — нет пункта."""
    return [(cmd, text) for cmd, text in ITEMS if cmd not in _BATS or (root / _BATS[cmd]).is_file()]


def backup(python: Path, root: Path, run: Callable[..., Any] = subprocess.run) -> tuple[str, bool]:
    """Копия базы без окна (`server.backup --quiet` печатает только путь): текст и успех."""
    try:
        res = run(
            [str(python), "-m", "server.backup", "--quiet"],
            cwd=str(root),
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except OSError as exc:  # нет python.exe
        return f"Копия базы не создана: {exc}", False
    if res.returncode == 0:
        return f"Копия базы сохранена: {res.stdout.strip()}", True
    why = (res.stderr.strip().splitlines() or [f"код {res.returncode}"])[-1]
    return f"Копия базы не создана: {why.removeprefix('копия не создана: ')}", False


def command(
    cmd: int,
    python: Path,
    root: Path,
    notify: Callable[[str, bool], Any],
    *,
    popen: Callable[..., Any] = subprocess.Popen,
    start: Callable[[str], Any] = os.startfile,
    run: Callable[..., Any] = subprocess.run,
) -> None:
    """Пункт ITEMS: окно паролей и копия — без консоли; батники — в окне (вопросы, UAC)."""
    if cmd == ID_USERS:  # свой процесс: значок не ждёт, пока окно открыто
        popen(
            [str(python), "-m", "server.tray_tools"],
            cwd=str(root),
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    elif cmd == ID_BACKUP:  # в потоке: копия большой базы не морозит значок
        notify("Снимаю копию базы…", True)  # сразу: иначе второй щелчок — вторая копия
        threading.Thread(target=lambda: notify(*backup(python, root, run)), daemon=True).start()
    elif cmd in _BATS:  # update.ps1 сам снимет и снова запустит трей
        start(str(root / _BATS[cmd]))


class _Passwords:  # pragma: no cover -- окно проверяется вживую
    """Окно паролей: записи списком и кнопки; работа — функции users.py и db.py, как у users.bat."""

    def __init__(self, tk: Any) -> None:
        from server import db, users

        self.tk, self.db, self.users = tk, db, users
        self.names: dict[str, str] = {}  # строка списка -> имя записи
        self.root = root = tk.Tk()
        root.title(TITLE)
        root.report_callback_exception = lambda *exc: self._say(f"Не вышло: {exc[1]}", "showerror")
        self.tree = tk.ttk.Treeview(
            root, columns=("state", "since"), height=12, selectmode="browse"
        )
        for col, text, width in (
            ("#0", "Имя", 180),
            ("state", "Состояние", 90),
            ("since", "Пароль от", 90),
        ):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width)
        self.tree.pack(side="left", fill="both", expand=True, padx=8, pady=8)
        bar = tk.ttk.Frame(root)
        bar.pack(side="right", fill="y", padx=(0, 8), pady=8)
        tk.ttk.Label(bar, text="Имя новой записи:").pack(anchor="w")
        self.name = tk.ttk.Entry(bar)
        self.name.pack(fill="x")
        self.name.bind("<Return>", lambda _event: self._add())
        for text, cmd in (
            ("Добавить", self._add),
            ("Новый пароль", self._picked(self._new_password)),
            ("Отключить", self._picked(lambda u: db.set_user_disabled(u["id"], True))),
            ("Включить", self._picked(lambda u: db.set_user_disabled(u["id"], False))),
            ("Выйти со всех устройств", self._picked(self._logout)),
            ("Закрыть", root.destroy),
        ):
            tk.ttk.Button(bar, text=text, command=cmd).pack(fill="x", pady=2)
        self._refresh()

    def _say(self, text: str, kind: str = "showinfo") -> Any:
        return getattr(self.tk.messagebox, kind)(TITLE, text, parent=self.root)

    def _selected(self) -> Optional[str]:
        sel = self.tree.selection()
        return self.names.get(sel[0]) if sel else None

    def _refresh(self) -> None:
        """Список заново из базы; выбранная запись остаётся выбранной."""
        keep = self._selected()
        self.tree.delete(*self.tree.get_children())
        self.names = {}
        for u in self.db.list_users():
            state = "отключена" if u["disabled"] else "активна"
            since = u["password_changed_at"][:10]
            iid = self.tree.insert("", "end", text=u["username"], values=(state, since))
            self.names[iid] = u["username"]
            if u["username"] == keep:
                self.tree.selection_set(iid)

    def _picked(self, action: Callable[[dict[str, Any]], Any]) -> Callable[[], None]:
        """Кнопка над выбранной записью: нет выбора — подсказка; после — список заново."""

        def run() -> None:
            name = self._selected()
            user = self.db.get_user(name) if name else None
            if user is None:
                self._say("Выберите запись в списке.")
                return
            action(user)
            self._refresh()

        return run

    def _add(self) -> None:
        # Имя — как в базе и в списке (casefold), иначе в окне пароля «Иванов», в списке «иванов».
        name = self.users._norm(self.name.get())
        password = self.users.add_user(name)
        if password is None:
            self._say(f"Запись «{name}» уже есть." if name else "Впишите имя записи.", "showerror")
            return
        self.name.delete(0, "end")
        self._refresh()
        self._show(name, password)

    def _new_password(self, user: dict[str, Any]) -> None:
        name = user["username"]
        ask = f"Новый пароль для «{name}»? Старый перестанет работать, входы записи погаснут."
        if self._say(ask, "askyesno"):
            self._show(name, self.users.reset_password(user["id"]))

    def _logout(self, user: dict[str, Any]) -> None:
        gone = self.db.delete_sessions(user["id"])
        self._say(f"Погашено входов записи «{user['username']}»: {gone}.")

    def _show(self, name: str, password: str) -> None:
        """Новый пароль — один раз: поле только для чтения и «Копировать»."""
        ttk, top = self.tk.ttk, self.tk.Toplevel(self.root)
        top.title(TITLE)
        top.transient(self.root)
        text = f"Пароль записи «{name}» показывается один раз — передайте его лично:"
        ttk.Label(top, text=text).pack(padx=12, pady=(12, 6))
        field = ttk.Entry(top, font=("Consolas", 14), justify="center")
        field.insert(0, password)
        field.configure(state="readonly")
        field.pack(fill="x", padx=12)

        def copy() -> None:
            top.clipboard_clear()
            top.clipboard_append(password)

        ttk.Button(top, text="Копировать", command=copy).pack(side="left", padx=12, pady=12)
        ttk.Button(top, text="Закрыть", command=top.destroy).pack(side="right", padx=12, pady=12)


def main() -> int:  # pragma: no cover -- окно и Win32 проверяются вживую
    """`python -m server.tray_tools`: окно паролей; не открылось — почему и запасной путь."""
    try:
        import tkinter
        import tkinter.messagebox
        import tkinter.ttk

        from server import db
        from server.config import load_config

        db.init_db(load_config().resolved_db_path())
        _Passwords(tkinter).root.mainloop()
    except Exception as exc:  # нет tkinter (embeddable Python), база или конфиг недоступны
        text = f"Окно паролей не открылось: {exc}\n\nЗапасной путь — users.bat в папке сервера."
        ctypes.WinDLL("user32").MessageBoxW(None, text, TITLE, 0x30)  # MB_ICONWARNING
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
