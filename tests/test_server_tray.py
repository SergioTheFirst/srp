"""Трей сервера (spec 2026-11-07-server-tray-design.md): иконки, статус и надзор."""

from __future__ import annotations

import contextlib
import http.server
import logging
import os
import runpy
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pytest
from server import tray, tray_tools

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]
_ICONS = _ROOT / "server" / "tray_icons"
_RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"
_APPROVED = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
_NAME = "J-Space Server"
_NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("state", ["ok", "beat", "warn", "alert"])
def test_icon_is_one_16x16_32bit_image(state: str) -> None:
    """Каждое состояние — .ico с ОДНИМ кадром 16×16, 32 бит (альфа): трей грузит ровно его."""
    data = (_ICONS / f"{state}.ico").read_bytes()
    assert struct.unpack_from("<HHH", data, 0) == (0, 1, 1)  # reserved, icon, один кадр
    width, height, _, _, planes, bpp, size, offset = struct.unpack_from("<BBBBHHII", data, 6)
    assert (width, height, planes, bpp) == (16, 16, 1, 32)
    assert (offset, size) == (22, 40 + 16 * 16 * 4 + 16 * 4)  # заголовок + BGRA + маска
    assert offset + size == len(data)
    assert struct.unpack_from("<IiiHH", data, offset) == (40, 16, 32, 1, 32)


def test_tray_imports_nothing_heavy() -> None:
    """Трей живёт долго: загруженные .pyd fastapi/pydantic мешали бы pip при обновлении."""
    code = (
        "import sys, server.tray; heavy = {'fastapi', 'pydantic', 'pydantic_core', 'uvicorn',"
        " 'starlette'}; print(sorted(m for m in sys.modules if m.split('.')[0] in heavy))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert out.stdout.strip() == "[]"


# --- пути и автозапуск -----------------------------------------------------------------


def test_child_python_prefers_root_venv(tmp_path: Path) -> None:
    """Сын — python.exe из .venv корня, иначе python.exe рядом с pythonw трея (не pythonw)."""
    assert tray.child_python(tmp_path, r"C:\Py\pythonw.exe") == Path(r"C:\Py\python.exe")
    venv = tmp_path / ".venv" / "Scripts" / "python.exe"
    venv.parent.mkdir(parents=True)
    venv.write_bytes(b"")
    assert tray.child_python(tmp_path, r"C:\Py\pythonw.exe") == venv


def test_autostart_command_quotes_both_paths() -> None:
    """Значение Run: оба пути в кавычках (пробелы, кириллица), всегда pythonw — без окна."""
    root = Path(r"C:\Program Files\J-Space сервер")
    cmd = tray.autostart_command(root, r"C:\Py 3\python.exe")
    assert cmd == r'"C:\Py 3\pythonw.exe" "C:\Program Files\J-Space сервер\server_tray.pyw"'


class _FakeReg:
    """winreg в памяти: ключ — dict значений; открытый ключ — тот же dict."""

    HKEY_CURRENT_USER = "HKCU"
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self) -> None:
        self.keys: dict[str, dict[str, Any]] = {}

    def OpenKey(self, root: str, sub: str, reserved: int = 0, access: int = 0) -> Any:
        assert root == "HKCU"  # только свой профиль: права администратора не нужны
        if sub not in self.keys:
            raise FileNotFoundError(sub)
        return contextlib.nullcontext(self.keys[sub])

    def CreateKeyEx(self, root: str, sub: str, reserved: int = 0, access: int = 0) -> Any:
        assert root == "HKCU"
        return contextlib.nullcontext(self.keys.setdefault(sub, {}))

    def QueryValueEx(self, key: dict[str, Any], name: str) -> tuple[Any, int]:
        if name not in key:
            raise FileNotFoundError(name)
        return key[name], 1

    def SetValueEx(self, key: dict[str, Any], name: str, res: int, kind: int, value: Any) -> None:
        key[name] = value

    def DeleteValue(self, key: dict[str, Any], name: str) -> None:
        if name not in key:
            raise FileNotFoundError(name)
        del key[name]


def test_autostart_toggle_is_honest() -> None:
    """Галочка — только когда Run ведёт на ЭТОТ трей и Диспетчер задач его не отключил."""
    reg = _FakeReg()
    assert not tray.autostart_enabled(reg)
    tray.set_autostart(reg, True)
    assert reg.keys[_RUN][_NAME] == tray.autostart_command()
    assert tray.autostart_enabled(reg)
    # «Отключено» в Диспетчере задач — первый байт StartupApproved нечётный.
    reg.keys[_APPROVED] = {_NAME: b"\x03" + bytes(11)}
    assert not tray.autostart_enabled(reg)
    reg.keys[_APPROVED][_NAME] = b"\x02" + bytes(11)
    assert tray.autostart_enabled(reg)
    reg.keys[_APPROVED][_NAME] = b"\x03" + bytes(11)
    tray.set_autostart(reg, True)  # включение из меню снимает отметку Диспетчера
    assert _NAME not in reg.keys[_APPROVED]
    assert tray.autostart_enabled(reg)
    tray.set_autostart(reg, False)
    assert _NAME not in reg.keys[_RUN]
    assert not tray.autostart_enabled(reg)
    tray.set_autostart(reg, False)  # уже выключен — не ошибка
    reg.keys[_RUN][_NAME] = r'"C:\Py\pythonw.exe" "D:\old copy\server_tray.pyw"'
    assert not tray.autostart_enabled(reg)  # чужая копия — не наш автозапуск


# --- надзор ----------------------------------------------------------------------------


class _Proc:
    """Поддельный Popen: жив, пока тест не задаст returncode; kill — как TerminateProcess."""

    pid = 4242

    def __init__(self, args: list[str], **kw: Any) -> None:
        self.args, self.kw = args, kw
        self.returncode: Optional[int] = None
        self.killed = False

    def poll(self) -> Optional[int]:
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        if self.returncode is None:
            self.returncode = 1

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        return self.returncode


class _Rig:
    """Надзор с поддельными часами, опросом порта и Popen — без процессов и сети."""

    def __init__(self, root: Path) -> None:
        self.now = 1000.0
        self.health: Optional[dict[str, Any]] = None  # None — порт молчит
        self.metrics: Optional[dict[str, Any]] = None
        self.procs: list[_Proc] = []
        self.spawn_error: Optional[OSError] = None  # Popen не смог запустить процесс
        self.sup = tray.Supervisor(
            18123, root, popen=self._popen, clock=lambda: self.now, http=self._http
        )

    def _popen(self, args: list[str], **kw: Any) -> _Proc:
        if self.spawn_error is not None:
            raise self.spawn_error
        self.procs.append(_Proc(args, **kw))
        return self.procs[-1]

    def _http(self, port: int, path: str) -> Optional[dict[str, Any]]:
        assert port == 18123
        return self.health if path == "/api/v1/health" else self.metrics

    def tick(self, sec: float = 10, cmd: Optional[str] = None) -> None:
        self.now += sec
        self.sup.tick(cmd)

    def status(self) -> tray.TrayStatus:
        return tray.tray_status(self.sup.snapshot, _NOW, port=18123, stale_after=600, host="SRV")


def _metrics(**fleet: Any) -> dict[str, Any]:
    base = {"total": 55, "stale": 3, "at_risk": 2, "last_seen": "2026-11-07T11:59:45+00:00"}
    return {"fleet": {**base, **fleet}, "ingest": {"heartbeats_5m": 128}}


def test_starts_own_server_after_two_misses(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    rig.tick()
    assert rig.procs == []  # один промах: сервер мог перезапускаться сам (окно, обновление)
    rig.tick()
    (proc,) = rig.procs
    assert proc.args == [str(tray.child_python(tmp_path)), "-m", "server.main"]
    assert proc.kw["cwd"] == str(tmp_path)
    assert proc.kw["creationflags"] & subprocess.CREATE_NO_WINDOW
    env = proc.kw["env"]
    assert env["SRP_LOG_FILE"] == str(tmp_path / "logs" / "server.log")
    assert env["PYTHONUNBUFFERED"] == "1" and env["PYTHONIOENCODING"] == "utf-8"
    assert env["NoDefaultCurrentDirectoryInExePath"] == "1"
    assert Path(proc.kw["stdout"].name) == tmp_path / "logs" / "server-start.log"
    assert proc.kw["stderr"] == subprocess.STDOUT
    st = rig.status()
    assert (st.colour, st.lines[0], st.can_restart) == ("warn", "Сервер запускается", True)
    rig.tick()
    assert len(rig.procs) == 1  # свой ещё поднимается — второго не запускаем


def test_foreign_server_is_left_alone(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    rig.health = {"status": "ok"}
    for _ in range(5):
        rig.tick()
    assert rig.procs == []
    st = rig.status()
    assert (st.lines[0], st.can_restart) == ("Сервер работает (запущен не треем)", False)
    rig.tick(cmd="restart")  # чужой сервер трей не трогает
    assert rig.procs == []
    rig.health = {"status": "proxy"}  # 200 от посторонней программы — не наш сервер
    rig.tick()
    rig.tick()
    assert len(rig.procs) == 1


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("OSError: [WinError 10048] Обычно разрешается только одно использование", "порт занят"),
        ("OSError: [Errno 98] Address already in use", "порт занят"),
        ("ERROR: address already in use", "порт занят"),
        ("RuntimeError: lifespan failed", "код 3"),
        (None, "код 3"),
    ],
)
def test_crash_reason(tmp_path: Path, text: Optional[str], reason: str) -> None:
    log = tmp_path / "server-start.log"
    if text is not None:
        log.write_text(f"Traceback (most recent call last):\n{text}\n", encoding="utf-8")
    assert tray.crash_reason(3, log) == reason


def test_crash_restarts_with_growing_pause(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    rig.tick()
    rig.tick()
    (tmp_path / "logs" / "server-start.log").write_text("[WinError 10048]", encoding="utf-8")
    rig.procs[-1].returncode = 1
    rig.tick(1)
    st = rig.status()
    assert (st.colour, st.lines[0]) == ("alert", "Сервер не отвечает: порт занят")
    assert rig.sup._wait() == pytest.approx(3)  # пауза 3 с — будим вовремя, не через 10
    for pause in (3, 10, 30, 60, 60):
        count = len(rig.procs)
        rig.tick(pause - 0.5)
        assert len(rig.procs) == count  # пауза ещё идёт
        rig.tick(0.5)
        assert len(rig.procs) == count + 1
        rig.procs[-1].returncode = 3
        rig.tick(1)
    assert rig.status().lines[0] == "Сервер не отвечает: код 3"  # журнал запуска перезаписан
    rig.tick(60)  # пауза прошла — свой снова запущен
    rig.tick(600)  # и проработал 10 минут: после падения пауза снова 3 с
    rig.procs[-1].returncode = 1
    rig.tick(1)
    count = len(rig.procs)
    rig.tick(3)
    assert len(rig.procs) == count + 1


def test_spawn_failure_is_red_with_pause(tmp_path: Path) -> None:
    """Нет python.exe — красный с причиной и пауза, а не падение потока или цикл без паузы."""
    rig = _Rig(tmp_path)
    rig.spawn_error = FileNotFoundError(2, "нет файла", "python.exe")
    rig.tick()
    rig.tick()
    st = rig.status()
    assert (st.colour, st.lines[0]) == (
        "alert",
        "Сервер не отвечает: не запускается: [Errno 2] нет файла: 'python.exe'",
    )
    assert rig.sup._wait() == pytest.approx(3)
    rig.spawn_error = None
    rig.tick(3)
    assert len(rig.procs) == 1


def test_update_lock_blocks_start(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    lock = tmp_path / "update.lock"
    lock.write_text("")
    for _ in range(3):
        rig.tick()
    rig.tick(cmd="restart")
    assert rig.procs == []
    st = rig.status()
    assert (st.colour, st.lines[0]) == ("warn", "Сервер остановлен на время обновления")
    assert not st.can_restart
    stale = time.time() - 2 * 3600 - 60
    os.utime(lock, (stale, stale))  # брошенный замок старше 2 ч не держит
    rig.tick()
    assert len(rig.procs) == 1


def test_restart_and_stop_commands(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    rig.tick()
    rig.tick()
    rig.health = {"status": "ok"}
    rig.tick()
    assert rig.status().can_restart
    rig.tick(0, cmd="restart")
    assert rig.procs[0].killed and len(rig.procs) == 2  # убит и сразу запущен новый
    rig.health = None
    rig.procs[-1].returncode = 1
    rig.tick(1)
    rig.tick(0, cmd="restart")
    assert len(rig.procs) == 3  # ручной перезапуск паузы после падения не ждёт
    rig.sup.commands.put("stop")
    rig.sup.run()  # «stop» гасит своего сервера и завершает поток надзора
    assert rig.procs[-1].killed and len(rig.procs) == 3


def test_beat_when_agent_signal_grows(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    rig.health = {"status": "ok"}
    rig.metrics = _metrics(last_seen="2026-11-07T11:59:00+00:00")
    rig.tick()
    assert not rig.sup.snapshot.beat  # сравнивать не с чем
    rig.metrics = _metrics(last_seen="2026-11-07T11:59:30+00:00")
    rig.tick()
    assert rig.sup.snapshot.beat
    rig.tick()
    assert not rig.sup.snapshot.beat


# --- статус значка ---------------------------------------------------------------------


def _status(snap: tray.Snapshot) -> tray.TrayStatus:
    return tray.tray_status(snap, _NOW, port=8000, stale_after=600, host="SRV")


def test_status_of_a_live_server() -> None:
    snap = tray.Snapshot(
        answered=True, uptime=3 * 3600 + 12 * 60 + 5, metrics=_metrics(), beat=True
    )
    st = _status(snap)
    assert (st.colour, st.beat, st.can_restart) == ("ok", True, True)
    assert st.lines == (
        "Сервер работает · 3 ч 12 мин",
        "ПК на связи: 52 · в зоне риска: 2",
        "Последний сигнал агента: 15 с назад",
        "Телеметрия: 128 за 5 мин",
        "Адрес: http://SRV:8000",
    )
    assert st.tooltip == "\n".join(("J-Space сервер", *st.lines[:3]))
    assert len(st.tooltip) <= 127


def test_status_yellow_when_agents_are_silent() -> None:
    silent = _status(
        tray.Snapshot(
            answered=True, metrics=_metrics(last_seen="2026-11-07T11:49:00+00:00"), beat=True
        )
    )
    assert (silent.colour, silent.beat) == ("warn", False)
    assert silent.lines[2] == "Последний сигнал агента: 11 мин назад"
    no_pcs = _status(
        tray.Snapshot(answered=True, metrics=_metrics(total=0, stale=0, last_seen=None))
    )
    assert no_pcs.colour == "ok"  # ПК ещё нет — жаловаться не на что
    broken = _status(tray.Snapshot(answered=True, metrics={"fleet": "мусор"}))
    assert (broken.colour, broken.lines[1]) == ("warn", "Метрики недоступны")


def test_status_red_and_tooltip_cap() -> None:
    down = _status(tray.Snapshot(reason="не запускается: " + "x" * 200))
    assert (down.colour, down.can_restart) == ("alert", True)
    assert len(down.tooltip) == 127
    hung = _status(tray.Snapshot(uptime=181))  # свой жив, но 3 минуты не отвечает
    assert (hung.colour, hung.lines[0]) == ("alert", "Сервер не отвечает")


# --- опрос порта -----------------------------------------------------------------------


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = {"/api/v1/health": b'{"status":"ok"}', "/list": b"[1]", "/html": b"<p>"}.get(
            self.path
        )
        self.send_response(200 if body else 500)
        self.end_headers()
        self.wfile.write(body or b"")

    def log_message(self, *args: Any) -> None:
        pass


def test_fetch_json_goes_direct_and_wants_an_object(monkeypatch: pytest.MonkeyPatch) -> None:
    """Прокси из окружения (VPN) не перехватывает 127.0.0.1; не-объект и ошибка — None."""
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        assert tray.fetch_json(port, "/api/v1/health") == {"status": "ok"}
        assert tray.fetch_json(port, "/list") is None
        assert tray.fetch_json(port, "/html") is None
        assert tray.fetch_json(port, "/missing") is None
    finally:
        srv.shutdown()
        srv.server_close()


# --- меню и точки входа (окно Win32 проверяется вживую) ---------------------------------


def test_menu_shows_status_lines_then_commands() -> None:
    """Строки статуса — серые; чужой сервер не перезапустить (почему — в первой строке)."""
    foreign = _status(tray.Snapshot(answered=True, metrics=_metrics()))
    items = tray.menu_items(foreign, log_exists=False, autostart=True, tools=tray_tools.ITEMS)
    count = len(foreign.lines)
    assert items[:count] == [(tray.MF_GRAYED, 0, line) for line in foreign.lines]
    assert items[count:] == [
        (tray.MF_SEPARATOR, 0, None),
        (0, tray.ID_OPEN, "Открыть дашборд"),
        (tray.MF_GRAYED, tray.ID_RESTART, "Перезапустить сервер"),
        (tray.MF_GRAYED, tray.ID_LOG, "Журнал сервера"),  # журнала ещё нет
        (tray.MF_CHECKED, tray.ID_AUTOSTART, "Запускать при входе в Windows"),
        (tray.MF_SEPARATOR, 0, None),
        (0, tray_tools.ID_USERS, "Пароли для входа из интернета…"),
        (0, tray_tools.ID_BACKUP, "Резервная копия базы"),
        (0, tray_tools.ID_UPDATE, "Обновить сервер…"),
        (0, tray_tools.ID_CADDY, "Вход из интернета (Caddy)…"),
        (tray.MF_SEPARATOR, 0, None),
        (0, tray.ID_EXIT, "Остановить сервер и выйти"),
    ]
    commands = [cmd for _, cmd, _ in items if cmd]
    assert len(commands) == len(set(commands))  # команды трея и tray_tools не пересекаются
    own = _status(tray.Snapshot(answered=True, uptime=60, metrics=_metrics()))
    menu = tray.menu_items(own, log_exists=True, autostart=False, tools=())
    flags = {cmd: f for f, cmd, _ in menu}
    assert flags[tray.ID_RESTART] == flags[tray.ID_LOG] == flags[tray.ID_AUTOSTART] == 0


def test_tool_items_need_their_bat(tmp_path: Path) -> None:
    """Пункт батника — только если батник есть: в публичной сборке нет update.bat."""
    (tmp_path / "install-caddy.bat").write_bytes(b"")
    assert tray_tools.items(tmp_path) == [
        item for item in tray_tools.ITEMS if item[0] != tray_tools.ID_UPDATE
    ]
    (tmp_path / "update.bat").write_bytes(b"")
    assert tray_tools.items(tmp_path) == list(tray_tools.ITEMS)


def _done(code: int, out: str = "", err: str = "") -> Any:
    return lambda argv, **kw: subprocess.CompletedProcess(argv, code, out, err)


def test_backup_runs_quiet_without_a_window(tmp_path: Path) -> None:
    """«Резервная копия базы»: server.backup --quiet без окна, utf-8; итог — для всплывающего."""
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(argv: list[str], **kw: Any) -> Any:
        calls.append((argv, kw))
        return _done(0, "C:\\SRP\\backups\\srp-1.db\n")(argv)

    text, ok = tray_tools.backup(Path("py.exe"), tmp_path, run)
    assert (text, ok) == ("Копия базы сохранена: C:\\SRP\\backups\\srp-1.db", True)
    argv, kw = calls[0]
    assert argv == ["py.exe", "-m", "server.backup", "--quiet"]
    assert kw["cwd"] == str(tmp_path) and kw["creationflags"] == subprocess.CREATE_NO_WINDOW
    assert kw["env"]["PYTHONIOENCODING"] == kw["encoding"] == "utf-8"


def _no_python(argv: list[str], **kw: Any) -> Any:
    raise FileNotFoundError(2, "Не удаётся найти указанный файл")


@pytest.mark.parametrize(
    ("run", "text"),
    [
        (_done(1, err="копия не создана: database is locked\n"), "database is locked"),
        (_done(1, err="Traceback ...\nValueError: битый конфиг\n"), "ValueError: битый конфиг"),
        (_done(3), "код 3"),
        (_no_python, "[Errno 2] Не удаётся найти указанный файл"),
    ],
)
def test_backup_failure_says_why(tmp_path: Path, run: Any, text: str) -> None:
    assert tray_tools.backup(Path("py.exe"), tmp_path, run) == (
        f"Копия базы не создана: {text}",
        False,
    )


def test_tool_commands(tmp_path: Path) -> None:
    """Окно паролей — отдельный процесс без консоли (значок не ждёт); батники — в видимом окне
    (вопросы, UAC); копия — в потоке, итог — notify; чужая команда — ничего."""
    spawned: list[tuple[list[str], dict[str, Any]]] = []
    opened: list[str] = []
    notes: list[tuple[str, bool]] = []
    done = threading.Event()

    def notify(text: str, ok: bool) -> None:
        notes.append((text, ok))
        if len(notes) == 2:  # «снимаю» сразу, итог — из потока
            done.set()

    py = tmp_path / "python.exe"
    for cmd in (tray_tools.ID_USERS, tray_tools.ID_UPDATE, tray_tools.ID_CADDY, 0):
        tray_tools.command(
            cmd,
            py,
            tmp_path,
            notify,
            popen=lambda a, **k: spawned.append((a, k)),
            start=opened.append,
        )
    no_window = {"cwd": str(tmp_path), "creationflags": subprocess.CREATE_NO_WINDOW}
    assert spawned == [([str(py), "-m", "server.tray_tools"], no_window)]
    assert opened == [str(tmp_path / "update.bat"), str(tmp_path / "install-caddy.bat")]
    tray_tools.command(tray_tools.ID_BACKUP, py, tmp_path, notify, run=_done(0, "b.db\n"))
    assert done.wait(10)
    assert notes == [("Снимаю копию базы…", True), ("Копия базы сохранена: b.db", True)]


def test_main_install_turns_on_autostart_then_runs_the_tray(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--install (server_tray.bat) включает автозапуск, без него реестр не трогаем; порт — из
    конфига; журнал трея — logs\\tray.log (у pythonw нет консоли)."""
    cfg = tmp_path / "config.json"
    cfg.write_text('{"port": 18555}', encoding="utf-8")
    monkeypatch.setenv("SRP_CONFIG_PATH", str(cfg))
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.delenv("SRP_PORT", raising=False)
    monkeypatch.setattr(tray, "ROOT", tmp_path)
    reg, ports = _FakeReg(), []

    def run(server_cfg: Any, run_reg: Any) -> int:
        assert run_reg is reg  # галочку меню ставит и снимает тот же реестр
        ports.append(server_cfg.port)
        return 7

    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    try:
        assert tray.main([], reg=reg, run=run) == 7
        assert reg.keys == {}
        assert tray.main(["--install"], reg=reg, run=run) == 7
        logging.getLogger("srp.tray").info("проверка журнала трея")
    finally:
        for handler in set(root.handlers) - set(handlers):
            root.removeHandler(handler)
            handler.close()
        root.setLevel(level)
    assert tray.autostart_enabled(reg)
    assert ports == [18555, 18555]
    log = (tmp_path / "logs" / "tray.log").read_text(encoding="utf-8")
    assert "INFO srp.tray: проверка журнала трея" in log


def test_pyw_reports_a_failed_start(tmp_path: Path) -> None:
    """Трей не поднялся (нет пакетов, битый код): трассировка — в logs\\tray.log и окно
    владельцу, а не тишина pythonw."""
    pyw = runpy.run_path(str(_ROOT / "server_tray.pyw"))  # не __main__: трей не запускается
    shown: list[str] = []
    try:
        raise ImportError("No module named 'server.tray'")
    except ImportError:
        assert pyw["_crash"](tmp_path / "logs", shown.append) == 1
    log = tmp_path / "logs" / "tray.log"
    assert "ImportError: No module named 'server.tray'" in log.read_text(encoding="utf-8")
    assert len(shown) == 1 and str(log) in shown[0]


@pytest.mark.parametrize(("error", "first"), [(0, True), (183, False), (5, False)])
def test_first_instance_respects_a_mutex_held_with_other_rights(error: int, first: bool) -> None:
    # Мьютекс держит трей с повышенными правами: CreateMutexW из обычного -> NULL + 5
    # (ERROR_ACCESS_DENIED). Это не «первый» — иначе два значка и два надзора.
    import ctypes
    from types import SimpleNamespace

    def create(*_args: object) -> None:
        ctypes.set_last_error(error)

    assert tray._first_instance(SimpleNamespace(CreateMutexW=create)) is first
