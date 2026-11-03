"""Win32 helpers. pywebview's own on_top / resize setters touch WinForms from the JS-bridge worker thread,
which can deadlock the window while the page is busy, so these post the change to the window asynchronously."""
import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.SetWindowPos.restype = wintypes.BOOL
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE

HWND_TOPMOST, HWND_NOTOPMOST = wintypes.HWND(-1), wintypes.HWND(-2)
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE, SWP_ASYNCWINDOWPOS = 0x1, 0x2, 0x4, 0x10, 0x4000
SW_RESTORE = 9
ERROR_ALREADY_EXISTS = 183

_mutex = None  # keep the handle alive for the life of the process


def acquire_single_instance(name):
    """True if this is the only running instance."""
    global _mutex
    _mutex = kernel32.CreateMutexW(None, False, name)
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def focus_window(title):
    """Bring an already-running instance's window to the front."""
    hwnd = user32.FindWindowW(None, title)
    if hwnd:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)


def set_topmost(hwnd, on):
    return bool(user32.SetWindowPos(hwnd, HWND_TOPMOST if on else HWND_NOTOPMOST, 0, 0, 0, 0,
                                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_ASYNCWINDOWPOS))


def dpi_scale(hwnd):
    try:
        user32.GetDpiForWindow.argtypes = [wintypes.HWND]
        return (user32.GetDpiForWindow(hwnd) or 96) / 96
    except (AttributeError, OSError):
        return 1.0


def resize(hwnd, width, height):
    """Size in logical (DPI-independent) pixels; top-left stays where it is."""
    s = dpi_scale(hwnd)
    return bool(user32.SetWindowPos(hwnd, None, 0, 0, int(width * s), int(height * s),
                                    SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_ASYNCWINDOWPOS))


def is_on_screen(x, y, margin=60):
    """Whether a window at (x, y) still has a grabbable corner on some monitor (guards against unplugged displays)."""
    vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
    return vx - 200 < x < vx + vw - margin and vy - 10 < y < vy + vh - margin
