"""One-window-per-user helpers shared by the launcher and the app.

The launcher is the first thing that runs, so it owns the "is Super already
open?" question: if the app is running it raises that window and exits. The
app keeps the same mutex as a backstop, so a stray second copy can never open
two windows even if it is started directly.
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

WINDOW_TITLE = "Super"
MUTEX_NAME = "Super.VideoClipper.SingleInstance"
SHOW_EVENT_NAME = "Super.VideoClipper.Show"


def acquire_mutex(name: str = MUTEX_NAME):
    """Claims the one-and-only app slot, returning None if it is taken.

    A named Windows mutex is released by the OS when the process dies, even
    after a crash, so there is no stale lock file that could keep Super from
    starting on a later day.
    """
    if sys.platform != "win32":
        return True
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = kernel32.CreateMutexW(None, False, name)
        if not handle:
            return True  # never refuse to start over a lock we could not open
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return None
        return handle
    except Exception:
        return True


def create_show_event():
    """Creates the signal a later launch rings to raise the app window."""
    if sys.platform != "win32":
        return None
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateEventW.argtypes = (ctypes.c_void_p, wintypes.BOOL,
                                          wintypes.BOOL, wintypes.LPCWSTR)
        kernel32.CreateEventW.restype = wintypes.HANDLE
        return kernel32.CreateEventW(None, False, False, SHOW_EVENT_NAME)
    except Exception:
        return None


def signal_running_instance() -> bool:
    """Tells the open app to come to the front, if it is listening."""
    if sys.platform != "win32":
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenEventW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
        kernel32.OpenEventW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = kernel32.OpenEventW(0x0002, False, SHOW_EVENT_NAME)  # EVENT_MODIFY_STATE
        if not handle:
            return False
        kernel32.SetEvent(handle)
        kernel32.CloseHandle(handle)
        return True
    except Exception:
        return False


def running_main_window():
    """Finds the visible top-level Super window, if one is open.

    Tk owns several top-level windows, and a plain FindWindow by title can
    return one of the invisible helpers, so walk the real window list and
    match the title the user actually sees.
    """
    if sys.platform != "win32":
        return 0
    try:
        user32 = ctypes.windll.user32
        matches = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def visit(hwnd, _param):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if buffer.value.strip().lower() == WINDOW_TITLE.lower():
                matches.append(hwnd)
                return False
            return True

        user32.EnumWindows(callback_type(visit), 0)
        return matches[0] if matches else 0
    except Exception:
        return 0


def focus_running_instance() -> bool:
    """Raises the copy that is already open instead of starting a second one."""
    if sys.platform != "win32":
        return False
    try:
        user32 = ctypes.windll.user32
        handle = running_main_window()
        if not handle:
            return False
        user32.SendMessageW.argtypes = (wintypes.HWND, wintypes.UINT,
                                        wintypes.WPARAM, wintypes.LPARAM)
        user32.ShowWindow(handle, 9)  # SW_RESTORE
        if user32.IsIconic(handle):
            # A Tk window ignores SW_RESTORE, so send what the taskbar sends
            # when the user clicks the minimised window.
            user32.SendMessageW(handle, 0x0112, 0xF120, 0)  # WM_SYSCOMMAND, SC_RESTORE
        user32.BringWindowToTop(handle)
        user32.SetForegroundWindow(handle)
        return True
    except Exception:
        return False
