"""Super's startup window: check for updates, then start the app.

The app ships as a folder (``app\\SuperApp.exe``) because unpacking a 350 MB
single-file build on every launch takes ~8 seconds. This launcher is tiny, so
it is on screen at once: it checks the public manifest, installs a newer build
when there is one, and only then starts the app. Double-clicking Super.exe
while the app is open just raises the open window.
"""
from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import customtkinter as ctk
from PIL import Image

import single_instance
import updater
from runtime_utils import resource_path
from version import APP_VERSION, UPDATE_MANIFEST_URL, UPDATE_TIMEOUT

APP_NAME = "Super"
APP_FOLDER = "app"
APP_EXE = "SuperApp.exe"
LAUNCHER_EXE = "Super.exe"

WINDOW_W = 470
WINDOW_H = 232
MARGIN = 22
CONTENT_W = WINDOW_W - MARGIN * 2

BG = "#08080D"
BORDER = "#232330"
HEAD = "#F5F5FA"
TEXT = "#DDDDE8"
MUTED = "#9A9AAE"
FAINT = "#6E6E86"
PURPLE = "#7C3AED"
PURPLE_SOFT = "#B49CFF"
TRACK = "#1B1B26"
CLOSE_BORDER = "#2C2C3A"
CLOSE_HOVER = "#241528"
GREEN = "#4ADE80"
RED = "#FF7A7A"
RED_BAR = "#F05A5A"

CREATE_NO_WINDOW = 0x08000000
MB = 1024 * 1024

INSTALL_SCRIPT = r"""@echo off
setlocal
set "APPPID=%~1"
set "SETUP=%~2"
set "LAUNCH=%~3"
set /a waited=0
:wait
tasklist /FI "PID eq %APPPID%" /NH 2>NUL | findstr /C:"%APPPID%" >NUL
if errorlevel 1 goto install
set /a waited+=1
if %waited% geq 300 goto install
ping -n 2 127.0.0.1 >NUL
goto wait
:install
start "" /wait "%SETUP%" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
if exist "%LAUNCH%" start "" "%LAUNCH%"
del "%~f0" >NUL 2>&1
"""


# --------------------------------------------------------------------------
# paths and small helpers (no Tk needed - covered by tests)
# --------------------------------------------------------------------------
def install_folder() -> Path:
    """Folder holding this launcher: the per-user or per-drive install root."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def app_executable(folder=None) -> Path:
    """The heavy app that this launcher starts."""
    root = Path(folder) if folder else install_folder()
    return root / APP_FOLDER / APP_EXE


def update_cache_dir() -> Path:
    cache = Path(tempfile.gettempdir()) / "SuperUpdate"
    cache.mkdir(parents=True, exist_ok=True)
    return cache


def needs_update(info, current_version: str, app_present: bool) -> bool:
    """True when the manifest is newer, or when the app itself is missing.

    The second case is how a 1.0.x install that only ever had a single
    Super.exe migrates: the launcher lands, finds no app folder, and installs
    the full build instead of failing.
    """
    if info is None:
        return False
    if not app_present:
        return True
    return updater.is_newer(info.version, current_version)


def installer_name(version: str) -> str:
    safe = re.sub(r"[^0-9A-Za-z._-]", "_", str(version or "latest")) or "latest"
    return f"Super-Setup-{safe}.exe"


def install_command(setup_path, launcher_path, pid) -> list:
    """The hidden cmd call that waits for us to exit, then installs."""
    script = Path(tempfile.gettempdir()) / f"super-install-{int(pid)}.cmd"
    script.write_text(INSTALL_SCRIPT, encoding="utf-8")
    return ["cmd", "/c", str(script), str(int(pid)), str(setup_path), str(launcher_path)]


def short_error(exc) -> str:
    text = " ".join(str(exc).split())
    return text[:180] + "..." if len(text) > 180 else text


def format_megabytes(size: float) -> str:
    return f"{max(0.0, size) / MB:.0f} MB"


# --------------------------------------------------------------------------
# window
# --------------------------------------------------------------------------
class Launcher(ctk.CTk):
    def __init__(self, manifest_url: str = "", autostart: bool = True):
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.withdraw()
        self.configure(fg_color=BG)
        self.title(f"{APP_NAME} Launcher")
        try:
            self.iconbitmap(resource_path("assets", "super.ico"))
        except Exception:
            pass
        self.overrideredirect(True)
        self.attributes("-topmost", True)

        self.manifest_url = (manifest_url or os.environ.get("SUPER_UPDATE_URL")
                             or UPDATE_MANIFEST_URL).strip()
        self.events: queue.Queue = queue.Queue()
        self._drag_offset = None
        self._finished = False
        self._pump_job = None

        self._build()
        self._centre()
        self.deiconify()
        self.update()
        self.lift()
        self.focus_force()
        self._pump_job = self.after(80, self._pump)
        if autostart:
            self.bar.configure(mode="indeterminate")
            self.bar.start()
            # The check is ~2 s of network wait, so it starts now and overlaps
            # with the rest of the window setup instead of after it.
            threading.Thread(target=self._check_worker, daemon=True).start()

    # ---------------------------------------------------------------- layout
    def _build(self):
        border = ctk.CTkFrame(self, fg_color=BORDER, corner_radius=14)
        border.pack(fill="both", expand=True)
        body = ctk.CTkFrame(border, fg_color=BG, corner_radius=13)
        body.pack(fill="both", expand=True, padx=1, pady=1)

        header = ctk.CTkFrame(body, fg_color=BG, height=46, corner_radius=0)
        header.place(x=0, y=0, relwidth=1)

        logo = None
        try:
            image = Image.open(resource_path("assets", "logo_mark.png")).convert("RGBA")
            logo = ctk.CTkImage(light_image=image, dark_image=image, size=(20, 20))
        except Exception:
            pass
        self._logo = logo
        ctk.CTkLabel(header, image=logo, text="", width=20, height=20).place(x=MARGIN, y=13)
        ctk.CTkLabel(header, text=APP_NAME, text_color=HEAD, height=20, anchor="w",
                     font=ctk.CTkFont(family="Segoe UI Semibold", size=13)).place(
            x=MARGIN + 28, y=13)
        self.version_chip = ctk.CTkLabel(header, text=f"v{APP_VERSION}", text_color=FAINT,
                                         height=20, anchor="w",
                                         font=ctk.CTkFont(family="Segoe UI", size=10))
        self.version_chip.place(x=MARGIN + 76, y=14)

        close = ctk.CTkButton(
            header, text="✕", width=32, height=28, corner_radius=9,
            fg_color="transparent", hover_color=CLOSE_HOVER,
            border_width=1, border_color=CLOSE_BORDER,
            text_color=MUTED, font=ctk.CTkFont(family="Segoe UI", size=12),
            command=self._close)
        close.place(relx=1.0, x=-MARGIN, y=11, anchor="ne")

        for widget in (header,):
            widget.bind("<Button-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._on_drag)

        self.title_label = ctk.CTkLabel(
            body, text="Checking for updates", text_color=TEXT, anchor="w",
            font=ctk.CTkFont(family="Segoe UI Semibold", size=13))
        self.title_label.place(x=MARGIN, y=64)

        self.detail_label = ctk.CTkLabel(
            body, text="Contacting the Super update server", text_color=MUTED,
            anchor="w", font=ctk.CTkFont(family="Segoe UI", size=10))
        self.detail_label.place(x=MARGIN, y=91)

        self.bar = ctk.CTkProgressBar(body, width=CONTENT_W, height=6, corner_radius=3,
                                      fg_color=TRACK, progress_color=PURPLE)
        self.bar.set(0)
        self.bar.place(x=MARGIN, y=124)

        footer = ctk.CTkFrame(body, fg_color=BG, height=40, corner_radius=0)
        footer.place(x=0, y=WINDOW_H - 46, relwidth=1)
        ctk.CTkFrame(footer, fg_color=BORDER, height=1).place(x=0, y=0, relwidth=1)
        ctk.CTkLabel(footer, text=f"Installs to {install_folder()}", text_color=FAINT,
                     anchor="w", font=ctk.CTkFont(family="Segoe UI", size=9)).place(
            x=MARGIN, y=14)

    def _centre(self):
        self.update_idletasks()
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        x = max(0, (screen_w - WINDOW_W) // 2)
        y = max(0, (screen_h - WINDOW_H) // 2 - 40)
        self.geometry(f"{WINDOW_W}x{WINDOW_H}+{x}+{y}")

    def _start_drag(self, event):
        self._drag_offset = (event.x_root - self.winfo_x(), event.y_root - self.winfo_y())

    def _on_drag(self, event):
        if not self._drag_offset:
            return
        x = event.x_root - self._drag_offset[0]
        y = event.y_root - self._drag_offset[1]
        self.geometry(f"+{x}+{y}")

    # ---------------------------------------------------------------- states
    def _set_state(self, title: str, detail: str, color: str = TEXT):
        self.title_label.configure(text=title, text_color=color)
        self.detail_label.configure(text=detail)

    def _fail(self, title: str, detail: str):
        self._finished = True
        self.bar.stop()
        self.bar.configure(progress_color=RED_BAR)
        self.bar.set(1)
        self._set_state(title, detail, RED)

    # ---------------------------------------------------------------- flow
    def _check_worker(self):
        try:
            info = updater.check_for_update(APP_VERSION, self.manifest_url,
                                            timeout=UPDATE_TIMEOUT)
        except Exception as exc:
            self.events.put(("check_failed", short_error(exc)))
            return
        self.events.put(("checked", info))

    def _on_checked(self, info):
        app_present = app_executable().is_file()
        if needs_update(info, APP_VERSION, app_present):
            if not info.installer_url:
                self._fail("Update server is not ready",
                           "No installer is published for this version yet.")
                return
            self._start_download(info)
            return
        if not app_present:
            self._fail("Super isn't installed",
                       "Run SuperSetup.exe to install Super.")
            return
        self.bar.stop()
        self.bar.configure(progress_color=GREEN)
        self.bar.set(1)
        self._set_state("Super is up to date", "Starting Super...", GREEN)
        self.after(650, self._start_app)

    def _start_download(self, info):
        self.bar.stop()
        self.bar.configure(mode="determinate", progress_color=PURPLE)
        self.bar.set(0)
        self._set_state(f"Updating to Super {info.version}", "Downloading the installer...")
        threading.Thread(target=self._download_worker, args=(info,), daemon=True).start()

    def _download_worker(self, info):
        try:
            target = update_cache_dir() / installer_name(info.version)
            path = updater.download_file(
                info.installer_url, target, info.installer_sha256,
                progress=lambda done, total: self.events.put(("progress", (done, total))),
                timeout=60)
        except Exception as exc:
            self.events.put(("failed", short_error(exc)))
            return
        self.events.put(("downloaded", path))

    def _on_progress(self, done, total):
        if not total:
            self.detail_label.configure(text=f"{format_megabytes(done)} downloaded")
            self.bar.configure(mode="indeterminate")
            return
        share = max(0.0, min(1.0, done / total))
        self.bar.set(share)
        self.detail_label.configure(
            text=f"{format_megabytes(done)} of {format_megabytes(total)}  ·  {share * 100:.0f}%")

    def _on_downloaded(self, path):
        self.bar.set(1)
        self._set_state("Installing Super", "Super will reopen when it's finished.")
        self.after(500, lambda: self._run_installer(Path(path)))

    def _run_installer(self, setup_path):
        if self._finished:
            return
        self._finished = True
        try:
            subprocess.Popen(
                install_command(setup_path, Path(sys.executable), os.getpid()),
                creationflags=CREATE_NO_WINDOW, close_fds=True)
        except Exception as exc:
            self._fail("Couldn't start the installer", short_error(exc))
            return
        self.after(250, self.destroy)

    def _start_app(self):
        exe = app_executable()
        if not exe.is_file():
            self._fail("Super isn't installed", "Run SuperSetup.exe to install Super.")
            return
        try:
            subprocess.Popen([str(exe)], cwd=str(exe.parent), close_fds=True)
        except Exception as exc:
            self._fail("Super couldn't start", short_error(exc))
            return
        self.after(180, self.destroy)

    # ---------------------------------------------------------------- pump
    def _pump(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "checked":
                    self._on_checked(value)
                elif kind == "check_failed":
                    # Offline is not fatal: start the installed app anyway.
                    if app_executable().is_file():
                        self.bar.stop()
                        self._set_state("Couldn't check for updates",
                                        "Starting Super anyway...", MUTED)
                        self.after(500, self._start_app)
                    else:
                        self._fail("Couldn't reach the update server", value)
                elif kind == "progress":
                    self._on_progress(*value)
                elif kind == "downloaded":
                    self._on_downloaded(value)
                elif kind == "failed":
                    self._fail("Super couldn't update", value)
        except queue.Empty:
            pass
        try:
            if self.winfo_exists():
                self._pump_job = self.after(80, self._pump)
        except Exception:
            pass

    def _close(self):
        self.destroy()


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Super launcher")
    parser.add_argument("--manifest", default="", help="Override the update manifest URL")
    parser.add_argument("--no-check", action="store_true",
                        help="Skip the update check and start the app")
    args = parser.parse_args()

    manifest = (args.manifest or os.environ.get("SUPER_UPDATE_URL")
                or UPDATE_MANIFEST_URL).strip()

    # If Super is already open, raise it and leave before Tk is even built:
    # no window flash, no update check, and the second launch closes at once.
    if single_instance.signal_running_instance():
        return

    # With no update server configured (or --no-check) there is nothing to
    # wait for, so start the installed app straight away.
    exe = app_executable()
    if (args.no_check or not manifest) and exe.is_file():
        subprocess.Popen([str(exe)], cwd=str(exe.parent), close_fds=True)
        return

    Launcher(manifest_url=manifest).mainloop()


if __name__ == "__main__":
    main()
