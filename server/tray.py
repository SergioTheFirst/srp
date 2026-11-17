"""Трей сервера J-Space: значок, надзор за сервером, автозапуск при входе в Windows.

Спека: docs/superpowers/specs/2026-11-07-server-tray-design.md. Один процесс pythonw в сессии
пользователя держит значок и поток надзора — единственного владельца своего сервера
(`python -m server.main`). Логика покрыта тестами: пути, автозапуск через HKCU Run, опрос
порта, статус значка, надзор, меню и main(). Слой Win32 в конце модуля (окно, значок, мьютекс,
Job Object) проверяется вживую.

Только stdlib, server.config и server.tray_tools (пункты меню без командной строки): трей живёт
долго, и загруженные .pyd fastapi/pydantic/uvicorn мешали бы pip при обновлении (тест
test_tray_imports_nothing_heavy).
"""

from __future__ import annotations

import contextlib
import ctypes
import importlib
import json
import logging
import os
import queue
import socket
import subprocess  # nosec B404 -- свой сервер: постоянный argv, без shell
import sys
import threading
import time
import urllib.request
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import HTTPException
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Optional

from server import tray_tools
from server.config import ServerConfig, load_config

logger = logging.getLogger("srp.tray")

# Не resolve(): значение Run хранит путь в том виде, в каком трей запущен (%~dp0); по нему
# update.ps1 (Get-LoopWrappers) ищет трей — цель junction/subst с корнем не совпала бы.
ROOT = Path(os.path.abspath(__file__)).parents[1]
POLL_SEC = 10  # проверка порта и своего сервера
HTTP_TIMEOUT = 3  # закрытый порт на Windows отвечает отказом за ~2 с
BACKOFF = (3, 10, 30, 60)  # пауза перед перезапуском после падения, с
STABLE_SEC = 600  # проработал столько — счёт падений заново, пауза снова 3 с
START_GRACE = 180  # свой молчит дольше — красный (update.ps1 ждёт /health столько же)
LOCK_TTL = 2 * 3600  # update.lock моложе — идёт обновление, свой сервер не запускать
RUN_NAME = "J-Space Server"
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
_PORT_BUSY = ("10048", "errno 98", "address already in use")  # WinError / Linux / uvicorn


def child_python(root: Path = ROOT, executable: str = sys.executable) -> Path:
    """Интерпретатор сервера: .venv корня, иначе python.exe рядом с pythonw трея.

    Не pythonw: uvicorn под ним падает на sys.stdout.isatty() (stdout = None).
    """
    venv = root / ".venv" / "Scripts" / "python.exe"
    return venv if venv.is_file() else Path(executable).with_name("python.exe")


def autostart_command(root: Path = ROOT, executable: str = sys.executable) -> str:
    """Значение Run: pythonw (без окна) и server_tray.pyw, оба пути в кавычках."""
    pythonw = Path(executable).with_name("pythonw.exe")
    return f'"{pythonw}" "{root / "server_tray.pyw"}"'


def _query(reg: Any, sub: str) -> Any:
    """Значение RUN_NAME в HKCU\\<sub>; None — нет ключа или значения."""
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, sub) as key:
            return reg.QueryValueEx(key, RUN_NAME)[0]
    except OSError:
        return None


def autostart_enabled(reg: Any) -> bool:
    """Галочка «Запускать при входе в Windows»: Run ведёт на этот трей и не отключён.

    reg — модуль winreg (в тестах подделка). Отключённый в Диспетчере задач элемент — первый
    байт значения в Explorer\\StartupApproved\\Run нечётный: Windows его не запустит.
    """
    flags = _query(reg, _APPROVED_KEY)
    disabled = isinstance(flags, bytes) and len(flags) > 0 and flags[0] % 2 == 1
    return str(_query(reg, _RUN_KEY)).lower() == autostart_command().lower() and not disabled


def set_autostart(reg: Any, enabled: bool) -> None:
    """Ставит или убирает автозапуск трея при входе в Windows (HKCU: без прав администратора)."""
    if enabled:
        with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, _RUN_KEY, 0, reg.KEY_SET_VALUE) as key:
            reg.SetValueEx(key, RUN_NAME, 0, reg.REG_SZ, autostart_command())
    # Включение снимает и отметку «Отключено» Диспетчера задач — иначе Windows Run пропустит.
    with contextlib.suppress(OSError):  # значения нет — уже как надо
        sub = _APPROVED_KEY if enabled else _RUN_KEY
        with reg.OpenKey(reg.HKEY_CURRENT_USER, sub, 0, reg.KEY_SET_VALUE) as key:
            reg.DeleteValue(key, RUN_NAME)


def fetch_json(port: int, path: str) -> Optional[dict[str, Any]]:
    """GET http://127.0.0.1:<port><path> мимо прокси; JSON-объект ответа или None.

    Прокси окружения и реестра обходится явно: VPN владельца отвечает 200 на что угодно.
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}{path}", timeout=HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read(1 << 20))
    except (OSError, ValueError, HTTPException):  # нет ответа, код ошибки, не JSON
        return None
    return data if isinstance(data, dict) else None


def crash_reason(code: int, start_log: Path) -> str:
    """Почему свой сервер вышел: «порт занят» по хвосту server-start.log, иначе код выхода."""
    try:
        with start_log.open("rb") as fh:
            fh.seek(max(start_log.stat().st_size - 65536, 0))
            tail = fh.read().decode("utf-8", "replace").lower()
    except OSError:
        tail = ""
    return "порт занят" if any(sign in tail for sign in _PORT_BUSY) else f"код {code}"


@dataclass(frozen=True)
class Snapshot:
    """Итог последней проверки надзора; заменяется целиком — значок читает без замков."""

    answered: bool = False  # /api/v1/health ответил {"status": "ok"} — свой или чужой сервер
    uptime: Optional[float] = None  # время работы своего сервера, с; None — своего нет
    metrics: Optional[dict[str, Any]] = None  # /api/v1/metrics, если сервер ответил
    reason: Optional[str] = None  # почему свой упал: «порт занят», «код 1»
    locked: bool = False  # свежий update.lock — идёт обновление
    beat: bool = False  # вырос fleet.last_seen — пришёл сигнал агента


@dataclass(frozen=True)
class TrayStatus:
    """Что показать: цвет (имя .ico в tray_icons), вспышку, серые строки меню, подсказку."""

    colour: str  # ok | warn | alert
    beat: bool  # показать на секунду beat.ico
    lines: tuple[str, ...]
    tooltip: str  # не длиннее 127 символов (szTip)
    can_restart: bool  # пункт «Перезапустить сервер» активен: чужой сервер трей не трогает


def _dur(sec: float) -> str:
    """Длительность для человека: «15 с», «11 мин», «3 ч 12 мин», «2 д 5 ч»."""
    total = int(sec)
    if total < 60:
        return f"{total} с"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} ч {minutes} мин"
    days, hours = divmod(hours, 24)
    return f"{days} д {hours} ч"


def _age(stamp: Any, now: datetime) -> Optional[float]:
    """Сколько секунд назад было ISO-время stamp; None — времени нет или оно не читается."""
    try:
        return max((now - datetime.fromisoformat(stamp)).total_seconds(), 0.0)
    except (TypeError, ValueError):
        return None


def _state(snap: Snapshot) -> tuple[str, str]:
    """Цвет и первая строка меню: работает, запускается, обновление или почему не отвечает."""
    up = snap.uptime  # None — своего сервера нет
    if snap.answered:
        tail = "(запущен не треем)" if up is None else f"· {_dur(up)}"
        return "ok", f"Сервер работает {tail}"
    if up is not None:  # свой жив, но молчит: поднимается или завис
        hung = up >= START_GRACE
        return ("alert", "Сервер не отвечает") if hung else ("warn", "Сервер запускается")
    if snap.locked:
        return "warn", "Сервер остановлен на время обновления"
    if snap.reason:
        return "alert", f"Сервер не отвечает: {snap.reason}"
    return "warn", "Сервер запускается"


def _fleet_lines(metrics: Any, now: datetime, stale_after: int) -> tuple[bool, list[str]]:
    """Строки о ПК и повод для жёлтого: агенты молчат дольше stale_after или метрик нет."""
    try:
        fleet, ingest = metrics["fleet"], metrics["ingest"]
        total, stale, risk = int(fleet["total"]), int(fleet["stale"]), int(fleet["at_risk"])
        per5 = int(ingest["heartbeats_5m"])
    except (KeyError, TypeError, ValueError):  # метрик нет или формат чужой
        return True, ["Метрики недоступны"]
    age = _age(fleet.get("last_seen"), now)
    seen = "нет данных" if age is None else f"{_dur(age)} назад"
    # Без «из N»: total считает и записи-призраки переустановок, которых дашборд не показывает.
    return total > 0 and (age is None or age > stale_after), [
        f"ПК на связи: {total - stale} · в зоне риска: {risk}",
        f"Последний сигнал агента: {seen}",
        f"Телеметрия: {per5} за 5 мин",
    ]


def tray_status(
    snap: Snapshot, now: datetime, *, port: int, stale_after: int, host: str
) -> TrayStatus:
    """Статус значка из снимка надзора; now — UTC, stale_after — из server.config."""
    colour, state = _state(snap)
    lines = [state]
    if snap.answered:
        silent, fleet = _fleet_lines(snap.metrics, now, stale_after)
        lines += fleet
        colour = "warn" if silent else colour
    lines.append(f"Адрес: http://{host}:{port}")
    return TrayStatus(
        colour=colour,
        beat=snap.beat and colour == "ok",
        lines=tuple(lines),
        tooltip="\n".join(["J-Space сервер", *lines[:3]])[:127],
        can_restart=snap.uptime is not None or not (snap.answered or snap.locked),
    )


class Supervisor:
    """Надзор за своим сервером; единственный владелец сына — поток run().

    Правило: на порту должен отвечать сервер. Отвечает (свой или чужой) — ничего не делать;
    не отвечает две проверки подряд, своего нет, пауза после падения прошла и нет свежего
    update.lock — запустить свой. Меню шлёт команды в очередь commands («restart», «stop»),
    значок читает snapshot. popen/clock/http подменяются в тестах (и обёрткой Job Object).
    """

    def __init__(
        self,
        port: int,
        root: Path = ROOT,
        *,
        popen: Callable[..., Any] = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
        http: Callable[[int, str], Optional[dict[str, Any]]] = fetch_json,
    ) -> None:
        self.port, self.root = port, root
        self._popen, self._clock, self._http = popen, clock, http
        self._logs = root / "logs"
        self.commands: queue.Queue[str] = queue.Queue()
        self.snapshot = Snapshot()
        self._child: Any = None  # свой сервер (Popen); None — своего нет
        self._started = 0.0  # когда запущен свой (по clock)
        self._misses = 0  # проверок подряд без ответа
        self._failures = 0  # падений подряд без 10 минут работы между ними
        self._next_start = 0.0  # раньше не запускать: пауза после падения
        self._reason: Optional[str] = None
        self._seen: Optional[str] = None  # последний fleet.last_seen

    def run(self) -> None:
        """Поток надзора: проверка раз в POLL_SEC, команда меню — сразу; «stop» завершает."""
        cmd: Optional[str] = None
        while True:
            try:
                self.tick(cmd)
            except Exception:  # поток не должен умереть: без него сервер больше не поднимется
                logger.exception("сбой надзора за сервером")
            if cmd == "stop":
                return
            try:
                cmd = self.commands.get(timeout=self._wait())
            except queue.Empty:
                cmd = None

    def tick(self, cmd: Optional[str] = None) -> None:
        """Одна проверка: команда меню, выход своего, ответ порта, запуск, новый снимок."""
        if cmd == "stop":
            self._kill()
            return
        if cmd == "restart":
            self._restart()
        if self._child is not None and self._child.poll() is not None:
            self._fail(crash_reason(self._child.returncode, self._logs / "server-start.log"))
        answered = (self._http(self.port, "/api/v1/health") or {}).get("status") == "ok"
        self._misses = 0 if answered else self._misses + 1
        locked = self._locked()
        if answered:
            self._reason = None
        elif (
            self._child is None
            and self._misses >= 2
            and not locked
            and self._clock() >= self._next_start  # пауза после падения прошла
        ):
            self._start()
        metrics = self._http(self.port, "/api/v1/metrics") if answered else None
        self.snapshot = Snapshot(
            answered=answered,
            uptime=self._clock() - self._started if self._child is not None else None,
            metrics=metrics,
            reason=self._reason,
            locked=locked,
            beat=self._beat(metrics),
        )

    def _wait(self) -> float:
        """Сколько ждать команды до следующей проверки: запуск после паузы — вовремя."""
        left = self._next_start - self._clock()
        return min(POLL_SEC, left) if self._child is None and left > 0 else POLL_SEC

    def _locked(self) -> bool:
        """Свежий update.lock: update.ps1 обновляет (трей другой сессии он не видит)."""
        try:
            return time.time() - (self.root / "update.lock").stat().st_mtime < LOCK_TTL
        except OSError:
            return False

    def _start(self) -> None:
        """Свой сервер без окна; его stdout/stderr до настройки журнала — в server-start.log."""
        env = {
            **os.environ,
            "SRP_LOG_FILE": str(self._logs / "server.log"),  # журнал с ротацией пишет сам сервер
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
            "NoDefaultCurrentDirectoryInExePath": "1",  # .exe не ищется в текущем каталоге
        }
        self._started = self._clock()
        try:
            self._logs.mkdir(exist_ok=True)
            with open(self._logs / "server-start.log", "wb") as out:  # перезапись при запуске
                self._child = self._popen(
                    [str(child_python(self.root)), "-m", "server.main"],
                    cwd=str(self.root),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
        except OSError as exc:
            self._fail(f"не запускается: {exc}")
            return
        logger.info("свой сервер запущен, PID %s", self._child.pid)

    def _fail(self, reason: str) -> None:
        """Свой упал или не запустился: причина и пауза 3/10/30/60 с до следующего запуска."""
        now = self._clock()
        if now - self._started >= STABLE_SEC:
            self._failures = 0
        self._next_start = now + BACKOFF[min(self._failures, len(BACKOFF) - 1)]
        self._failures += 1
        self._reason = reason
        self._child = None
        logger.warning("свой сервер остановился: %s", reason)

    def _restart(self) -> None:
        """«Перезапустить сервер»: свой — убить и сразу поднять; чужой отвечающий не трогаем."""
        if self._child is None and self.snapshot.answered:
            return
        self._kill()
        self._failures, self._reason = 0, None
        if not self._locked():
            self._start()

    def _kill(self) -> None:
        """Останавливает свой сервер: TerminateProcess (SQLite в WAL это переживает)."""
        if self._child is None:
            return
        self._child.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self._child.wait(timeout=5)
        self._child = None

    def _beat(self, metrics: Optional[dict[str, Any]]) -> bool:
        """Вырос ли fleet.last_seen с прошлого ответа: ISO-строки одного сервера сравнимы."""
        fleet = metrics.get("fleet") if metrics else None
        seen = fleet.get("last_seen") if isinstance(fleet, dict) else None
        if not isinstance(seen, str):
            return False
        grew = self._seen is not None and seen > self._seen
        self._seen = seen
        return grew


# --- меню и точки входа; ниже — слой Win32 (проверяется вживую, план T8) -----------------

MF_GRAYED, MF_CHECKED, MF_SEPARATOR = 0x0001, 0x0008, 0x0800
ID_OPEN, ID_RESTART, ID_LOG, ID_AUTOSTART, ID_EXIT = range(1, 6)  # 0 — меню закрыли без выбора


def menu_items(
    st: TrayStatus, *, log_exists: bool, autostart: bool, tools: Iterable[tuple[int, str]]
) -> list[tuple[int, int, Optional[str]]]:
    """Меню значка: (флаги AppendMenuW, команда, текст; None — черта).

    Строки статуса серые. «Перезапустить» серый у чужого сервера и на время обновления (почему —
    в первой строке), «Журнал сервера» — пока журнала нет; галочка — только честный автозапуск.
    """
    return [
        *((MF_GRAYED, 0, line) for line in st.lines),
        (MF_SEPARATOR, 0, None),
        (0, ID_OPEN, "Открыть дашборд"),
        (0 if st.can_restart else MF_GRAYED, ID_RESTART, "Перезапустить сервер"),
        (0 if log_exists else MF_GRAYED, ID_LOG, "Журнал сервера"),
        (MF_CHECKED if autostart else 0, ID_AUTOSTART, "Запускать при входе в Windows"),
        (MF_SEPARATOR, 0, None),
        *((0, cmd, text) for cmd, text in tools),  # tray_tools: пароли, копия, обновление, Caddy
        (MF_SEPARATOR, 0, None),
        (0, ID_EXIT, "Остановить сервер и выйти"),
    ]


def _log_to_file(path: Path) -> None:
    """Журнал трея (1 МБ × 2): у pythonw нет консоли — без файла ошибки трея пропали бы."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=2**20, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def main(
    argv: Optional[list[str]] = None,
    *,
    reg: Any = None,
    run: Optional[Callable[[ServerConfig, Any], int]] = None,
) -> int:
    """Вход server_tray.pyw: журнал logs\\tray.log; --install (server_tray.bat) — автозапуск
    при входе в Windows; затем значок и надзор. reg (winreg) и run (Win32) подменяют тесты.
    """
    _log_to_file(ROOT / "logs" / "tray.log")
    reg = reg if reg is not None else importlib.import_module("winreg")
    if "--install" in (sys.argv[1:] if argv is None else argv):
        try:
            set_autostart(reg, True)
            logger.info("автозапуск при входе в Windows включён: %s", autostart_command())
        except OSError:  # запись запретила политика — сервер всё равно нужен
            logger.exception("автозапуск при входе в Windows не включён")
    return (run or _run_tray)(load_config(), reg)


WM_NULL, WM_DESTROY, WM_TIMER = 0x0000, 0x0002, 0x0113
WM_LBUTTONDBLCLK, WM_RBUTTONUP = 0x0203, 0x0205
WM_TRAY = 0x0400 + 20  # WM_USER + 20: события значка
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
NIIF_INFO, NIIF_ERROR = 0x01, 0x03
TPM_MENU = 0x0102  # TPM_RIGHTBUTTON | TPM_RETURNCMD: выбранный пункт — результатом
MB_ASK, IDYES = 0x10124, 6  # MB_YESNO | MB_ICONQUESTION | MB_DEFBUTTON2 | MB_SETFOREGROUND
ICONS = Path(__file__).resolve().with_name("tray_icons")
BALLOON_PAUSE = 600  # «Сервер не отвечает» — не чаще раза в 10 минут
TITLE = "J-Space сервер"

WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


class NOTIFYICONDATAW(ctypes.Structure):
    """Размер V2: значок, подсказка до 127 символов, всплывающее сообщение."""

    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", ctypes.c_wchar * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", wintypes.DWORD),
    ]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class JOBLIMITS(ctypes.Structure):
    """JOBOBJECT_EXTENDED_LIMIT_INFORMATION; вложенные структуры развёрнуты, раскладка та же."""

    _fields_ = [
        ("UserTimeLimits", ctypes.c_int64 * 2),
        ("LimitFlags", wintypes.DWORD),
        ("WorkingSetSizes", ctypes.c_size_t * 2),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
        ("IoInfo", ctypes.c_uint64 * 6),
        ("MemoryLimits", ctypes.c_size_t * 4),
    ]


_h, _p, _s, _u, _i = wintypes.HANDLE, ctypes.c_void_p, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int
_b, _wp, _lp = wintypes.BOOL, wintypes.WPARAM, wintypes.LPARAM
_lr, _sz = ctypes.c_ssize_t, ctypes.c_size_t
# (результат, аргументы...): без типов ctypes передаёт c_int, и 64-битные HWND/HMENU/HINSTANCE
# переполняются (тот же урок, что в client/tray/icon.py).
_API: dict[str, dict[str, tuple[Any, ...]]] = {
    "user32": {
        "RegisterWindowMessageW": (_u, _s),
        "RegisterClassW": (wintypes.ATOM, _p),
        "CreateWindowExW": (_h, _u, _s, _s, _u, _i, _i, _i, _i, _h, _h, _h, _p),
        "DefWindowProcW": (_lr, _h, _u, _wp, _lp),
        "DestroyWindow": (_b, _h),
        "ChangeWindowMessageFilterEx": (_b, _h, _u, _u, _p),
        "LoadImageW": (_h, _h, _s, _u, _i, _i, _u),
        "SetTimer": (_sz, _h, _sz, _u, _p),
        "CreatePopupMenu": (_h,),
        "AppendMenuW": (_b, _h, _u, _sz, _s),
        "SetMenuDefaultItem": (_b, _h, _u, _u),
        "DestroyMenu": (_b, _h),
        "GetCursorPos": (_b, _p),
        "SetForegroundWindow": (_b, _h),
        "TrackPopupMenu": (_b, _h, _u, _i, _i, _i, _h, _p),
        "PostMessageW": (_b, _h, _u, _wp, _lp),
        "PostQuitMessage": (None, _i),
        "GetMessageW": (_b, _p, _h, _u, _u),
        "TranslateMessage": (_b, _p),
        "DispatchMessageW": (_lr, _p),
        "MessageBoxW": (_i, _h, _s, _s, _u),
    },
    "kernel32": {
        "GetModuleHandleW": (_h, _s),
        "CreateMutexW": (_h, _p, _b, _s),
        "CreateJobObjectW": (_h, _p, _s),
        "SetInformationJobObject": (_b, _h, _i, _p, _u),
        "AssignProcessToJobObject": (_b, _h, _h),
    },
    "shell32": {"Shell_NotifyIconW": (_b, _u, _p)},
}


def _win32() -> SimpleNamespace:  # pragma: no cover
    """Функции _API с объявленными типами, свои экземпляры DLL (use_last_error для кодов)."""
    ns = SimpleNamespace()
    for dll, funcs in _API.items():
        lib = ctypes.WinDLL(dll, use_last_error=True)
        for name, (restype, *argtypes) in funcs.items():
            fn = getattr(lib, name)
            fn.restype, fn.argtypes = restype, argtypes
            setattr(ns, name, fn)
    return ns


def _open(target: str) -> None:  # pragma: no cover
    """Дашборд — в браузере по умолчанию, журнал — в программе для .log (Блокнот)."""
    os.startfile(target)  # nosec B606 -- свой адрес 127.0.0.1 или свой файл журнала


def _first_instance(api: SimpleNamespace) -> bool:  # pragma: no cover
    """Мьютекс сеанса Local\\JSpaceServerTray: Local — у каждого сеанса Windows свой значок."""
    ctypes.set_last_error(0)
    api.CreateMutexW(None, False, "Local\\JSpaceServerTray")  # описатель живёт до выхода
    # 183 — ERROR_ALREADY_EXISTS; 5 — ERROR_ACCESS_DENIED: мьютекс держит трей с другими
    # правами (запущенный от администратора), он тоже «уже есть» (как client/agent.py).
    return ctypes.get_last_error() not in (183, 5)


def _job(api: SimpleNamespace) -> Any:  # pragma: no cover
    """Job Object: трей вышел, упал или убит — Windows закрывает описатель и гасит сервер.

    Только KILL_ON_JOB_CLOSE: с SILENT_BREAKAWAY_OK дети сына уходили бы из-под него.
    """
    job = api.CreateJobObjectW(None, None)
    info = JOBLIMITS(LimitFlags=0x2000)  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    # 9 — JobObjectExtendedLimitInformation
    if job and api.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        return job
    logger.error("Job Object не создан (%s): сервер переживёт трей", ctypes.get_last_error())
    return None


def _popen_in_job(api: SimpleNamespace, job: Any) -> Callable[..., Any]:  # pragma: no cover
    """Popen для Supervisor: каждый свой сервер сразу привязан к Job Object трея."""

    def popen(args: list[str], **kw: Any) -> Any:
        proc: Any = subprocess.Popen(args, **kw)  # nosec B603 -- argv из Supervisor._start
        if job and not api.AssignProcessToJobObject(job, int(proc._handle)):
            logger.error("сервер не привязан к Job Object (%s)", ctypes.get_last_error())
        return proc

    return popen


def _run_tray(cfg: ServerConfig, reg: Any) -> int:  # pragma: no cover
    """Второй экземпляр в сеансе открывает дашборд и выходит; первый — значок и надзор."""
    api = _win32()
    if not _first_instance(api):
        _open(f"http://127.0.0.1:{cfg.port}/")
        return 0
    sup = Supervisor(cfg.port, popen=_popen_in_job(api, _job(api)))
    logger.info("трей запущен: порт %s, PID %s", cfg.port, os.getpid())
    _Tray(api, sup, cfg, reg).run()
    return 0


class _Tray:  # pragma: no cover
    """Невидимое окно и значок; всё — в потоке окна, сына держит поток надзора."""

    def __init__(self, api: SimpleNamespace, sup: Supervisor, cfg: ServerConfig, reg: Any):
        self._api, self._sup, self._reg = api, sup, reg
        self._port, self._stale, self._host = cfg.port, cfg.stale_after_sec, socket.gethostname()
        self._dashboard = f"http://127.0.0.1:{cfg.port}/"
        self._log = sup.root / "logs" / "server.log"
        self._thread = threading.Thread(target=sup.run, name="srp-supervisor", daemon=True)
        self._icons = {  # 16×16 (pythonw не знает DPI); 1 — IMAGE_ICON, 0x10 — LR_LOADFROMFILE
            name: api.LoadImageW(None, str(ICONS / f"{name}.ico"), 1, 16, 16, 0x10)
            for name in ("ok", "beat", "warn", "alert")
        }
        if not all(self._icons.values()):
            logger.error("значки не загружены из %s (%s)", ICONS, ctypes.get_last_error())
        self._added = False  # NIM_ADD принят — только после успеха, иначе повтор на тике
        self._drawn: Optional[tuple[str, str]] = None  # значок и подсказка на экране
        self._flashed: Optional[Snapshot] = None  # снимок, по которому уже была вспышка
        self._greeted, self._down, self._warned = False, False, float("-inf")
        self._taskbar = api.RegisterWindowMessageW("TaskbarCreated")
        self._proc = WNDPROC(self._wndproc)  # ссылка держит обратный вызов живым
        self._hwnd = self._window()

    def run(self) -> None:
        """Поток надзора, значок сразу, затем цикл сообщений до выхода."""
        self._thread.start()
        self._tick()
        msg = wintypes.MSG()
        while self._api.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            self._api.TranslateMessage(ctypes.byref(msg))
            self._api.DispatchMessageW(ctypes.byref(msg))

    def _window(self) -> Any:
        """Невидимое окно (не message-only: тому не приходит TaskbarCreated) и таймер 1 с."""
        api = self._api
        hinst = api.GetModuleHandleW(None)
        wc = WNDCLASSW(lpfnWndProc=self._proc, hInstance=hinst, lpszClassName="JSpaceServerTray")
        api.RegisterClassW(ctypes.byref(wc))
        hwnd = api.CreateWindowExW(
            0, wc.lpszClassName, TITLE, 0, 0, 0, 0, 0, None, None, hinst, None
        )
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        for msg in (self._taskbar, WM_TRAY):  # трей с правами администратора: иначе UIPI
            api.ChangeWindowMessageFilterEx(hwnd, msg, 1, None)  # MSGFLT_ALLOW
        api.SetTimer(hwnd, 1, 1000, None)
        return hwnd

    def _nid(self, flags: int) -> NOTIFYICONDATAW:
        size = ctypes.sizeof(NOTIFYICONDATAW)
        return NOTIFYICONDATAW(
            cbSize=size, hWnd=self._hwnd, uID=1, uFlags=flags, uCallbackMessage=WM_TRAY
        )

    def _show(self, icon: str, tip: str) -> None:
        """Значок с подсказкой: ADD, пока не принят (повтор на следующем тике), затем MODIFY."""
        if self._added and self._drawn == (icon, tip):
            return
        nid = self._nid(NIF_MESSAGE | NIF_ICON | NIF_TIP)
        nid.hIcon, nid.szTip = self._icons[icon], tip
        notify = self._api.Shell_NotifyIconW
        ok = notify(NIM_MODIFY if self._added else NIM_ADD, ctypes.byref(nid))
        if not ok and not self._added:  # ADD не дождался ответа проводника, а значок уже есть
            ok = notify(NIM_MODIFY, ctypes.byref(nid))
        self._added = bool(ok)  # отказ MODIFY (проводник упал) — снова ADD на следующем тике
        if ok:
            self._drawn = (icon, tip)

    def _balloon(self, text: str, level: int) -> bool:
        """Всплывающее сообщение значка (Windows 10/11 показывает его уведомлением)."""
        nid = self._nid(NIF_INFO)
        nid.szInfoTitle, nid.szInfo, nid.dwInfoFlags = TITLE, text[:255], level
        return self._added and bool(self._api.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid)))

    def _state(self) -> tuple[Snapshot, TrayStatus]:
        snap = self._sup.snapshot
        now = datetime.now(timezone.utc)
        st = tray_status(snap, now, port=self._port, stale_after=self._stale, host=self._host)
        return snap, st

    def _tick(self) -> None:
        """Раз в секунду: значок и подсказка, вспышка ~1 с на сигнал агента, сообщения."""
        snap, st = self._state()
        flash = st.beat and snap is not self._flashed  # одна секунда на снимок надзора
        if flash:
            self._flashed = snap
        self._show("beat" if flash else st.colour, st.tooltip)
        if snap.answered and not self._greeted:
            address = f"http://{self._host}:{self._port}"
            self._greeted = self._balloon(f"Сервер запущен — дашборд: {address}", NIIF_INFO)
        down, now = st.colour == "alert", time.monotonic()
        fell = down and not self._down and now - self._warned >= BALLOON_PAUSE
        if fell and self._balloon(st.lines[0], NIIF_ERROR):
            self._warned = now
        self._down = down

    def _menu(self) -> None:
        """Меню: SetForegroundWindow до и WM_NULL после — иначе оно не закрывается щелчком мимо."""
        api, hwnd = self._api, self._hwnd
        st = self._state()[1]
        log, auto = self._log.is_file(), autostart_enabled(self._reg)
        items = menu_items(st, log_exists=log, autostart=auto, tools=tray_tools.items(ROOT))
        menu = api.CreatePopupMenu()
        for flags, cmd, text in items:
            api.AppendMenuW(menu, flags, cmd, text)
        api.SetMenuDefaultItem(menu, ID_OPEN, 0)  # жирный — он же по двойному щелчку
        pt = wintypes.POINT()
        api.GetCursorPos(ctypes.byref(pt))
        api.SetForegroundWindow(hwnd)
        chosen = api.TrackPopupMenu(menu, TPM_MENU, pt.x, pt.y, 0, hwnd, None)
        api.PostMessageW(hwnd, WM_NULL, 0, 0)
        api.DestroyMenu(menu)
        self._command(chosen)

    def _command(self, cmd: int) -> None:
        if cmd == ID_OPEN:
            _open(self._dashboard)
        elif cmd == ID_RESTART:
            self._sup.commands.put("restart")
        elif cmd == ID_LOG:
            _open(str(self._log))
        elif cmd == ID_AUTOSTART:
            set_autostart(self._reg, not autostart_enabled(self._reg))
        elif cmd == ID_EXIT:
            self._exit()
        else:  # пункты server/tray_tools.py; итог копии базы — всплывающим сообщением
            tray_tools.command(cmd, child_python(), ROOT, self._note)

    def _note(self, text: str, ok: bool) -> None:
        self._balloon(text, NIIF_INFO if ok else NIIF_ERROR)

    def _exit(self) -> None:
        """«Остановить сервер и выйти» — после подтверждения; сервер гасит поток надзора."""
        text = (
            "Остановить сервер J-Space и убрать значок?\n\n"
            "Пока сервер остановлен, агенты копят данные и передадут их после запуска."
        )
        if self._api.MessageBoxW(self._hwnd, text, TITLE, MB_ASK) != IDYES:
            return
        self._sup.commands.put("stop")
        self._thread.join(timeout=15)
        self._api.DestroyWindow(self._hwnd)  # WM_DESTROY: значок убран, цикл завершён

    def _wndproc(self, hwnd: Any, msg: int, wparam: int, lparam: int) -> int:
        try:
            if self._on_message(msg, lparam & 0xFFFF):
                return 0
        except Exception:  # сбой обработчика не должен ронять цикл сообщений и значок
            logger.exception("сбой обработки сообщения %#x окна трея", msg)
            return 0
        return int(self._api.DefWindowProcW(hwnd, msg, wparam, lparam))

    def _on_message(self, msg: int, event: int) -> bool:
        """Своё сообщение — обработать (True); остальные — DefWindowProcW (False)."""
        if msg == WM_TIMER:
            self._tick()
        elif msg == WM_TRAY and event == WM_LBUTTONDBLCLK:  # одиночный щелчок — ничего
            _open(self._dashboard)
        elif msg == WM_TRAY and event == WM_RBUTTONUP:
            self._menu()
        elif msg == self._taskbar:  # проводник перезапущен: значок заново, последнее состояние
            self._added = False
            if self._drawn:
                self._show(*self._drawn)
        elif msg == WM_DESTROY:
            self._api.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid(0)))
            self._api.PostQuitMessage(0)
        else:
            return msg == WM_TRAY
        return True
