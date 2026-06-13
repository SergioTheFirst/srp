"""Win32 system-tray adapter (tray spec §2): ``Shell_NotifyIcon`` + hidden window.

A thin ctypes wrapper with **no decision logic** -- colours, text and nag timing
are decided in :mod:`client.tray.state`. Windows only; on any other platform the
constructor raises (import still succeeds, so the pure logic stays importable for
tests). ``TaskbarCreated`` is handled so the icon comes back after an explorer
restart, and ``NIF_INFO`` balloons are rendered by Windows 10/11 as toasts.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

# --- message + Shell_NotifyIcon constants ---------------------------------- #
WM_DESTROY = 0x0002
WM_TIMER = 0x0113
WM_COMMAND = 0x0111
WM_USER = 0x0400
_CALLBACK_MSG = WM_USER + 20
_TIMER_ID = 1
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
NIIF_INFO, NIIF_WARNING, NIIF_ERROR = 0x01, 0x02, 0x03

IMAGE_ICON = 1
LR_LOADFROMFILE, LR_DEFAULTSIZE, LR_SHARED = 0x0010, 0x0040, 0x8000
IDI_APPLICATION = 32512

TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100
MF_STRING, MF_SEPARATOR = 0x0000, 0x0800
CW_USEDEFAULT = ctypes.c_int(0x80000000).value

# Context-menu command ids.
ID_OPEN, ID_REFRESH, ID_ABOUT, ID_EXIT = 0xE001, 0xE002, 0xE003, 0xE004

_BALLOON_FLAG = {"ok": NIIF_INFO, "warn": NIIF_WARNING, "alert": NIIF_ERROR}

_WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


class _NOTIFYICONDATAW(ctypes.Structure):
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


class _WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


def _assets_dir() -> Path:
    return Path(__file__).resolve().parent / "assets"


class TrayIcon:
    """One tray icon + hidden message window. Construct on the UI thread."""

    def __init__(
        self,
        *,
        on_open: Callable[[], None],
        on_refresh: Callable[[], None],
        on_about: Callable[[], None],
        on_exit: Callable[[], None],
    ) -> None:
        try:
            self._u32 = ctypes.windll.user32
            self._shell = ctypes.windll.shell32
            self._k32 = ctypes.windll.kernel32
        except AttributeError as exc:  # not Windows
            raise RuntimeError("TrayIcon requires Windows (win32 API)") from exc

        self._declare_signatures()  # ctypes defaults to c_int args -> handles overflow
        self._on = {
            ID_OPEN: on_open,
            ID_REFRESH: on_refresh,
            ID_ABOUT: on_about,
            ID_EXIT: on_exit,
        }
        self._icons: dict[str, int] = {}
        self._added = False
        self._on_timer: Optional[Callable[[], None]] = None
        # "TaskbarCreated" is broadcast when explorer (re)starts -> re-add icon.
        self._taskbar_created = self._u32.RegisterWindowMessageW("TaskbarCreated")
        self._proc = _WNDPROC(self._wndproc)  # keep a ref alive
        self._hwnd = self._make_window()

    def _declare_signatures(self) -> None:
        """Pin argtypes/restype for every win32 call.

        Without this, ctypes marshals int args as ``c_int`` and 64-bit HWND/LPARAM
        pointer values overflow ("int too long to convert") -- e.g. in the WNDPROC
        relay to DefWindowProcW, and in SetTimer/PostMessageW/TrackPopupMenu.
        """
        u, k, sh = self._u32, self._k32, self._shell
        hwnd_p = ctypes.POINTER(wintypes.MSG)
        u.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
        u.RegisterWindowMessageW.restype = wintypes.UINT
        u.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASS)]
        u.RegisterClassW.restype = wintypes.ATOM
        u.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        ]
        u.CreateWindowExW.restype = wintypes.HWND
        u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.DefWindowProcW.restype = ctypes.c_ssize_t
        u.LoadImageW.argtypes = [
            wintypes.HINSTANCE,
            wintypes.LPCWSTR,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        u.LoadImageW.restype = wintypes.HANDLE
        u.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
        u.LoadIconW.restype = wintypes.HICON
        u.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, wintypes.LPVOID]
        u.SetTimer.restype = ctypes.c_size_t
        u.CreatePopupMenu.restype = wintypes.HMENU
        u.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
        u.AppendMenuW.restype = wintypes.BOOL
        u.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        u.GetCursorPos.restype = wintypes.BOOL
        u.SetForegroundWindow.argtypes = [wintypes.HWND]
        u.SetForegroundWindow.restype = wintypes.BOOL
        u.TrackPopupMenu.argtypes = [
            wintypes.HMENU,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.LPVOID,
        ]
        u.TrackPopupMenu.restype = wintypes.BOOL
        u.DestroyMenu.argtypes = [wintypes.HMENU]
        u.PostQuitMessage.argtypes = [ctypes.c_int]
        u.GetMessageW.argtypes = [hwnd_p, wintypes.HWND, wintypes.UINT, wintypes.UINT]
        u.GetMessageW.restype = ctypes.c_int
        u.TranslateMessage.argtypes = [hwnd_p]
        u.DispatchMessageW.argtypes = [hwnd_p]
        u.DispatchMessageW.restype = ctypes.c_ssize_t
        u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.PostMessageW.restype = wintypes.BOOL
        k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        k.GetModuleHandleW.restype = wintypes.HMODULE
        sh.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.c_void_p]
        sh.Shell_NotifyIconW.restype = wintypes.BOOL

    def set_timer(self, interval_ms: int, callback: Callable[[], None]) -> None:
        """Call *callback* on the message-loop thread every *interval_ms*."""
        self._on_timer = callback
        self._u32.SetTimer(self._hwnd, _TIMER_ID, interval_ms, None)

    # -- window + icon plumbing -------------------------------------------- #

    def _make_window(self) -> int:
        hinst = self._k32.GetModuleHandleW(None)
        wc = _WNDCLASS()
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = "SRPTrayHiddenWindow"
        if not self._u32.RegisterClassW(ctypes.byref(wc)):
            log.debug("RegisterClassW failed (%s)", ctypes.get_last_error())
        hwnd = self._u32.CreateWindowExW(
            0, wc.lpszClassName, "SRP", 0, 0, 0, 0, 0, None, None, hinst, None
        )
        if not hwnd:
            raise RuntimeError(f"CreateWindowExW failed ({ctypes.get_last_error()})")
        return hwnd

    def _load_icon(self, state: str) -> int:
        if state in self._icons:
            return self._icons[state]
        path = _assets_dir() / f"srp_{state}.ico"
        handle = self._u32.LoadImageW(
            None, str(path), IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE
        )
        if not handle:  # fall back to the stock app icon rather than show nothing
            # IDI_APPLICATION is an integer resource id -> MAKEINTRESOURCE: the raw
            # int travels in the LPCWSTR slot, NOT as a pointer to chr(32512).
            stock = ctypes.cast(ctypes.c_void_p(IDI_APPLICATION), wintypes.LPCWSTR)
            handle = self._u32.LoadIconW(None, stock)
        self._icons[state] = handle
        return handle

    def _nid(self, *, flags: int) -> _NOTIFYICONDATAW:
        nid = _NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = flags
        nid.uCallbackMessage = _CALLBACK_MSG
        return nid

    def show(self, state: str, tooltip: str) -> None:
        """Add the icon, or update it if already present (idempotent)."""
        nid = self._nid(flags=NIF_MESSAGE | NIF_ICON | NIF_TIP)
        nid.hIcon = self._load_icon(state)
        nid.szTip = tooltip[:127]
        action = NIM_MODIFY if self._added else NIM_ADD
        added = self._shell.Shell_NotifyIconW(action, ctypes.byref(nid))
        if not added and action == NIM_ADD:
            # a stale icon (e.g. after a crash) blocks ADD -> recover by modifying
            self._shell.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        self._added = True

    def balloon(self, title: str, text: str, level: str = "ok") -> None:
        nid = self._nid(flags=NIF_INFO)
        nid.szInfoTitle = title[:63]
        nid.szInfo = text[:255]
        nid.dwInfoFlags = _BALLOON_FLAG.get(level, NIIF_INFO)
        self._shell.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def remove(self) -> None:
        if self._added:
            self._shell.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid(flags=0)))
            self._added = False

    # -- menu + message loop ----------------------------------------------- #

    def _popup_menu(self) -> None:
        u = self._u32
        menu = u.CreatePopupMenu()
        u.AppendMenuW(menu, MF_STRING, ID_OPEN, "Открыть панель")
        u.AppendMenuW(menu, MF_STRING, ID_REFRESH, "Обновить")
        u.AppendMenuW(menu, MF_STRING, ID_ABOUT, "О программе")
        u.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        u.AppendMenuW(menu, MF_STRING, ID_EXIT, "Выход")
        pt = wintypes.POINT()
        u.GetCursorPos(ctypes.byref(pt))
        u.SetForegroundWindow(self._hwnd)  # so the menu dismisses on click-away
        cmd = u.TrackPopupMenu(
            menu, TPM_RIGHTBUTTON | TPM_RETURNCMD, pt.x, pt.y, 0, self._hwnd, None
        )
        u.DestroyMenu(menu)
        if cmd in self._on:
            self._dispatch(cmd)

    def _dispatch(self, cmd: int) -> None:
        try:
            self._on[cmd]()
        except Exception:  # noqa: BLE001 -- a callback must never kill the loop
            log.exception("tray menu handler %s failed", cmd)

    def _wndproc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        if msg == _CALLBACK_MSG:
            if lparam in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                self._dispatch(ID_OPEN)
            elif lparam == WM_RBUTTONUP:
                self._popup_menu()
            return 0
        if msg == WM_COMMAND:
            self._dispatch(wparam & 0xFFFF)
            return 0
        if msg == WM_TIMER and self._on_timer is not None:
            try:
                self._on_timer()
            except Exception:  # noqa: BLE001 -- a refresh tick must never kill the loop
                log.exception("tray timer tick failed")
            return 0
        if msg == self._taskbar_created:  # explorer restarted -> re-add
            self._added = False
            self.show("ok", "SRP")
            return 0
        if msg == WM_DESTROY:
            self._u32.PostQuitMessage(0)
            return 0
        return int(self._u32.DefWindowProcW(hwnd, msg, wparam, lparam))

    def run(self) -> None:
        """Pump the message loop until quit (blocks the calling thread)."""
        msg = wintypes.MSG()
        while self._u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            self._u32.TranslateMessage(ctypes.byref(msg))
            self._u32.DispatchMessageW(ctypes.byref(msg))

    def post_quit(self) -> None:
        self._u32.PostMessageW(self._hwnd, WM_DESTROY, 0, 0)
