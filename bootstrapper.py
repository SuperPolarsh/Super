"""SuperSetup — the branded online bootstrapper for Super.

A small, frameless installer window that:

1. asks which drive Super should live on (drive picker + folder browse),
2. checks the public update manifest,
3. downloads the real Inno Setup payload with live speed and time left,
4. verifies its SHA-256,
5. installs it silently into the chosen folder,
6. offers to launch Super.

Run from source with::

    python bootstrapper.py --manifest https://example.com/update.json

Build the distributable ``SuperSetup.exe`` with::

    python -m PyInstaller SuperBootstrapper.spec --noconfirm
"""
from __future__ import annotations

import argparse
import ctypes
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageTk

import updater
from runtime_utils import resource_path
from version import UPDATE_MANIFEST_URL, UPDATE_TIMEOUT

APP_NAME = "Super"
APP_EXE = "Super.exe"
UNINSTALL_KEY = (r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
                 r"\{B1E4D0A2-5C3F-4E88-9A17-2F6D8C4B7E31}_is1")

WINDOW_W = 520
MARGIN = 22
CONTENT_W = 474
PROGRESS_H = 250
DONE_H = 250

BG = "#08080D"
PANEL = "#12121A"
FIELD = "#101017"
BORDER = "#232330"
HEAD = "#F5F5FA"
TEXT = "#DDDDE8"
MUTED = "#9A9AAE"
FAINT = "#6E6E86"
PURPLE = "#7C3AED"
PURPLE_HOVER = "#8B5CF6"
PURPLE_SOFT = "#B49CFF"
TRACK = "#1B1B26"
DIVIDER = "#191923"
CLOSE_BORDER = "#2C2C3A"
CLOSE_HOVER = "#241528"
GREEN = "#4ADE80"
GREEN_BG = "#0F2A1C"
RED = "#FF7A7A"
RED_BAR = "#F05A5A"

CREATE_NO_WINDOW = 0x08000000
MB = 1024 * 1024


# --------------------------------------------------------------------------
# helpers (no Tk needed — covered by tests)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Drive:
    """A fixed local disk, e.g. ``Drive("D", free, total)``."""

    letter: str
    free: int
    total: int

    @property
    def used_fraction(self) -> float:
        return usage_fraction(self.free, self.total)


def usage_fraction(free: int, total: int) -> float:
    """Share of the disk that is already in use (0..1)."""
    if total <= 0:
        return 0.0
    return max(0.0, min(1.0, 1.0 - (free / total)))


def drive_label(letter: str) -> str:
    return f"Drive {str(letter).strip().rstrip(':/\\').upper()[:1]}"


def drive_detail(drive: Drive) -> str:
    if drive.total <= 0:
        return "Free space unknown"
    return f"{format_gigabytes(drive.free)} free of {format_gigabytes(drive.total)}"


def drive_bar_color(used: float) -> str:
    """Red when a drive is nearly full, purple otherwise."""
    return RED_BAR if used >= 0.9 else PURPLE_SOFT


def default_install_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Programs" / APP_NAME


def downloads_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Super" / "downloads"


def install_folder_for(letter: str, system_drive: str = "") -> Path:
    """Folder Super uses on a given drive.

    The system drive keeps the per-user ``%LOCALAPPDATA%`` folder so the app
    can update itself without a UAC prompt; any other drive gets ``<X>:\\Super``.
    """
    clean = str(letter or "").strip().rstrip(":/\\").upper()[:1]
    if not clean or not clean.isalpha():
        return default_install_dir()
    system = str(system_drive or os.environ.get("SystemDrive") or "C:").strip()
    system = system.rstrip(":/\\").upper()[:1] or "C"
    if clean == system:
        return default_install_dir()
    return Path(f"{clean}:\\") / APP_NAME


def list_drives() -> list[Drive]:
    """Fixed local disks, in drive-letter order."""
    if os.name != "nt":
        return []
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.GetLogicalDrives.restype = ctypes.c_ulong
        kernel32.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
        kernel32.GetDriveTypeW.restype = ctypes.c_uint
        kernel32.GetDiskFreeSpaceExW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
        ]
        bitmap = kernel32.GetLogicalDrives()
    except Exception:
        return []

    drives: list[Drive] = []
    for index in range(26):
        if not bitmap & (1 << index):
            continue
        root = f"{chr(65 + index)}:\\"
        if kernel32.GetDriveTypeW(root) != 3:  # DRIVE_FIXED
            continue
        free = ctypes.c_ulonglong(0)
        total = ctypes.c_ulonglong(0)
        if not kernel32.GetDiskFreeSpaceExW(
                root, ctypes.byref(free), ctypes.byref(total), None):
            continue
        if total.value <= 0:
            continue
        drives.append(Drive(chr(65 + index), free.value, total.value))
    return drives


def folder_writable(folder) -> bool:
    """True when this user can create files in ``folder`` without UAC."""
    folder = Path(folder)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / f".super-write-test-{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def installer_args(installer, install_dir) -> list[str]:
    """Arguments that make Inno Setup install without any UI."""
    return [
        str(installer),
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/SP-",
        "/CLOSEAPPLICATIONS",
        f"/DIR={install_dir}",
    ]


def shorten_path(path, max_chars: int = 58) -> str:
    """Keeps the start and tail of a long Windows path readable."""
    text = str(path).rstrip("\\/")
    if len(text) <= max_chars:
        return text
    parts = [part for part in text.replace("/", "\\").split("\\") if part]
    if len(parts) <= 2:
        return text[: max_chars - 1] + "…"
    tail: list[str] = []
    for part in reversed(parts[1:]):
        trial = tail + [part]
        candidate = "\\".join([parts[0], "…"] + list(reversed(trial)))
        if len(candidate) > max_chars:
            break
        tail = trial
    if tail:
        return "\\".join([parts[0], "…"] + list(reversed(tail)))
    return text[: max_chars - 1] + "…"


def format_megabytes(value: int) -> str:
    return f"{value / MB:.0f} MB"


def format_gigabytes(value: int) -> str:
    return f"{value / (1024 ** 3):.2f} GB"


def format_speed(bytes_per_second: float) -> str:
    if not bytes_per_second or bytes_per_second < 0:
        bytes_per_second = 0
    return f"{bytes_per_second / MB:.1f} MB/s"


def format_eta(seconds: float) -> str:
    total = int(max(0, round(seconds)))
    if total < 60:
        return f"about {max(1, total)} s left"
    minutes = int(round(total / 60))
    if minutes < 60:
        return f"about {minutes} min left"
    hours, minutes = divmod(minutes, 60)
    if minutes == 0:
        return f"about {hours} h left"
    return f"about {hours} h {minutes} min left"


def progress_summary(done: int, total: int, rate: float = 0) -> str:
    """The subtitle line under the download heading."""
    if total:
        text = f"{done / MB:.1f} MB of {format_megabytes(total)}"
    else:
        text = f"{format_megabytes(done)} downloaded"
    if rate and rate > 0:
        text += f" · {format_speed(rate)}"
        if total and done < total:
            text += f" · {format_eta((total - done) / rate)}"
    return text


def _registry_value(key, name):
    try:
        import winreg
        value, _ = winreg.QueryValueEx(key, name)
        return str(value)
    except OSError:
        return ""


def installed_info() -> tuple[str, Path | None]:
    """Returns (version, install folder) for an existing install."""
    if os.name != "nt":
        return "", None
    import winreg
    version, location = "", ""
    for hive, flags in ((winreg.HKEY_CURRENT_USER, 0),
                        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY)):
        try:
            with winreg.OpenKey(hive, UNINSTALL_KEY, 0,
                                winreg.KEY_READ | flags) as key:
                version = _registry_value(key, "DisplayVersion")
                location = _registry_value(key, "InstallLocation").strip('"')
                break
        except OSError:
            continue
    folder = Path(location) if location else None
    if folder:
        # The in-app updater replaces Super.exe without touching the registry,
        # so the marker file is the fresher source of truth.
        try:
            marker = (folder / "version.txt").read_text(encoding="utf-8").strip()
            if marker and updater.is_newer(marker, version):
                version = marker
        except OSError:
            pass
    return version, folder


def installed_exe() -> Path | None:
    _, folder = installed_info()
    if folder:
        candidate = folder / APP_EXE
        if candidate.is_file():
            return candidate
    return None


# --------------------------------------------------------------------------
# widgets
# --------------------------------------------------------------------------
class GradientBar(ctk.CTkFrame):
    """Thin rounded bar painted with a purple gradient.

    ``value`` of ``None`` animates an indeterminate shimmer; a float from
    0 to 1 paints a determinate fill.
    """

    COLORS = ((91, 59, 234), (139, 92, 246), (192, 132, 252))

    def __init__(self, master, width=CONTENT_W, height=9, **kwargs):
        super().__init__(master, width=width, height=height,
                         fg_color=BG, corner_radius=0, **kwargs)
        self._canvas = tk.Canvas(self, highlightthickness=0, bd=0, bg=BG)
        self._canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self._canvas.bind("<Configure>", lambda _event: self._paint())
        self._value: float | None = None
        self._phase = 0.0
        self._job = None
        self._photo = None
        self._gradient_cache: tuple[int, int, Image.Image] | None = None

    @property
    def value(self):
        return self._value

    def set(self, value: float):
        self.stop()
        self._value = max(0.0, min(1.0, float(value)))
        self._paint()

    def indeterminate(self):
        self._value = None
        if self._job is None:
            self._tick()

    def stop(self):
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except Exception:
                pass
            self._job = None

    def _tick(self):
        self._phase = (self._phase + 0.018) % 1.0
        self._paint()
        self._job = self.after(33, self._tick)

    def _gradient(self, width: int, height: int) -> Image.Image:
        cached = self._gradient_cache
        if cached and cached[0] == width and cached[1] == height:
            return cached[2]
        image = Image.new("RGBA", (width, height))
        draw = ImageDraw.Draw(image)
        stops = self.COLORS
        for x in range(width):
            position = (x / max(1, width - 1)) * (len(stops) - 1)
            index = min(int(position), len(stops) - 2)
            fraction = position - index
            a, b = stops[index], stops[index + 1]
            color = tuple(int(a[i] + (b[i] - a[i]) * fraction) for i in range(3))
            draw.line((x, 0, x, height), fill=color + (255,))
        self._gradient_cache = (width, height, image)
        return image

    def _paint(self):
        width = self._canvas.winfo_width()
        height = self._canvas.winfo_height()
        if width <= 2 or height <= 2:
            return
        scale = 4
        big_w, big_h = width * scale, height * scale
        image = Image.new("RGBA", (big_w, big_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        radius = big_h / 2
        draw.rounded_rectangle((0, 0, big_w - 1, big_h - 1),
                               radius=radius, fill=TRACK)

        if self._value is None:
            pill = int(big_w * 0.34)
            left = int(self._phase * (big_w + pill)) - pill
            right = left + pill
            left, right = max(0, left), min(big_w, right)
            if right > left:
                width_px = right - left
                mask = Image.new("L", (width_px, big_h), 0)
                ImageDraw.Draw(mask).rounded_rectangle(
                    (0, 0, width_px - 1, big_h - 1), radius=radius, fill=255)
                image.paste(self._gradient(width_px, big_h), (left, 0), mask)
        elif self._value > 0:
            fill = max(2, int(self._value * big_w))
            mask = Image.new("L", (fill, big_h), 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                (0, 0, fill - 1, big_h - 1),
                radius=min(radius, fill / 2), fill=255)
            image.paste(self._gradient(fill, big_h), (0, 0), mask)

        image = image.resize((width, height), Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(image)
        self._canvas.delete("all")
        self._canvas.create_image(0, 0, anchor="nw", image=self._photo)


class UsageBar(tk.Canvas):
    """Slim rounded bar showing how full a disk is."""

    def __init__(self, master, width=176, height=8, background=PANEL):
        super().__init__(master, width=width, height=height, bg=background,
                         highlightthickness=0, bd=0)
        self._width = width
        self._height = height
        self._background = background
        self._fraction = 0.0
        self._color = PURPLE_SOFT
        self.bind("<Configure>", lambda _event: self._paint())

    def set_fraction(self, fraction: float, color: str):
        self._fraction = max(0.0, min(1.0, float(fraction)))
        self._color = color
        self._paint()

    def _paint(self):
        width = self.winfo_width() or self._width
        height = self.winfo_height() or self._height
        self.configure(bg=self._background)
        self.delete("all")
        middle = height / 2
        thickness = max(3, height - 3)
        pad = thickness / 2
        self.create_line(pad, middle, width - pad, middle, width=thickness,
                         capstyle="round", fill=TRACK)
        end = pad + (width - 2 * pad) * self._fraction
        if end > pad:
            self.create_line(pad, middle, end, middle, width=thickness,
                             capstyle="round", fill=self._color)


class DriveCard(ctk.CTkFrame):
    """Pickable disk row: letter tile, free space, and a usage bar."""

    def __init__(self, master, drive: Drive, on_select):
        super().__init__(master, fg_color=PANEL, corner_radius=12,
                         width=CONTENT_W, height=58, border_width=1,
                         border_color=BORDER)
        self.drive = drive
        self._on_select = on_select
        self.grid_propagate(False)
        self.pack_propagate(False)

        self.tile = ctk.CTkLabel(self, text=drive.letter, width=34, height=34,
                                 corner_radius=9, fg_color="#1B1B27",
                                 text_color=MUTED,
                                 font=ctk.CTkFont(family="Segoe UI Semibold",
                                                  size=13))
        self.tile.place(x=14, y=12)

        self.title = ctk.CTkLabel(self, text=drive_label(drive.letter),
                                  text_color=TEXT, anchor="w", width=210,
                                  font=ctk.CTkFont(family="Segoe UI Semibold",
                                                   size=12))
        self.title.place(x=58, y=7)

        self.detail = ctk.CTkLabel(self, text=drive_detail(drive),
                                   text_color=FAINT, anchor="w", width=210,
                                   font=ctk.CTkFont(family="Segoe UI", size=10))
        self.detail.place(x=58, y=29)

        self.bar = UsageBar(self, width=176, height=8, background=PANEL)
        self.bar.place(relx=1.0, x=-16, y=25, anchor="ne")
        self.bar.set_fraction(drive.used_fraction,
                              drive_bar_color(drive.used_fraction))

        for widget in (self, self.tile, self.title, self.detail, self.bar):
            widget.bind("<Button-1>", self._clicked)
            try:
                widget.configure(cursor="hand2")
            except Exception:
                pass

    def _clicked(self, _event=None):
        self._on_select(self.drive)

    def set_selected(self, selected: bool):
        self.configure(border_color=PURPLE if selected else BORDER,
                       border_width=2 if selected else 1)
        self.tile.configure(fg_color=PURPLE if selected else "#1B1B27",
                            text_color="#FFFFFF" if selected else MUTED)


class Bootstrapper(ctk.CTk):
    def __init__(self, autostart: bool = True, manifest_url: str = "",
                 install_dir: str = ""):
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.withdraw()
        self.configure(fg_color=BG)
        self.title(f"{APP_NAME} Setup")
        try:
            self.iconbitmap(resource_path("assets", "super.ico"))
        except Exception:
            pass
        self.overrideredirect(True)
        self.attributes("-topmost", True)

        self.manifest_url = (manifest_url or os.environ.get("SUPER_UPDATE_URL")
                             or UPDATE_MANIFEST_URL).strip()
        existing_version, existing_folder = installed_info()
        if install_dir:
            self.install_dir = Path(install_dir)
            self._show_location = False
        elif existing_folder:
            self.install_dir = existing_folder
            self._show_location = False
        else:
            self.install_dir = default_install_dir()
            self._show_location = True

        self.events: queue.Queue = queue.Queue()
        self._installing = False
        self._closed = False
        self._pump_job = None
        self._start_job = None
        self._launch_job = None
        self._exit_job = None
        self._drag_offset = None
        self._folder_icon = None
        self._drive_cards: list[DriveCard] = []
        self._selected_letter = ""
        self._chosen_folder: Path | None = existing_folder
        self._chosen_exe: Path | None = None
        self._location_height = PROGRESS_H
        self._rate = 0.0
        self._last_tick: tuple[float, int] | None = None
        self._stop_reason = ""

        self._build()
        self._show_screen("location" if self._show_location else "progress")
        self.deiconify()
        # CustomTkinter's mainloop() calls _windows_set_titlebar_color() while
        # the window is still flagged as non-existent, which withdraws it and
        # restores whatever state it sampled — "withdrawn", because we hid the
        # window while building. One real update() marks it as existing so
        # mainloop leaves our deiconify() alone.
        self.update()
        self.lift()
        self.focus_force()
        self._pump_job = self.after(120, self._pump)
        if autostart:
            if self._show_location:
                self._sync_selection()
            else:
                self._start_job = self.after(120, self._begin_check)

    # ---------------------------------------------------------------- layout
    def _build(self):
        border = ctk.CTkFrame(self, fg_color=BORDER, corner_radius=0)
        border.pack(fill="both", expand=True)
        self.body = ctk.CTkFrame(border, fg_color=BG, corner_radius=0)
        self.body.pack(fill="both", expand=True, padx=1, pady=1)

        self._build_header()
        self._build_location()
        self._build_progress()
        self._build_done()
        self._populate_drives(list_drives())
        self._select_current_drive()
        self._update_path()

    def _build_header(self):
        self.header = ctk.CTkFrame(self.body, fg_color=BG, height=48,
                                   corner_radius=0)
        self.header.place(x=0, y=0, relwidth=1)
        self.logo = None
        try:
            logo = Image.open(resource_path("assets", "logo_mark.png")).convert("RGBA")
            self.logo = ctk.CTkImage(light_image=logo, dark_image=logo, size=(20, 20))
        except Exception:
            pass
        self.logo_label = ctk.CTkLabel(self.header, image=self.logo, text="",
                                       width=20, height=20)
        self.logo_label.place(x=MARGIN, y=13)
        self.name_label = ctk.CTkLabel(
            self.header, text=APP_NAME, text_color=HEAD, height=20, anchor="w",
            font=ctk.CTkFont(family="Segoe UI Semibold", size=13))
        self.name_label.place(x=MARGIN + 28, y=13)

        self.version_chip = ctk.CTkLabel(
            self.header, text="", text_color=FAINT, height=20, anchor="w",
            font=ctk.CTkFont(family="Segoe UI", size=10))
        self.version_chip.place(x=MARGIN + 76, y=13)

        self.close_button = ctk.CTkButton(
            self.header, text="✕", width=32, height=28, corner_radius=9,
            fg_color="transparent", hover_color=CLOSE_HOVER,
            border_width=1, border_color=CLOSE_BORDER,
            text_color=MUTED, font=ctk.CTkFont(family="Segoe UI", size=12),
            command=self._close)
        self.close_button.place(relx=1.0, x=-MARGIN, y=11, anchor="ne")

        for widget in (self.header, self.logo_label, self.name_label):
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)
        self.bind("<Escape>", lambda _event: self._close())

    def _build_location(self):
        screen = ctk.CTkFrame(self.body, fg_color=BG, corner_radius=0)
        self.screens = {"location": screen}

        ctk.CTkLabel(screen, text=f"Where should {APP_NAME} be installed?",
                     text_color=HEAD, anchor="w",
                     font=ctk.CTkFont(family="Segoe UI Semibold", size=20)
                     ).place(x=MARGIN, y=70)

        self.location_subtitle = ctk.CTkLabel(
            screen,
            text=("The app, its models, projects and work files all go here, so a "
                  "full C: drive is no problem. Your account and settings stay in "
                  "your Windows profile."),
            text_color=MUTED, anchor="nw", justify="left", wraplength=CONTENT_W,
            font=ctk.CTkFont(family="Segoe UI", size=11))
        self.location_subtitle.place(x=MARGIN, y=104)

        self.drive_container = ctk.CTkFrame(screen, fg_color="transparent")
        self.drive_container.place(x=MARGIN, y=184)

        self.error_label = ctk.CTkLabel(
            screen, text="", text_color=RED, anchor="w", justify="left",
            wraplength=CONTENT_W, width=CONTENT_W, height=18,
            font=ctk.CTkFont(family="Segoe UI", size=10))
        self.error_label.place(x=MARGIN, y=320)

        self.folder_title = ctk.CTkLabel(
            screen, text="Folder", text_color=FAINT, anchor="w", height=18,
            font=ctk.CTkFont(family="Segoe UI", size=10))
        self.folder_title.place(x=MARGIN, y=344)

        self.folder_row = ctk.CTkFrame(
            screen, fg_color=FIELD, corner_radius=12, border_width=1,
            border_color=BORDER, height=48, width=CONTENT_W)
        self.folder_row.grid_propagate(False)
        self.folder_row.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(self.folder_row, text="", image=self._make_folder_icon(),
                     width=16, height=16).grid(row=0, column=0, padx=(14, 10))
        self.folder_label = ctk.CTkLabel(
            self.folder_row, text="", text_color=TEXT, anchor="w",
            font=ctk.CTkFont(family="Segoe UI", size=11))
        self.folder_label.grid(row=0, column=1, sticky="ew")
        self.browse_button = ctk.CTkButton(
            self.folder_row, text="Browse", width=86, height=32, corner_radius=8,
            fg_color="#1B1B27", hover_color="#262634", border_width=1,
            border_color="#333342", text_color=TEXT,
            font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
            command=self._browse)
        self.browse_button.grid(row=0, column=2, padx=(8, 8))
        self.folder_row.place(x=MARGIN, y=370)

        self.installs_label = ctk.CTkLabel(
            screen, text="", text_color=MUTED, anchor="w", width=CONTENT_W,
            font=ctk.CTkFont(family="Segoe UI", size=10))
        self.installs_label.place(x=MARGIN, y=432)

        self.install_button = ctk.CTkButton(
            screen, text="Install", width=132, height=44, corner_radius=11,
            fg_color=PURPLE, hover_color=PURPLE_HOVER, text_color="#FFFFFF",
            font=ctk.CTkFont(family="Segoe UI Semibold", size=12),
            command=self._install_clicked)
        self.install_button.place(relx=1.0, x=-MARGIN, y=472, anchor="ne")
        self.cancel_button = ctk.CTkButton(
            screen, text="Cancel", width=112, height=44, corner_radius=11,
            fg_color="#15151F", hover_color="#1F1F2B", border_width=1,
            border_color="#2A2A38", text_color=TEXT,
            font=ctk.CTkFont(family="Segoe UI Semibold", size=12),
            command=self._close)
        self.cancel_button.place(relx=1.0, x=-MARGIN - 132 - 10, y=472,
                                 anchor="ne")

    def _build_progress(self):
        screen = ctk.CTkFrame(self.body, fg_color=BG, corner_radius=0)
        self.screens["progress"] = screen

        self.progress_heading = ctk.CTkLabel(
            screen, text="Checking for updates", text_color=HEAD, anchor="w",
            font=ctk.CTkFont(family="Segoe UI Semibold", size=20))
        self.progress_heading.place(x=MARGIN, y=66)

        self.progress_percent = ctk.CTkLabel(
            screen, text="", text_color=PURPLE_SOFT, anchor="e",
            font=ctk.CTkFont(family="Segoe UI Semibold", size=16))
        self.progress_percent.place(relx=1.0, x=-MARGIN, y=68, anchor="ne")

        self.progress_subtitle = ctk.CTkLabel(
            screen, text="Contacting the Super update server", text_color=MUTED,
            anchor="nw", justify="left", wraplength=CONTENT_W,
            font=ctk.CTkFont(family="Segoe UI", size=11))
        self.progress_subtitle.place(x=MARGIN, y=100)

        self.bar = GradientBar(screen, width=CONTENT_W, height=9)
        self.bar.place(x=MARGIN, y=154)

        ctk.CTkFrame(screen, fg_color=DIVIDER, height=1, corner_radius=0).place(
            x=0, y=188, relwidth=1)

        self.progress_footer = ctk.CTkFrame(screen, fg_color=BG, corner_radius=0,
                                            height=46)
        self.progress_footer.pack_propagate(False)
        self.progress_footer.place(x=0, y=204, relwidth=1)
        ctk.CTkLabel(self.progress_footer, text="",
                     image=self._make_folder_icon(), width=15, height=15).place(
            x=MARGIN, y=8)
        ctk.CTkLabel(self.progress_footer, text="Installs to", text_color=FAINT,
                     anchor="w", width=76,
                     font=ctk.CTkFont(family="Segoe UI", size=10)).place(
            x=MARGIN + 23, y=2)
        self.progress_path = ctk.CTkLabel(
            self.progress_footer, text="", text_color=MUTED, anchor="w",
            width=CONTENT_W - 76,
            font=ctk.CTkFont(family="Segoe UI", size=10))
        self.progress_path.place(x=MARGIN + 78, y=2)

        self.retry_button = ctk.CTkButton(
            screen, text="Try again", width=124, height=40, corner_radius=10,
            fg_color=PURPLE, hover_color=PURPLE_HOVER, text_color="#FFFFFF",
            font=ctk.CTkFont(family="Segoe UI Semibold", size=11),
            command=self._begin_check)

    def _build_done(self):
        screen = ctk.CTkFrame(self.body, fg_color=BG, corner_radius=0)
        self.screens["done"] = screen

        tile = ctk.CTkFrame(screen, fg_color=GREEN_BG, corner_radius=12,
                            width=46, height=46)
        tile.place(x=MARGIN, y=70)
        tile.pack_propagate(False)
        ctk.CTkLabel(tile, text="✓", text_color=GREEN,
                     font=ctk.CTkFont(family="Segoe UI Symbol", size=20,
                                      weight="bold")).pack(expand=True)

        self.done_heading = ctk.CTkLabel(
            screen, text="Installed", text_color=HEAD, anchor="w",
            font=ctk.CTkFont(family="Segoe UI Semibold", size=19))
        self.done_heading.place(x=MARGIN + 62, y=68)

        self.done_subtitle = ctk.CTkLabel(
            screen, text="", text_color=MUTED, anchor="nw", justify="left",
            wraplength=CONTENT_W - 62, width=CONTENT_W - 62,
            font=ctk.CTkFont(family="Segoe UI", size=11))
        self.done_subtitle.place(x=MARGIN + 62, y=104)

        self.done_launch = ctk.CTkButton(
            screen, text=f"Launch {APP_NAME}", width=150, height=44,
            corner_radius=11, fg_color=PURPLE, hover_color=PURPLE_HOVER,
            text_color="#FFFFFF",
            font=ctk.CTkFont(family="Segoe UI Semibold", size=12),
            command=self._done_launch_clicked)
        self.done_launch.place(relx=1.0, x=-MARGIN, y=170, anchor="ne")

        self.done_folder = ctk.CTkButton(
            screen, text="Open folder", width=124, height=44, corner_radius=11,
            fg_color="#15151F", hover_color="#1F1F2B", border_width=1,
            border_color="#2A2A38", text_color=TEXT,
            font=ctk.CTkFont(family="Segoe UI Semibold", size=12),
            command=self._open_folder)
        self.done_folder.place(relx=1.0, x=-MARGIN - 150 - 10, y=170,
                               anchor="ne")

    def _populate_drives(self, drives: list[Drive]):
        for card in self._drive_cards:
            card.destroy()
        self._drive_cards = []
        for drive in drives:
            card = DriveCard(self.drive_container, drive, self._choose_drive)
            card.pack(fill="x", pady=(0, 10))
            self._drive_cards.append(card)
        count = max(1, len(drives))
        end = 184 + count * 58 + (count - 1) * 10
        self.error_label.place_configure(y=end + 10)
        self.folder_title.place_configure(y=end + 34)
        self.folder_row.place_configure(y=end + 56)
        self.installs_label.place_configure(y=end + 118)
        buttons_y = end + 158
        self.install_button.place_configure(y=buttons_y)
        self.cancel_button.place_configure(y=buttons_y)
        self._location_height = buttons_y + 44 + 26

    def _make_folder_icon(self):
        if self._folder_icon is not None:
            return self._folder_icon
        size = 60
        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        color = (122, 122, 142, 255)
        width = 5
        draw.rounded_rectangle((6, 20, 54, 51), radius=7, outline=color, width=width)
        draw.line((9, 20, 9, 15, 16, 15), fill=color, width=width)
        draw.line((16, 15, 23, 20), fill=color, width=width)
        self._folder_icon = ctk.CTkImage(light_image=image, dark_image=image,
                                         size=(15, 15))
        return self._folder_icon

    def _center(self):
        self.update_idletasks()
        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 1:
            width, height = WINDOW_W, self._location_height
        x = max(0, (self.winfo_screenwidth() - width) // 2)
        y = max(0, (self.winfo_screenheight() - height) // 2 - 40)
        self.geometry(f"+{x}+{y}")

    def _show_screen(self, name: str):
        for key, frame in self.screens.items():
            if key == name:
                frame.place(x=0, y=0, relwidth=1, relheight=1)
                frame.lift()
            else:
                frame.place_forget()
        self.header.lift()
        height = {"location": self._location_height,
                  "progress": PROGRESS_H,
                  "done": DONE_H}[name]
        self.geometry(f"{WINDOW_W}x{height}")
        self._center()

    # ------------------------------------------------------------- dragging
    def _drag_start(self, event):
        self._drag_offset = (event.x_root - self.winfo_x(),
                             event.y_root - self.winfo_y())

    def _drag_move(self, event):
        if not self._drag_offset:
            return
        x = event.x_root - self._drag_offset[0]
        y = event.y_root - self._drag_offset[1]
        self.geometry(f"+{x}+{y}")

    # ---------------------------------------------------------------- state
    def _set_version_chip(self, version: str):
        text = f"v{version}" if version else ""
        self.version_chip.configure(text=text)

    def _set_heading(self, text: str, color: str = HEAD):
        self.progress_heading.configure(text=text, text_color=color)

    def _set_subtitle(self, text: str, lines: int = 2):
        text = " ".join(str(text).split())
        limit = 76 * lines - 4
        if len(text) > limit:
            text = text[: limit - 1].rstrip() + "…"
        self.progress_subtitle.configure(text=text, height=18 * lines)

    def _update_path(self):
        short = shorten_path(self.install_dir, 52)
        self.installs_label.configure(text=f"Installs to {short}")
        if hasattr(self, "progress_path"):
            self.progress_path.configure(text=shorten_path(self.install_dir, 56))
        if hasattr(self, "folder_label"):
            self.folder_label.configure(text=shorten_path(self.install_dir, 46))

    def _show_error(self, message: str):
        self.error_label.configure(text=message)

    def _clear_error(self):
        self.error_label.configure(text="")

    # ------------------------------------------------------------ selection
    def _choose_drive(self, drive: Drive):
        if self._installing:
            return
        self._selected_letter = drive.letter
        self.install_dir = install_folder_for(drive.letter)
        self._sync_selection()
        self._clear_error()
        self._update_path()

    def _sync_selection(self):
        for card in self._drive_cards:
            card.set_selected(card.drive.letter == self._selected_letter)

    def _select_current_drive(self):
        """Highlights the disk that already holds the chosen folder."""
        letter = str(self.install_dir)[:1].upper()
        for card in self._drive_cards:
            if card.drive.letter == letter:
                self._selected_letter = letter
                break
        self._sync_selection()

    def _browse(self):
        if self._installing:
            return
        try:
            chosen = filedialog.askdirectory(
                parent=self, title=f"Choose where {APP_NAME} installs",
                initialdir=str(self.install_dir), mustexist=False)
        except Exception:
            return
        if not chosen:
            return
        self.install_dir = Path(chosen)
        self._selected_letter = ""
        self._sync_selection()
        self._clear_error()
        self._update_path()

    def _install_clicked(self):
        if self._installing:
            return
        if not folder_writable(self.install_dir):
            self._show_error(
                "That folder needs administrator rights. Pick another drive "
                "or folder.")
            return
        self._clear_error()
        self._show_screen("progress")
        self._begin_check()

    # ---------------------------------------------------------------- flow
    def _begin_check(self):
        if self._closed:
            return
        self._installing = False
        self._rate = 0.0
        self._last_tick = None
        self.retry_button.place_forget()
        self.progress_footer.place(x=0, y=204, relwidth=1)
        self.bar.place(x=MARGIN, y=154)
        self.progress_percent.configure(text="")
        self.close_button.configure(state="normal", text_color=MUTED,
                                    border_color=CLOSE_BORDER)
        self._set_version_chip("")
        self._set_heading("Checking for updates")
        self._set_subtitle("Contacting the Super update server", lines=1)
        self.bar.indeterminate()
        if not self.manifest_url:
            self._fail("Updates aren't configured",
                       "This copy of SuperSetup doesn't know where the update "
                       "server is. Download the latest installer from the Super "
                       "website.", retry=None)
            return
        threading.Thread(target=self._check_worker, daemon=True).start()

    def _check_worker(self):
        try:
            data = updater.fetch_manifest(self.manifest_url, timeout=UPDATE_TIMEOUT)
            info = updater.validate_manifest(data)
        except Exception as exc:
            self.events.put(("check_failed", str(exc)))
            return
        self.events.put(("manifest", info))

    def _on_manifest(self, info):
        version, folder = installed_info()
        exe = installed_exe() if folder else None
        if version and exe and not updater.is_newer(info.version, version):
            self._state_installed(version, exe, up_to_date=True)
            return
        if not info.installer_url:
            self._fail("This release isn't ready",
                       "The update server has no installer for this version yet. "
                       "Please try again in a little while.")
            return
        if not info.installer_sha256:
            self._fail("This release isn't ready",
                       "The update server didn't publish a checksum for the "
                       "installer. Please try again in a little while.")
            return
        self._start_download(info)

    def _start_download(self, info):
        safe = re.sub(r"[^0-9A-Za-z._-]", "_", info.version) or "latest"
        target = downloads_dir() / f"Super-Setup-{safe}.exe"
        self._set_version_chip(info.version)
        self._set_heading(f"Downloading {APP_NAME} {info.version}")
        self._set_subtitle("Starting the download…", lines=1)
        self.progress_percent.configure(text="0%")
        self.bar.indeterminate()
        self._rate = 0.0
        self._last_tick = (time.monotonic(), 0)
        threading.Thread(target=self._download_worker,
                         args=(info, target), daemon=True).start()

    def _download_worker(self, info, target):
        try:
            path = updater.download_file(
                info.installer_url, target, info.installer_sha256,
                progress=lambda done, total: self.events.put(
                    ("progress", (done, total))),
                timeout=120)
        except Exception as exc:
            self.events.put(("download_failed", str(exc)))
            return
        self.events.put(("downloaded", (info, path)))

    def _on_progress(self, done: int, total: int):
        now = time.monotonic()
        if self._last_tick is not None:
            previous_time, previous_done = self._last_tick
            elapsed = now - previous_time
            # Ignore sub-frame samples: they turn into absurd MB/s numbers.
            if elapsed >= 0.3:
                sample = max(0, done - previous_done) / elapsed
                if self._rate <= 0:
                    self._rate = sample
                else:
                    self._rate = self._rate * 0.7 + sample * 0.3
                self._last_tick = (now, done)

        if total:
            self.bar.set(done / total)
            self.progress_percent.configure(text=f"{int(done * 100 / total)}%")
        self._set_subtitle(progress_summary(done, total, self._rate), lines=1)

    def _start_install(self, info, installer):
        self._installing = True
        self.bar.indeterminate()
        self.progress_percent.configure(text="")
        self._set_heading(f"Installing {APP_NAME}")
        self._set_subtitle("Setting up for this PC. This only takes a minute.",
                           lines=1)
        self.close_button.configure(state="disabled", text_color=FAINT,
                                    border_color="#20202C")
        threading.Thread(target=self._install_worker,
                         args=(installer, info), daemon=True).start()

    def _install_worker(self, installer, info):
        try:
            result = subprocess.run(
                installer_args(installer, self.install_dir),
                timeout=900, creationflags=CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            self.events.put(("install_failed", str(exc)))
            return
        if result.returncode != 0:
            self.events.put(("install_failed",
                             f"The installer exited with code {result.returncode}."))
            return
        exe = installed_exe()
        if exe is None:
            candidate = Path(self.install_dir) / APP_EXE
            exe = candidate if candidate.is_file() else None
        if exe is None:
            self.events.put(("install_failed",
                             "The installer finished but Super.exe wasn't found. "
                             "Try installing again."))
            return
        try:
            Path(installer).unlink(missing_ok=True)
        except OSError:
            pass
        self.events.put(("installed", (info, exe)))

    def _on_installed(self, info, exe: Path):
        self._installing = False
        self._chosen_exe = Path(exe)
        self._chosen_folder = self._chosen_exe.parent
        self._set_version_chip(info.version)
        self.done_heading.configure(text=f"{APP_NAME} {info.version} is installed")
        self.done_subtitle.configure(
            text="Start menu shortcuts are ready. Your projects and settings stay "
                 "in your Windows profile.")
        self.bar.stop()
        self._show_screen("done")

    def _state_installed(self, version: str, exe: Path, up_to_date: bool = False):
        self._installing = False
        self._chosen_exe = Path(exe)
        self._chosen_folder = self._chosen_exe.parent
        self._set_version_chip(version)
        self.done_heading.configure(text=f"{APP_NAME} {version} is installed")
        self.done_subtitle.configure(
            text="You already have the latest version." if up_to_date
            else "Start menu shortcuts are ready.")
        self.bar.stop()
        self._show_screen("done")

    def _done_launch_clicked(self):
        if self._chosen_exe is None:
            self._launch(self._installed_exe_guess())
        else:
            self._launch(self._chosen_exe)

    def _installed_exe_guess(self) -> Path:
        exe = installed_exe()
        return exe or (Path(self.install_dir) / APP_EXE)

    def _launch(self, exe: Path):
        if self._closed:
            return
        try:
            if not Path(exe).is_file():
                raise FileNotFoundError(str(exe))
            subprocess.Popen([str(exe)], close_fds=True)
        except Exception as exc:
            self._fail("Super couldn't start",
                       f"The app is installed, but Windows wouldn't launch it. {exc}")
            return
        self._installing = False
        self._exit_soon()

    def _open_folder(self):
        folder = self._chosen_folder or Path(self.install_dir)
        try:
            os.startfile(str(folder))  # noqa: S606 - documented shell open
        except Exception:
            pass

    def _fail(self, title: str, message: str, retry=None):
        self._installing = False
        self.close_button.configure(state="normal", text_color=MUTED,
                                    border_color=CLOSE_BORDER)
        self._show_screen("progress")
        self._set_heading(title, color=RED)
        self.progress_percent.configure(text="")
        self._set_subtitle(message, lines=3)
        self.bar.stop()
        self.bar.place_forget()
        self.progress_footer.place_forget()
        self.retry_button.configure(
            text="Try again" if retry else "Close",
            command=retry or self._close)
        self.retry_button.place(relx=1.0, x=-MARGIN, y=196, anchor="ne")

    def _exit_soon(self):
        self.bar.stop()
        if not self._closed:
            self._exit_job = self.after(300, self._close)

    # ------------------------------------------------------------- handlers
    def _close(self):
        if self._installing:
            return
        self._closed = True
        self.bar.stop()
        for job in (self._pump_job, self._start_job, self._launch_job,
                    self._exit_job):
            if job is not None:
                try:
                    self.after_cancel(job)
                except Exception:
                    pass
        self._pump_job = self._start_job = self._launch_job = self._exit_job = None
        try:
            self.destroy()
        except Exception:
            pass

    # ---------------------------------------------------------------- queue
    def _pump(self):
        if self._closed:
            return
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "manifest":
                    self._on_manifest(value)
                elif kind == "progress":
                    self._on_progress(*value)
                elif kind == "downloaded":
                    info, path = value
                    self._start_install(info, path)
                elif kind == "installed":
                    info, exe = value
                    self._on_installed(info, exe)
                elif kind == "check_failed":
                    self._fail("Couldn't check for updates",
                               "Super couldn't reach the update server. Check your "
                               "internet connection and try again.",
                               retry=self._begin_check)
                elif kind == "download_failed":
                    self._fail("Couldn't download Super",
                               f"The download didn't finish. {value}",
                               retry=self._begin_check)
                elif kind == "install_failed":
                    self._fail("Couldn't install Super", str(value),
                               retry=self._begin_check)
        except queue.Empty:
            pass
        if not self._closed:
            self._pump_job = self.after(90, self._pump)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Install Super")
    parser.add_argument("--manifest", default="",
                        help="Override the update manifest URL")
    parser.add_argument("--install-dir", default="",
                        help="Install somewhere other than the default folder")
    args, _ = parser.parse_known_args(argv)
    ctk.set_appearance_mode("dark")
    app = Bootstrapper(manifest_url=args.manifest,
                       install_dir=args.install_dir)
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

