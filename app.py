"""Desktop UI for creating AI-powered short-form video clips."""
import ctypes
import io
import os
import queue
import re
import sys
import tempfile
import threading
import tkinter as tk
import urllib.request
from ctypes import wintypes
from functools import partial
from tkinter import filedialog
from PIL import Image, ImageDraw, ImageFont

import customtkinter as ctk
import yt_dlp

import single_instance
from version import APP_VERSION
from link_info import extract_link_info, format_link_title, looks_like_link, short_error
from runtime_utils import (default_output_dir, hide_child_console_windows, load_env_files,
                           log_error, log_line, resource_path)

load_env_files()

import lazy_imports


def auto_generate_viral_clips(*args, **kwargs):
    """Imports the torch/whisper pipeline on first use, not at start-up."""
    return lazy_imports.call("main", "auto_generate_viral_clips", *args, **kwargs)


def download_public_video(*args, **kwargs):
    return lazy_imports.call("source_downloader", "download_public_video", *args, **kwargs)


def crop_image_to_ratio(*args, **kwargs):
    return lazy_imports.call("editor", "crop_image_to_ratio", *args, **kwargs)


def get_preview_frame(*args, **kwargs):
    return lazy_imports.call("editor", "get_preview_frame", *args, **kwargs)

hide_child_console_windows()

if os.name == "nt":
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Super.AI.Studio")
    except Exception:
        pass

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

BG = "#08080C"
RAIL = "#0B0B11"
CARD = "#121219"
CARD_HOVER = "#16161F"
CARD_ACTIVE = "#181128"
FIELD = "#101017"
BORDER = "#262633"
TEXT = "#F5F5FA"
MUTED = "#9A9AAE"
FAINT = "#6E6E86"
PURPLE = "#8B5CF6"
PURPLE_DARK = "#241A3A"
PURPLE_HOVER = "#7C3AED"
GREEN = "#4ADE80"

# The sidebar shows icons only until the window is wide enough for the labels.
# Below this width the restored window keeps a slim rail (like most editors),
# and zooming the app widens it so the nav names come back.
RAIL_OPEN = 216
RAIL_CLOSED = 68
RAIL_BREAKPOINT = 1500
TITLEBAR_HEIGHT = 38
# The workspace keeps its natural column width instead of stretching with the
# window. Stretching re-lays out every card and label on each zoom, which costs
# a few hundred milliseconds on Windows, so the column keeps one width and only
# the margins grow when the window is maximized.


def flat_label(master, text, background, color=TEXT, font=("Segoe UI", 12),
               anchor="w", justify="left", **options):
    """A plain Tk label for static text.

    Every customtkinter label is three window handles (frame, canvas, text),
    and Windows moves each handle during a layout pass. This machine spends
    roughly 0.7 ms per handle, so a full window of CTk labels made zooming
    crawl. Plain labels handle their own text, so the same UI costs a third as
    much to re-lay out while looking identical on these solid backgrounds.
    """
    return tk.Label(master, text=text, fg=color, bg=background, font=font,
                    anchor=anchor, justify=justify, bd=0, highlightthickness=0,
                    padx=0, pady=0, **options)

# A link preview must resolve to an image or a message; it may never sit on
# "Loading preview..." because a metadata fetch stalled somewhere.
PREVIEW_DEADLINE_MS = 15000
# Scrolling: how far one wheel notch moves the content, and how the eased
# follow-up frames move toward it. Each frame costs ~4 ms, so 15 ms keeps the
# motion continuous without eating the event loop.
SCROLL_NOTCH_PX = 96
SCROLL_FRAME_MS = 10
SCROLL_EASE = 0.4
SCROLL_MAX_STEP = 72.0
# The last few pixels land in one frame instead of a long tail of 1 px steps,
# which is what made the end of a scroll feel like it was sticking.
SCROLL_SNAP_PX = 12.0

# Where the progress bar sits while clips render. Everything before the first
# "Rendering 1/N" line fills the first 45%, and the rendering loop fills the
# rest so the last clip ends at 99% and the finished state shows 100%.
PROGRESS_RENDER_START = 45.0
PROGRESS_RENDER_END = 99.0

# ---------------------------------------------------------------- window chrome
# Windows 10 paints a bright accent-coloured outline around any resizable window
# that has no caption, and paints the caption itself in that same accent. The
# caption style is therefore kept, so the outline never appears, while
# WM_NCCALCSIZE hands the entire window to the client: that hides the native
# title bar and the accent band and leaves no frame pixels to tint. Because no
# part of the window is native any more, the header moves the window and the
# window borders resize it from our own grab strips.
WM_NCCALCSIZE = 0x0083
# Width of the invisible grab strip around each window border.
RESIZE_GRIP = 6
# Pixels the pointer must travel before a maximized header drag restores the
# window, so a plain click on the title bar does not un-maximize it.
DRAG_SLOP = 3
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
GWLP_WNDPROC = -4

if os.name == "nt":
    class _NCCALCSIZE_PARAMS(ctypes.Structure):
        _fields_ = [("rgrc", wintypes.RECT * 3), ("lppos", ctypes.c_void_p)]

    class _MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    _USER32 = ctypes.WinDLL("user32", use_last_error=True)
    _USER32.GetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int)
    _USER32.GetWindowLongPtrW.restype = ctypes.c_void_p
    _USER32.SetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_void_p)
    _USER32.SetWindowLongPtrW.restype = ctypes.c_void_p
    _USER32.CallWindowProcW.argtypes = (ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
                                        wintypes.WPARAM, wintypes.LPARAM)
    _USER32.CallWindowProcW.restype = ctypes.c_ssize_t
    _USER32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
    _USER32.GetAncestor.restype = wintypes.HWND
    _USER32.MonitorFromWindow.argtypes = (wintypes.HWND, wintypes.DWORD)
    _USER32.MonitorFromWindow.restype = wintypes.HMONITOR
    _USER32.GetMonitorInfoW.argtypes = (wintypes.HMONITOR, ctypes.c_void_p)
    _USER32.GetMonitorInfoW.restype = wintypes.BOOL
    _USER32.IsZoomed.argtypes = (wintypes.HWND,)
    _USER32.IsZoomed.restype = wintypes.BOOL
    _USER32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
    _USER32.GetCursorPos.restype = wintypes.BOOL
    _USER32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    _USER32.GetWindowRect.restype = wintypes.BOOL
    _USER32.SetWindowPos.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                     wintypes.UINT)
    _USER32.SetWindowPos.restype = wintypes.BOOL
    _WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM)


def _monitor_work_area(hwnd):
    """Returns the screen rectangle a maximized window should fill."""
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(_MONITORINFO)
    monitor = _USER32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
    if not _USER32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return None
    return info.rcWork


def job_progress(message, clip_base=0.0, clip_span=0.0):
    """Turns one pipeline status line into (percent, clip_base, clip_span).

    ``percent`` is ``None`` when the line says nothing about how far the job
    has got. Long stages report plain text, while the download and each FFmpeg
    pass report a percentage; mapping them all onto one bar is what makes the
    progress meaningful instead of a spinner.
    """
    text = " ".join(str(message).split())
    low = text.lower()
    if not low:
        return None, clip_base, clip_span

    match = re.search(r"rendering (\d+)\s*/\s*(\d+)", low)
    if match:
        index = max(1, int(match.group(1)))
        total = max(1, int(match.group(2)))
        span = (PROGRESS_RENDER_END - PROGRESS_RENDER_START) / total
        base = PROGRESS_RENDER_START + (index - 1) * span
        return base, base, span

    if clip_span > 0:
        match = re.search(r"(reframed clip|adding captions)\D*?(\d+(?:\.\d+)?)\s*%", low)
        if match:
            share = min(100.0, float(match.group(2))) / 100
            # Reframing and captions are two passes, so each owns half a clip.
            share = 0.5 + share * 0.5 if match.group(1) == "adding captions" else share * 0.5
            return clip_base + clip_span * share, clip_base, clip_span

    match = re.search(r"downloading source video\D*?(\d+(?:\.\d+)?)\s*%", low)
    if match:
        return 4.0 + 6.0 * min(100.0, float(match.group(1))) / 100, clip_base, clip_span

    checkpoints = (
        ("fetching the public video link", 3.0),
        ("starting analysis", 12.0),
        ("transcribing", 18.0),
        ("using cached transcription", 20.0),
        ("heuristics shortlisted", 34.0),
        ("ai is auditing", 38.0),
        ("ai audit returned", 42.0),
        ("candidate clip", 45.0),
        ("trust report saved", 99.0),
    )
    if "saved clip_" in low and clip_span > 0:
        return clip_base + clip_span, clip_base, clip_span
    if "done - created" in low or "finished - created" in low:
        return 100.0, clip_base, clip_span
    for needle, value in checkpoints:
        if needle in low:
            return value, clip_base, clip_span
    return None, clip_base, clip_span


def _acquire_single_instance(name=single_instance.MUTEX_NAME):
    """Keeps the boolean helper the launch tests use."""
    return single_instance.acquire_mutex(name) is not None


class ViralClipperApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Super")
        self.geometry("1240x820")
        self.minsize(1040, 680)
        self.configure(fg_color=BG)
        try:
            self.iconbitmap(resource_path("assets", "super.ico"))
        except Exception:
            pass

        self.events = queue.Queue()
        self.video_path = tk.StringVar()
        self.video_url = tk.StringVar()
        self.output_path = tk.StringVar(value=default_output_dir())
        self.score_value = tk.IntVar(value=30)
        self.duration_value = tk.IntVar(value=30)
        self.aspect_ratio = tk.StringVar(value="Original")
        self.current_preview_img = None
        self._preview_raw_img = None
        self._preview_ratio_img = None
        self._preview_source_key = None
        self._preview_source_height = 1080
        self.captions = tk.BooleanVar(value=True)
        self.selected_tool = "Auto Clipper"
        self.tool_cards = {}
        self._result_clips = []
        self._job_running = False
        self._link_fetch_job = None
        self._link_fetch_token = 0
        self._link_fetch_url = ""
        self._link_fetch_inflight = False
        self._link_cache = {}
        self._link_banner_job = None
        self._link_banner_dots = 0
        self._scroll_job = None
        self._scroll_targets = {}
        self._redraw = None
        self._progress_value = 0.0
        self._clip_base = 0.0
        self._clip_span = 0.0
        self._clip_index = 1
        self._clip_total = 1
        self._preview_autoscroll = True
        self._preview_token = 0
        self._preview_job = None
        self._build()
        # A crash inside a Tk callback is invisible in the packaged app (no
        # console), which left Generate stuck on "Generating..." forever.
        # Route every callback failure to the log and back to the UI instead.
        self.report_callback_exception = self._report_callback_exception
        threading.excepthook = self._report_thread_exception
        # torch/whisper/moviepy take ~2.2 s to import; do it after the window is
        # on screen so Generate never waits for them but start-up stays quick.
        self.after(350, lambda: threading.Thread(target=lazy_imports.warm,
                                                 daemon=True).start())
        self.after(100, self._read_events)
        self._install_custom_chrome()
        # Stripping the caption makes Windows keep the old client size by
        # growing the frame, so restate the size the app is designed for.
        self.geometry("1240x820")
        self._style_window_chrome()
        # DWM can composite the first frame before the attribute lands, so
        # apply it again once the window is actually on screen.
        self.after(250, self._style_window_chrome)
        self._show_event = single_instance.create_show_event()
        if self._show_event:
            threading.Thread(target=self._watch_show_event, daemon=True).start()

    def _style_window_chrome(self):
        """Turns the native title bar dark so it blends with the app.

        Windows paints the title bar in the user's accent colour by default,
        which was the purple strip above the window. The immersive dark-mode
        attribute forces a dark caption on Windows 10; Windows 11 also accepts
        an explicit caption colour, so it matches the app background exactly.
        """
        if sys.platform != "win32":
            return
        try:
            import ctypes
            import ctypes.wintypes as wintypes

            self.update_idletasks()
            user32 = ctypes.windll.user32
            dwmapi = ctypes.windll.dwmapi
            dwmapi.DwmSetWindowAttribute.argtypes = (
                wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD)
            dwmapi.DwmSetWindowAttribute.restype = ctypes.HRESULT
            hwnd = user32.GetAncestor(self.winfo_id(), 2) or self.winfo_id()

            def colorref(value):
                red, green, blue = (int(value[i:i + 2], 16) for i in (1, 3, 5))
                return red | (green << 8) | (blue << 16)

            enabled = ctypes.c_int(1)
            for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE
                try:
                    if dwmapi.DwmSetWindowAttribute(
                            hwnd, attribute, ctypes.byref(enabled),
                            ctypes.sizeof(enabled)) == 0:
                        break
                except OSError:
                    continue
            for attribute, color in ((34, BG), (35, "#0A0A10"), (36, TEXT)):
                # Border, caption and text colours: Windows 11 only, so a
                # failure here is expected and harmless on Windows 10.
                shade = ctypes.c_int(colorref(color))
                try:
                    dwmapi.DwmSetWindowAttribute(
                        hwnd, attribute, ctypes.byref(shade), ctypes.sizeof(shade))
                except OSError:
                    pass
        except Exception:
            pass

    def _watch_show_event(self):
        """Waits for a later launch to ask this window to come forward."""
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32
            kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, wintypes.DWORD)
            kernel32.WaitForSingleObject.restype = wintypes.DWORD
        except Exception:
            return
        while True:
            result = kernel32.WaitForSingleObject(self._show_event, 0xFFFFFFFF)
            if result != 0:  # WAIT_OBJECT_0 is the only success
                return
            self.events.put(("show_window", None))

    def _bring_to_front(self):
        """Restores and raises the window, as if the taskbar was clicked."""
        try:
            self.deiconify()
            self.lift()
            self.focus_force()
            self.attributes("-topmost", True)
            self.after(300, lambda: self.attributes("-topmost", False))
        except Exception:
            pass

    def _build(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self._build_titlebar()
        self._build_rail()

        # Plain frame instead of CTkScrollableFrame: a scrollable canvas
        # repaints every child window at ~0.6 ms each whenever it resizes
        # (zooming), which made maximize/restore lag. A plain frame costs
        # ~0.3 ms per child and needs no canvas at all. Content is placed
        # manually and scrolled by shifting its y offset.
        self.workspace = tk.Frame(self, bg=BG)
        self.workspace.grid(row=1, column=0, columnspan=2, sticky="nsew",
                            padx=(RAIL_CLOSED, 0))
        self.workspace_scrollbar = ctk.CTkScrollbar(
            self.workspace, width=14, button_color="#2B2B34",
            button_hover_color="#3A3A46", command=self._on_workspace_scroll)
        # Kept clear of the right border so the resize grip stays clickable.
        self.workspace_scrollbar.place(relx=1.0, x=-2 - RESIZE_GRIP, y=0,
                                       relheight=1.0, anchor="ne")
        self.workspace.bind("<Configure>",
                            lambda _e: self._place_content(), add="+")
        self._scroll_pos = 0.0
        self._build_workspace()
        self._install_smooth_scroll()
        self.rail.lift()
        self._sync_rail_mode()

    def _build_titlebar(self):
        """Our own slim window header: no purple accent strip, just chrome."""
        bar = ctk.CTkFrame(self, fg_color=RAIL, corner_radius=0, height=TITLEBAR_HEIGHT)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew")
        bar.grid_propagate(False)
        bar.grid_columnconfigure(1, weight=1)
        bar.grid_rowconfigure(0, weight=1)
        self.title_bar = bar

        mark = flat_label(bar, "✦", RAIL, PURPLE, ("Segoe UI Symbol", 12, "bold"))
        mark.grid(row=0, column=0, padx=(18, 7))
        name = flat_label(bar, "Super", RAIL, "#B9B9C9", ("Segoe UI", 11, "bold"))
        name.grid(row=0, column=1, sticky="w")

        buttons = tk.Frame(bar, bg=RAIL)
        buttons.grid(row=0, column=2, sticky="e", padx=(0, 8))

        def chrome_button(glyph, command, hover="#1A1A23", tone=MUTED):
            # Plain labels instead of CTkButtons: each CTk button carries
            # canvas windows that repaint on every zoom. Plain labels are a
            # single window and repaint far faster.
            button = flat_label(buttons, glyph, RAIL, tone,
                                ("Segoe UI Symbol", 13))
            button.pack(side="left", padx=2)
            button.configure(cursor="hand2")

            def on_enter(_e):
                button.configure(bg=hover)

            def on_leave(_e):
                button.configure(bg=RAIL)

            button.bind("<Enter>", on_enter)
            button.bind("<Leave>", on_leave)
            button.bind("<Button-1>", lambda _e: command())
            return button

        chrome_button("–", self.iconify)
        self.max_button = chrome_button("□", self._toggle_zoom)
        chrome_button("✕", self.destroy, hover="#B62A33", tone="#C9C9D6")

        for widget in (bar, mark, name):
            widget.bind("<Button-1>", self._begin_window_drag)
            widget.bind("<B1-Motion>", self._drag_window)
            widget.bind("<ButtonRelease-1>", self._end_window_drag)
            widget.bind("<Double-Button-1>", self._toggle_zoom)
            widget.bind("<Motion>", self._hover_window_edge)
            widget.bind("<Leave>", lambda _e: self.configure(cursor="arrow"))
            widget.configure(cursor="arrow")
        # The window has no native frame left, so border presses anywhere in
        # it have to reach the grip handler. The toplevel's bindtag is the
        # last tag of every child, which runs after the child's own handler.
        self.bind("<Button-1>", self._begin_window_drag, add="+")
        self.bind("<B1-Motion>", self._drag_window, add="+")
        self.bind("<ButtonRelease-1>", self._end_window_drag, add="+")
        self.bind("<Motion>", self._hover_window_edge, add="+")
        # Tk routes every descendant's <Configure> through the toplevel's
        # bindtag, so binding on the window itself runs the handler once per
        # child widget on every resize (88 calls while maximizing). A private
        # tag that only the window carries keeps it to a single call.
        self.bindtags(("SuperRootEvents",) + self.bindtags())
        self.bind_class("SuperRootEvents", "<Configure>", self._sync_max_button, add="+")
        self.bind_class("SuperRootEvents", "<Configure>", self._sync_rail_mode, add="+")

    def _begin_window_drag(self, event):
        """Starts a header drag, or arms a border resize.

        ``self.geometry()`` re-runs Tk's whole geometry manager on every mouse
        motion, which cost about 200 ms per move with this many widgets and is
        what made dragging crawl. ``SetWindowPos`` moves and resizes the window
        without touching Tk's layout and takes about 2 ms, so the window tracks
        the pointer. The pointer is read in physical screen pixels to match the
        units ``SetWindowPos`` expects, whatever the display scaling is.
        """
        self._drag_origin = None
        self._resize_origin = None
        self._resize_edges = None
        self._zoomed_drag_start = None
        if os.name != "nt":
            self._drag_origin = ((event.x_root, event.y_root),
                                 (self.winfo_x(), self.winfo_y()))
            return "break"
        try:
            point = wintypes.POINT()
            rect = wintypes.RECT()
            hwnd = self._chrome_handle()
            if not (_USER32.GetCursorPos(ctypes.byref(point))
                    and _USER32.GetWindowRect(hwnd, ctypes.byref(rect))):
                return "break"
            zoomed = self.state() == "zoomed"
            edges = self._grip_zone(point, rect)
            if edges is not None and not zoomed:
                # No part of the window belongs to the native frame, so the
                # border strips have to resize it themselves. A maximized
                # window has no edges to drag, just like a native one.
                self._resize_edges = edges
                self._resize_origin = (point.x, point.y, rect.left, rect.top,
                                       rect.right - rect.left,
                                       rect.bottom - rect.top)
                return "break"
            origin = wintypes.POINT(0, 0)
            _USER32.ClientToScreen(hwnd, ctypes.byref(origin))
            if point.y - origin.y <= TITLEBAR_HEIGHT:
                self._drag_origin = ((point.x, point.y), (rect.left, rect.top))
                if zoomed:
                    # Dragging a maximized title bar restores the window, but
                    # only once the pointer actually moves; a plain click must
                    # not change the state.
                    self._zoomed_drag_start = (point.x, point.y)
        except Exception:
            self._drag_origin = ((event.x_root, event.y_root),
                                 (self.winfo_x(), self.winfo_y()))
        return "break"

    @staticmethod
    def _grip_zone(point, rect):
        """Returns the border a screen point grabs, or ``None``."""
        vertical = horizontal = None
        if point.y - rect.top <= RESIZE_GRIP:
            vertical = "top"
        elif rect.bottom - point.y <= RESIZE_GRIP:
            vertical = "bottom"
        if point.x - rect.left <= RESIZE_GRIP:
            horizontal = "left"
        elif rect.right - point.x <= RESIZE_GRIP:
            horizontal = "right"
        if vertical is None and horizontal is None:
            return None
        return vertical, horizontal

    def _end_window_drag(self, _event=None):
        self._drag_origin = None
        self._resize_origin = None
        self._resize_edges = None
        self._zoomed_drag_start = None

    def _restore_for_drag(self, point, rect):
        """Restores a maximized window under the pointer so it can be moved.

        ``SetWindowPos`` on a still-maximized window leaves it zoomed but
        displaced, and Windows paints the accent border around that state.
        Restoring first, exactly as a native title bar does, keeps the frame
        clean and puts the header back under the cursor.
        """
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width <= 0 or height <= 0:
            return False
        ratio = (point.x - rect.left) / float(width)
        offset = min(max(point.y - rect.top, 0), TITLEBAR_HEIGHT - 1)
        self.state("normal")
        self.update_idletasks()
        hwnd = self._chrome_handle()
        restored = wintypes.RECT()
        if not _USER32.GetWindowRect(hwnd, ctypes.byref(restored)):
            return False
        new_width = restored.right - restored.left
        new_height = restored.bottom - restored.top
        left = point.x - int(ratio * new_width)
        top = point.y - offset
        _USER32.SetWindowPos(hwnd, None, left, top, new_width, new_height,
                             SWP_NOZORDER | SWP_NOACTIVATE)
        self._drag_origin = ((point.x, point.y), (left, top))
        self._zoomed_drag_start = None
        self._sync_max_button()
        return True

    def _minimum_size(self):
        """The window's minimum size in physical pixels, as Tk sees it."""
        try:
            value = self.tk.call("wm", "minsize", self._w)
            if not isinstance(value, (tuple, list)):
                value = str(value).split()
            return int(value[0]), int(value[1])
        except Exception:
            return 400, 300

    _GRIP_CURSORS = {("top", None): "size_ns", ("bottom", None): "size_ns",
                     (None, "left"): "size_we", (None, "right"): "size_we",
                     ("top", "left"): "size_nw_se",
                     ("top", "right"): "size_ne_sw",
                     ("bottom", "left"): "size_ne_sw",
                     ("bottom", "right"): "size_nw_se"}

    def _hover_window_edge(self, _event=None):
        """Shows the matching resize cursor over the window borders."""
        if os.name != "nt":
            return
        try:
            point = wintypes.POINT()
            rect = wintypes.RECT()
            if not (_USER32.GetCursorPos(ctypes.byref(point))
                    and _USER32.GetWindowRect(self._chrome_handle(),
                                              ctypes.byref(rect))):
                return
            edges = None if self.state() == "zoomed" else self._grip_zone(
                point, rect)
            wanted = self._GRIP_CURSORS.get(edges, "arrow") if edges else "arrow"
            if self.cget("cursor") != wanted:
                self.configure(cursor=wanted)
        except Exception:
            pass

    def _drag_window(self, event):
        """Moves or resizes the window while the header or a border is held."""
        origin = getattr(self, "_resize_origin", None)
        if origin:
            start_x, start_y, left, top, width, height = origin
            try:
                point = wintypes.POINT()
                if not _USER32.GetCursorPos(ctypes.byref(point)):
                    return "break"
                vertical, horizontal = self._resize_edges
                minimum_width, minimum_height = self._minimum_size()
                new_left, new_top = left, top
                new_width, new_height = width, height
                if horizontal == "left":
                    new_left = min(left + point.x - start_x,
                                   left + width - minimum_width)
                    new_width = width + (left - new_left)
                elif horizontal == "right":
                    new_width = max(minimum_width, width + point.x - start_x)
                if vertical == "top":
                    new_top = min(top + point.y - start_y,
                                  top + height - minimum_height)
                    new_height = height + (top - new_top)
                elif vertical == "bottom":
                    new_height = max(minimum_height, height + point.y - start_y)
                if (new_left, new_top, new_width, new_height) != (
                        left, top, width, height):
                    _USER32.SetWindowPos(
                        self._chrome_handle(), None, new_left, new_top,
                        new_width, new_height,
                        SWP_NOZORDER | SWP_NOACTIVATE)
            except Exception:
                pass
            return "break"
        origin = getattr(self, "_drag_origin", None)
        if not origin:
            return "break"
        start, corner = origin
        try:
            if os.name == "nt":
                point = wintypes.POINT()
                if not _USER32.GetCursorPos(ctypes.byref(point)):
                    return "break"
                if self._zoomed_drag_start and self.state() == "zoomed":
                    moved = (abs(point.x - start[0])
                             + abs(point.y - start[1]))
                    if moved <= DRAG_SLOP:
                        return "break"
                    area = wintypes.RECT()
                    if (_USER32.GetWindowRect(self._chrome_handle(),
                                              ctypes.byref(area))
                            and self._restore_for_drag(point, area)):
                        start, corner = self._drag_origin
                left = corner[0] + point.x - start[0]
                top = corner[1] + point.y - start[1]
                if (left, top) != corner:
                    _USER32.SetWindowPos(
                        self._chrome_handle(), None, left, top, 0, 0,
                        SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
                return "break"
        except Exception:
            pass
        x0, y0, win_x, win_y = start[0], start[1], corner[0], corner[1]
        self.geometry(f"+{win_x + event.x_root - x0}+{win_y + event.y_root - y0}")
        return "break"

    def _chrome_handle(self):
        """Returns the real top-level window handle Tk is drawing into."""
        hwnd = getattr(self, "_chrome_hwnd", None)
        if hwnd:
            return hwnd
        return _USER32.GetAncestor(self.winfo_id(), 2) or self.winfo_id()

    def _toggle_zoom(self, _event=None):
        zoomed = self.state() == "zoomed"
        self.state("normal" if zoomed else "zoomed")
        if hasattr(self, "max_button"):
            self.max_button.configure(text="□" if zoomed else "❐")
        return "break"

    def _sync_max_button(self, _event=None):
        """Keeps the maximize glyph right when Windows zooms the window."""
        if not hasattr(self, "max_button"):
            return
        wanted = "❐" if self.state() == "zoomed" else "□"
        try:
            if self.max_button.cget("text") != wanted:
                self.max_button.configure(text=wanted)
        except Exception:
            pass

    def _install_custom_chrome(self):
        """Removes the native caption without the 1 px accent outline.

        Windows 10 paints a bright accent border around captionless windows and
        the caption itself in that same accent, so the caption style is kept.
        ``WM_NCCALCSIZE`` then hands the entire window to the client, which
        hides the title bar and the accent band while the window keeps its
        shadow, Alt+Tab entry and maximized geometry. Nothing is left to the
        native frame, so the header and the border grips below move and resize
        the window themselves.
        """
        if os.name != "nt" or getattr(self, "_chrome_proc", None) is not None:
            return
        try:
            hwnd = _USER32.GetAncestor(self.winfo_id(), 2) or self.winfo_id()
            old_proc = _USER32.GetWindowLongPtrW(hwnd, GWLP_WNDPROC)
            if not old_proc:
                return

            def wndproc(h, message, wparam, lparam):
                if message == WM_NCCALCSIZE and wparam:
                    if _USER32.IsZoomed(h):
                        # Cover the work area exactly, so the taskbar stays
                        # visible and no frame pixels are left to tint.
                        area = _monitor_work_area(h)
                        if area is not None:
                            rect = ctypes.cast(
                                lparam, ctypes.POINTER(_NCCALCSIZE_PARAMS)
                            ).contents.rgrc[0]
                            rect.left, rect.top = area.left, area.top
                            rect.right, rect.bottom = area.right, area.bottom
                    # Give the whole window to the client: that removes the
                    # native caption and with it the accent band.
                    return 0
                return _USER32.CallWindowProcW(old_proc, h, message, wparam, lparam)

            self._chrome_hwnd = hwnd
            self._chrome_old_proc = old_proc
            self._chrome_proc = _WNDPROC(wndproc)
            _USER32.SetWindowLongPtrW(
                hwnd, GWLP_WNDPROC, ctypes.cast(self._chrome_proc, ctypes.c_void_p))
            _USER32.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                                 SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_FRAMECHANGED)
        except Exception:
            self._chrome_proc = None

    def _build_rail(self):
        rail = ctk.CTkFrame(self, width=RAIL_CLOSED, fg_color=RAIL, corner_radius=0)
        # Place directly: customtkinter refuses width/height on its own place().
        tk.Place.place(rail, x=0, y=TITLEBAR_HEIGHT, width=RAIL_CLOSED,
                       relheight=1.0, height=-TITLEBAR_HEIGHT)
        rail.grid_propagate(False)
        rail.grid_rowconfigure(2, weight=1)
        rail.grid_columnconfigure(0, weight=1)
        self.rail = rail
        self.rail_items = []      # (item frame, icon label, text label)
        self.rail_sections = []   # WORKSPACE / LIBRARY headings
        self.rail_nav_order = []  # nav children in display order
        self._rail_collapsed = False
        self._rail_pending = None
        self._rail_job = None
        self._rail_ready = False

        brand = tk.Frame(rail, bg=RAIL)
        brand.grid(row=0, column=0, sticky="ew", pady=(24, 0))
        brand.grid_columnconfigure(0, weight=1)
        self.rail_brand = brand
        self._rail_lockup_image = None
        self._rail_mark_image = None
        try:
            with Image.open(resource_path("assets", "logo_lockup.png")) as logo:
                lockup = logo.convert("RGBA")
            with Image.open(resource_path("assets", "logo_mark.png")) as mark:
                mark_image = mark.convert("RGBA")
            self._rail_lockup_image = ctk.CTkImage(
                light_image=lockup, dark_image=lockup, size=(152, 50))
            self._rail_mark_image = ctk.CTkImage(
                light_image=mark_image, dark_image=mark_image, size=(30, 27))
            self._rail_lockup = ctk.CTkLabel(
                brand, text="", image=self._rail_lockup_image)
            self._rail_lockup.grid(row=0, column=0, sticky="w", padx=(20, 0))
        except Exception:
            self._rail_lockup = ctk.CTkLabel(brand, text="SUPER", text_color=TEXT,
                                             font=("Segoe UI", 22, "bold"))
            self._rail_lockup.grid(row=0, column=0, sticky="w", padx=(20, 0))
        self._rail_tagline = flat_label(rail, "AI CLIPPING STUDIO", RAIL, FAINT,
                                        ("Segoe UI", 8, "bold"))
        self._rail_tagline.grid(row=1, column=0, sticky="w", padx=22, pady=(8, 0))

        nav = tk.Frame(rail, bg=RAIL)
        nav.grid(row=2, column=0, sticky="new", padx=12, pady=(18, 0))
        self.rail_nav = nav

        def section(text):
            heading = flat_label(nav, text, RAIL, "#5C5C72", ("Segoe UI", 8, "bold"))
            heading.pack(fill="x", padx=12, pady=(16, 5))
            heading.before_widget = None
            self.rail_sections.append(heading)
            self.rail_nav_order.append(heading)

        def coming(name):
            return lambda: self._show_popup(
                f"{name} is coming soon",
                f"{name} is not available in this build yet.\nStay tuned for the next Super update.",
                icon="✦", accent=PURPLE)

        section("WORKSPACE")
        self._nav_item(nav, "✦", "Create", active=True)
        self._nav_item(nav, "▤", "Highlights", command=coming("Highlights"))
        self._nav_item(nav, "◔", "Analytics", command=coming("Analytics"))

        section("LIBRARY")
        self._nav_item(nav, "◈", "Brand kit", command=coming("Brand kit"))
        self._nav_item(nav, "▣", "Clip library", command=coming("Clip library"))
        self._nav_item(nav, "⇩", "Exports", command=coming("Exports"))

        card = ctk.CTkFrame(rail, fg_color=CARD, corner_radius=14)
        card.grid(row=3, column=0, sticky="ew", padx=14, pady=(0, 8))
        self.rail_job = card
        top_row = tk.Frame(card, bg=CARD)
        top_row.pack(fill="x", padx=14, pady=(12, 0))
        self.rail_dot = flat_label(top_row, "●", CARD, GREEN, ("Segoe UI", 10, "bold"))
        self.rail_dot.pack(side="left")
        flat_label(top_row, "  Auto Clipper", CARD, TEXT, ("Segoe UI", 11, "bold")).pack(side="left")
        self.rail_state = flat_label(card, "Ready to render", CARD, MUTED, ("Segoe UI", 9))
        self.rail_state.pack(fill="x", padx=14, pady=(2, 0))
        bottom_row = tk.Frame(card, bg=CARD)
        bottom_row.pack(fill="x", padx=14, pady=(7, 12))
        bottom_row.grid_columnconfigure(0, weight=1)
        self.rail_bar = ctk.CTkProgressBar(bottom_row, height=4, corner_radius=2,
                                           fg_color="#1B1B26", progress_color=PURPLE)
        self.rail_bar.grid(row=0, column=0, sticky="ew")
        self.rail_bar.set(0)
        self.rail_percent = flat_label(bottom_row, "0%", CARD, FAINT, ("Segoe UI", 8))
        self.rail_percent.grid(row=0, column=1, padx=(7, 0))

        self._nav_item(rail, "⚙", "Settings", row=5, padx=20, pady=(2, 18),
                       command=lambda: self._show_popup("Settings", "App settings will be added here.",
                                                        icon="⚙", accent=PURPLE))
        self._rail_version = flat_label(rail, f"v{APP_VERSION}", RAIL, "#4E4E62", ("Segoe UI", 8))
        self._rail_version.grid(row=4, column=0, sticky="w", padx=22, pady=(2, 6))

        # Headings come back with pack(before=...) so they slot in above their
        # own group without re-packing every nav item.
        for index, widget in enumerate(self.rail_nav_order[:-1]):
            if widget in self.rail_sections:
                widget.before_widget = self.rail_nav_order[index + 1]

    def _nav_item(self, parent, glyph, label, command=None, active=False, row=None, padx=0, pady=0):
        item = ctk.CTkFrame(parent, fg_color=PURPLE_DARK if active else "transparent",
                            corner_radius=10, height=40)
        if row is None:
            item.pack(fill="x", pady=2)
            self.rail_nav_order.append(item)
        else:
            item.grid(row=row, column=0, sticky="ew", padx=padx, pady=pady)
        item.pack_propagate(False)
        resting = PURPLE_DARK if active else RAIL
        icon = flat_label(item, glyph, resting, PURPLE if active else "#8C8CA2",
                          ("Segoe UI Symbol", 14), width=4, anchor="center")
        icon.pack(side="left", padx=(12, 6))
        text = flat_label(item, label, resting, TEXT if active else "#B6B6C8",
                          ("Segoe UI", 12, "bold" if active else "normal"))
        text.pack(side="left")
        self.rail_items.append((item, icon, text))

        def paint(color):
            item.configure(fg_color="transparent" if color == RAIL else color)
            icon.configure(bg=color)
            text.configure(bg=color)

        def on_enter(_event=None):
            paint(PURPLE_DARK if active else "#16161F")
            item.configure(cursor="hand2" if command else "arrow")

        def on_leave(_event=None):
            paint(resting)

        for widget in [item, icon, text]:
            widget.bind("<Enter>", on_enter)
            widget.bind("<Leave>", on_leave)
            if command:
                widget.bind("<Button-1>", lambda _event: command())
                widget.configure(cursor="hand2")
        return item

    def _sync_rail_mode(self, _event=None):
        """Collapses the sidebar to icons on a narrow window, names on a wide one.

        The switch itself re-packs a dozen widgets, so it is deferred until the
        window has finished resizing: the frame then snaps to its new size
        instantly and the rail follows a moment later.
        """
        if not hasattr(self, "rail"):
            return
        collapsed = self.winfo_width() < RAIL_BREAKPOINT
        if collapsed == self._rail_collapsed or collapsed == self._rail_pending:
            return
        self._rail_pending = collapsed
        if not self._rail_ready:
            self._rail_ready = True
            self._apply_rail_mode(collapsed)
            return
        if self._rail_job is not None:
            try:
                self.after_cancel(self._rail_job)
            except Exception:
                self._rail_job = None
        self._rail_job = self.after(80, self._finish_rail_mode)

    def _finish_rail_mode(self):
        self._rail_job = None
        pending = self._rail_pending
        if pending is not None and pending != self._rail_collapsed:
            self._apply_rail_mode(pending)

    def _apply_rail_mode(self, collapsed):
        """Swaps the rail between icon-only and icon-plus-name.

        Packing and forgetting a dozen widgets made this the most expensive part
        of a zoom, so headings come back with a ``before`` reference instead of
        re-packing the whole nav, and the logo swaps its picture in place.
        """
        self._rail_collapsed = collapsed
        self._rail_pending = collapsed
        try:
            self.rail.place_configure(width=RAIL_CLOSED if collapsed else RAIL_OPEN)
            self.rail_nav.grid_configure(padx=8 if collapsed else 12,
                                         pady=(14, 0) if collapsed else (18, 0))
            image = self._rail_mark_image if collapsed else self._rail_lockup_image
            if image is not None:
                self._rail_lockup.configure(image=image)
                self._rail_lockup.grid_configure(padx=19 if collapsed else 20)
            else:
                self._rail_lockup.configure(text="" if collapsed else "SUPER")
            self._rail_tagline.configure(text="" if collapsed else "AI CLIPPING STUDIO")
            for heading in self.rail_sections:
                if collapsed:
                    heading.pack_forget()
                elif heading.before_widget is not None:
                    heading.pack(fill="x", padx=12, pady=(16, 5),
                                 before=heading.before_widget)
                else:
                    heading.pack(fill="x", padx=12, pady=(16, 5))
            for item, icon, text in self.rail_items:
                text.pack_forget()
                icon.pack_forget()
                if collapsed:
                    icon.pack(expand=True)
                else:
                    icon.pack(side="left", padx=(12, 6))
                    text.pack(side="left")
            if collapsed:
                self.rail_job.grid_remove()
                self._rail_version.grid_remove()
            else:
                self.rail_job.grid()
                self._rail_version.grid()
        except Exception:
            pass

    def _build_workspace(self):
        # The column is placed (not gridded) so it can be shifted up/down for
        # scrolling without resizing: its natural width stays fixed and it is
        # centred horizontally, so zooming only repaints children, never
        # re-lays them out.
        container = tk.Frame(self.workspace, bg=BG)
        container.grid_columnconfigure(0, weight=1)
        self.content_column = container

        top = tk.Frame(container, bg=BG)
        top.grid(row=0, column=0, sticky="ew")
        top.grid_columnconfigure(0, weight=1)
        flat_label(top, "✦  CREATE", BG, PURPLE, ("Segoe UI", 9, "bold")).grid(
            row=0, column=0, sticky="w")
        flat_label(top, "Make something super.", BG, TEXT, ("Segoe UI", 27, "bold")).grid(
            row=1, column=0, sticky="w", pady=(6, 0))
        flat_label(top, "Drop in a long video. Super finds the moments worth posting.", BG,
                   MUTED, ("Segoe UI", 12)).grid(row=2, column=0, sticky="w", pady=(5, 0))
        self.status = ctk.CTkLabel(top, text="●  Ready", text_color=GREEN, fg_color="#10251B",
                                   corner_radius=14, padx=13, pady=6, font=("Segoe UI", 11, "bold"))
        self.status.grid(row=0, column=1, rowspan=3, sticky="e")

        self._build_source_bar(container)
        self._build_link_banner(container)

        tools_head = tk.Frame(container, bg=BG)
        tools_head.grid(row=3, column=0, sticky="ew", pady=(30, 10))
        tools_head.grid_columnconfigure(0, weight=1)
        flat_label(tools_head, "TOOLS", BG, FAINT, ("Segoe UI", 9, "bold")).grid(
            row=0, column=0, sticky="w")
        flat_label(tools_head, "6 tools", BG, FAINT, ("Segoe UI", 9)).grid(
            row=0, column=1, sticky="e")

        self._build_tool_grid(container)
        self._build_action_panel(container)
        self._build_results_panel(container)
        self.after_idle(self._place_content)

    def _place_content(self):
        """Centres the content column in the workspace and applies the scroll
        offset, then positions the scrollbar. No child widget is resized, so
        zooming only triggers a cheap per-child repaint instead of a layout
        storm.
        """
        try:
            if not self.content_column.winfo_exists():
                return
            w = self.workspace.winfo_width()
            h = self.workspace.winfo_height()
        except Exception:
            return
        cw = self.content_column.winfo_reqwidth()
        ch = self.content_column.winfo_reqheight()
        x = max(0, (w - cw) // 2)
        y = 30 - int(self._scroll_pos)
        try:
            tk.Place.place(self.content_column, x=x, y=y, width=cw)
        except Exception:
            pass
        # Scrollbar thumb + visibility.
        total = ch + 76  # top (30) + bottom (46) padding
        viewport = h
        try:
            if total > viewport:
                self.workspace_scrollbar.place(relx=1.0, x=-2, y=0,
                                               relheight=1.0, anchor="ne")
                maximum = max(1, total - viewport)
                pos = min(1.0, max(0.0, self._scroll_pos / maximum))
                frac = min(1.0, viewport / total)
                self.workspace_scrollbar.set(pos, pos + frac)
            else:
                self.workspace_scrollbar.place_forget()
        except Exception:
            pass

    def _build_source_bar(self, parent):
        source = ctk.CTkFrame(parent, fg_color=FIELD, corner_radius=16)
        source.grid(row=1, column=0, sticky="ew", pady=(22, 0))
        source.grid_columnconfigure(1, weight=1)
        icon_box = ctk.CTkLabel(source, text="↗", width=38, height=38, corner_radius=10, fg_color=PURPLE_DARK,
                                text_color=PURPLE, font=("Segoe UI Symbol", 15, "bold"))
        icon_box.grid(row=0, column=0, padx=(16, 12), pady=14)
        icon_box.bind("<Button-1>", self._focus_source_entry)
        icon_box.configure(cursor="hand2")
        self.source_entry = ctk.CTkEntry(
            source, textvariable=self.video_url, height=42, fg_color="transparent", border_width=0,
            text_color=TEXT, font=("Segoe UI", 12),
        )
        self.source_entry.grid(row=0, column=1, sticky="ew", pady=11)
        # CTkEntry skips its own placeholder when a textvariable is bound, so
        # draw the hint as an overlay that disappears as soon as there is text.
        self.source_placeholder = flat_label(
            source, "Paste a video link (YouTube, TikTok, Instagram, Facebook, Twitch...)",
            FIELD, "#75758A", ("Segoe UI", 12))
        self.source_placeholder.grid(row=0, column=1, sticky="ew", padx=(11, 0), pady=11)
        self.source_placeholder.bind("<Button-1>", self._focus_source_entry)
        self._source_focused = False

        def on_focus_in(_event=None):
            self._source_focused = True
            self.source_placeholder.grid_remove()

        def on_focus_out(_event=None):
            self._source_focused = False
            self._sync_source_placeholder()

        self.source_entry.bind("<FocusIn>", on_focus_in)
        self.source_entry.bind("<FocusOut>", on_focus_out)
        self.source_entry.bind("<Return>", lambda _event: self._use_link())
        self.video_url.trace_add("write", self._on_url_changed)

        ctk.CTkButton(source, text="Choose file", width=130, height=40, corner_radius=10,
                      fg_color=PURPLE_DARK, hover_color="#32224F",
                      text_color="#D9CCFF", font=("Segoe UI", 11, "bold"),
                      command=self._choose_video).grid(row=0, column=2, padx=12, pady=11)

        self._sync_source_placeholder()

    def _sync_source_placeholder(self):
        placeholder = getattr(self, "source_placeholder", None)
        if placeholder is None:
            return
        if self.video_url.get().strip() or getattr(self, "_source_focused", False):
            placeholder.grid_remove()
        else:
            placeholder.grid()

    def _focus_source_entry(self, _event=None):
        """Sends a click on the hint (or its icon) into the paste field.

        The hint sits over the entry, and Tk gives the focus to the label's
        internal widget after this binding runs. Re-applying the focus once
        the event queue settles is what actually puts the caret in the entry,
        so a following Ctrl+V pastes into the field.
        """
        self._source_focused = True
        try:
            self.source_placeholder.grid_remove()
        except Exception:
            pass
        try:
            self.source_entry.focus_set()
        except Exception:
            pass
        self.after_idle(self._focus_source_entry_now)
        return "break"

    def _focus_source_entry_now(self):
        try:
            self.source_entry.focus_set()
            self.source_entry.icursor("end")
        except Exception:
            pass

    def _build_link_banner(self, parent):
        """Status line under the paste field: fetching / ready / unreadable."""
        self.link_banner = ctk.CTkFrame(
            parent, fg_color="#141122", corner_radius=12)
        self.link_banner.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self.link_banner.grid_columnconfigure(1, weight=1)
        self._link_wrap = 0
        self.link_banner_icon = ctk.CTkLabel(
            self.link_banner, text="◔", width=16, height=16, text_color="#B9A8FF",
            font=("Segoe UI Symbol", 12, "bold"))
        self.link_banner_icon.grid(row=0, column=0, padx=(14, 9), pady=11)
        self.link_banner_label = ctk.CTkLabel(
            self.link_banner, text="", text_color="#B9A8FF", anchor="w",
            justify="left", wraplength=880, font=("Segoe UI", 12))
        self.link_banner_label.grid(row=0, column=1, sticky="w", padx=(0, 14), pady=10)
        self.link_banner.bind("<Configure>", self._resize_link_banner)
        self.link_banner.grid_remove()

    def _resize_link_banner(self, event):
        wrap = max(220, event.width - 72)
        if wrap != self._link_wrap:
            self._link_wrap = wrap
            self.link_banner_label.configure(wraplength=wrap)

    def _build_tool_grid(self, parent):
        grid = tk.Frame(parent, bg=BG)
        grid.grid(row=4, column=0, sticky="ew")
        for column in range(3):
            grid.grid_columnconfigure(column, weight=1, uniform="tools")

        tools = [
            ("✦", "Auto Clipper", "Ready-to-post clips from a long video: the best moments, framed on the subject, with captions.",
             ["Finds the best moments", "Trust score with breakdown", "AI picks each clip's length"], "BETA"),
            ("★", "AI Best Moments", "Scans the whole video and suggests the strongest moments to clip.",
             ["Ranks moments by score", "Timestamps for each one", "Load any into the trimmer"], "COMING SOON"),
            ("✎", "Auto Editor", "Already know your moments? Pick them yourself and get the full edit.",
             ["Vertical framing", "Captions and headline", "Quick social export"], "COMING SOON"),
            ("✂", "Standard Trim", "Pick the exact start and end yourself and export a clean vertical clip.",
             ["Precise in and out points", "9:16 crop", "MP4 export"], "COMING SOON"),
            ("▣", "Auto Caption", "Add polished word-by-word captions to any video.",
             ["Accurate word timing", "Readable social styling", "Export-ready MP4"], "COMING SOON"),
            ("⇩", "MP3 / MP4 Downloader", "Save a public video or extract its audio for another workflow.",
             ["MP4 video export", "MP3 audio export", "Public links only"], "COMING SOON"),
        ]
        for index, (icon, title, description, bullets, badge) in enumerate(tools):
            card = self._tool_card(grid, icon, title, description, bullets, badge)
            card.grid(row=index // 3, column=index % 3, sticky="nsew", padx=(0 if index % 3 == 0 else 7,
                      0 if index % 3 == 2 else 7), pady=(0 if index < 3 else 14, 0))
            self.tool_cards[title] = card
        self._refresh_tool_cards()

    def _tool_card(self, parent, icon, title, description, bullets, badge):
        card = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=16)
        card.grid_columnconfigure(0, weight=1)

        icon_box = flat_label(card, icon, PURPLE_DARK, PURPLE,
                              ("Segoe UI Symbol", 18, "bold"))
        icon_box.grid(row=0, column=0, sticky="w", padx=18, pady=(18, 0))
        badge_bg = "#241A3A" if badge == "BETA" else "#1A1A22"
        badge_fg = "#C4AEFF" if badge == "BETA" else FAINT
        badge_label = flat_label(card, badge, badge_bg, badge_fg,
                                 ("Segoe UI", 8, "bold"))
        badge_label.grid(row=0, column=1, sticky="e", padx=18, pady=(20, 0))

        title_label = flat_label(card, title, CARD, TEXT, ("Segoe UI", 14, "bold"))
        title_label.grid(row=1, column=0, columnspan=2, sticky="ew", padx=18, pady=(16, 0))
        description_label = flat_label(card, description, CARD, MUTED, ("Segoe UI", 11),
                                       justify="left", wraplength=245)
        description_label.grid(row=2, column=0, columnspan=2, sticky="ew", padx=18, pady=(7, 14))
        bullet_text = "\n".join(f"•   {bullet}" for bullet in bullets)
        bullet_label = flat_label(card, bullet_text, CARD, "#83839A", ("Segoe UI", 10),
                                  justify="left")
        bullet_label.grid(row=3, column=0, columnspan=2, sticky="ew", padx=18, pady=(0, 18))
        # Plain labels do not inherit the card colour, so hover and selection
        # have to recolour them along with the rounded frame.
        card.tool_text = (title_label, description_label, bullet_label)

        def choose(_event=None):
            self._select_tool(title)

        def on_enter(_event=None):
            if title != self.selected_tool:
                self._paint_tool_card(card, CARD_HOVER)

        def on_leave(_event=None):
            if title != self.selected_tool:
                self._paint_tool_card(card, CARD)

        for widget in (card, icon_box, title_label, description_label, bullet_label, badge_label):
            widget.bind("<Button-1>", choose)
            widget.bind("<Enter>", on_enter)
            widget.bind("<Leave>", on_leave)
            widget.configure(cursor="hand2")
        return card

    def _build_action_panel(self, parent):
        self.action = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=16)
        self.action.grid(row=5, column=0, sticky="ew", pady=(24, 0))
        self.action.grid_columnconfigure(0, weight=1)
        self.action.grid_columnconfigure(1, weight=0)

        header = tk.Frame(self.action, bg=CARD)
        header.grid(row=0, column=0, sticky="w", padx=20, pady=(18, 0))
        ctk.CTkFrame(header, width=3, height=20, fg_color=PURPLE, corner_radius=2).pack(
            side="left", padx=(0, 10))
        self.action_title = flat_label(header, "Auto Clipper settings", CARD, TEXT,
                                       ("Segoe UI", 16, "bold"))
        self.action_title.pack(side="left")
        self.action_note = flat_label(
            self.action,
            "The AI finds the best moments. Your clip length is applied exactly.",
            CARD, MUTED, ("Segoe UI", 11))
        self.action_note.grid(row=1, column=0, sticky="w", padx=20, pady=(4, 14))

        controls = tk.Frame(self.action, bg=CARD)
        controls.grid(row=2, column=0, sticky="ew", padx=20, pady=(0, 14))
        controls.grid_columnconfigure(1, weight=1)

        def section(row, title):
            flat_label(controls, title, CARD, FAINT, ("Segoe UI", 9, "bold")).grid(
                row=row, column=0, sticky="w", pady=(14, 3))

        section(0, "CLIP")

        flat_label(controls, "Minimum score", CARD, MUTED, ("Segoe UI", 11)).grid(
            row=1, column=0, sticky="w")
        self.score_display = flat_label(controls, "30", CARD, TEXT, ("Segoe UI", 12, "bold"))
        self.score_display.grid(row=1, column=2, sticky="e")
        self.score_slider = ctk.CTkSlider(controls, from_=0, to=100, number_of_steps=20, height=16,
                                          fg_color="#262633", progress_color=PURPLE,
                                          button_color="#C9B6FF", button_hover_color="#DED2FF",
                                          command=self._update_score)
        self.score_slider.set(30)
        self.score_slider.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 0))

        flat_label(controls, "Clip length", CARD, MUTED, ("Segoe UI", 11)).grid(
            row=3, column=0, sticky="w", pady=(17, 0))
        self.duration_display = flat_label(controls, "30s", CARD, TEXT, ("Segoe UI", 12, "bold"))
        self.duration_display.grid(row=3, column=2, sticky="e", pady=(17, 0))
        self.duration_slider = ctk.CTkSlider(controls, from_=15, to=90, number_of_steps=15, height=16,
                                             fg_color="#262633", progress_color=PURPLE,
                                             button_color="#C9B6FF", button_hover_color="#DED2FF",
                                             command=self._update_duration)
        self.duration_slider.set(30)
        self.duration_slider.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8, 0))

        flat_label(controls, "Aspect ratio", CARD, MUTED, ("Segoe UI", 11)).grid(
            row=5, column=0, sticky="w", pady=(17, 0))
        self.ratio_selector = ctk.CTkSegmentedButton(
            controls, values=["Original", "9:16", "16:9"], variable=self.aspect_ratio,
            selected_color=PURPLE, selected_hover_color=PURPLE_HOVER,
            unselected_color="#1C1C26", unselected_hover_color="#282834",
            text_color=TEXT, font=("Segoe UI", 11, "bold"),
            command=self._update_preview
        )
        self.ratio_selector.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(8, 0))

        section(7, "CAPTIONS")

        self.caption_size = tk.IntVar(value=70)
        self.size_label = flat_label(controls, "Caption size: 70", CARD, TEXT, ("Segoe UI", 12, "bold"))
        self.size_label.grid(row=8, column=2, sticky="e", pady=(12, 0))
        self.size_slider = ctk.CTkSlider(controls, from_=30, to=150, number_of_steps=24, height=16,
                                         fg_color="#262633", progress_color=PURPLE,
                                         button_color="#C9B6FF", button_hover_color="#DED2FF",
                                         command=self._update_size_label)
        self.size_slider.set(70)
        self.size_slider.grid(row=9, column=0, columnspan=3, sticky="ew", pady=(8, 0))

        self.preview_frame = ctk.CTkFrame(self.action, width=250, height=386, fg_color=FIELD, corner_radius=14)
        self.preview_frame.grid(row=2, column=1, sticky="nsew", padx=(20, 20), pady=(0, 20))
        self.preview_frame.grid_propagate(False)
        self.preview_frame.pack_propagate(False)
        self.preview_img = ctk.CTkLabel(self.preview_frame, text="No Preview", text_color=MUTED)
        self.preview_ratio_label = flat_label(self.preview_frame, "", FIELD, FAINT,
                                              ("Segoe UI", 10, "bold"))
        self.preview_ratio_label.configure(anchor="center")
        # Pack (not place) so the image stays centred and the caption label can
        # never overlap it, even when Windows display scaling is not 100%.
        self.preview_ratio_label.pack(side="bottom", pady=(0, 9))
        self.preview_img.pack(expand=True, fill="both", padx=10, pady=(10, 0))

        footer = tk.Frame(self.action, bg=CARD)
        footer.grid(row=3, column=0, columnspan=2, sticky="ew", padx=20, pady=(0, 18))
        footer.grid_columnconfigure(0, weight=1)
        ctk.CTkSwitch(footer, text="Burn in word-by-word captions", variable=self.captions, text_color=MUTED,
                      font=("Segoe UI", 11), progress_color=PURPLE, button_color=TEXT,
                      command=self._on_caption_toggle).grid(row=0, column=0, sticky="w")
        self.run_button = ctk.CTkButton(footer, text="✦  Generate clips", width=180, height=42, corner_radius=10,
                                        fg_color=PURPLE, hover_color=PURPLE_HOVER, text_color="#FFFFFF",
                                        font=("Segoe UI", 12, "bold"), command=self._start)
        self.run_button.grid(row=0, column=1, sticky="e")

        progress_card = ctk.CTkFrame(self.action, fg_color="#0B0B11", corner_radius=12)
        progress_card.grid(row=4, column=0, columnspan=2, sticky="ew", padx=20, pady=(0, 20))
        progress_card.grid_columnconfigure(0, weight=1)
        self.progress_stage = flat_label(
            progress_card,
            "Ready when you are. Choose a source video, then generate clips.",
            "#0B0B11", "#A0A0B4", ("Segoe UI", 11))
        self.progress_stage.grid(row=0, column=0, sticky="w", padx=(16, 10), pady=(13, 0))
        self.progress_percent = flat_label(progress_card, "0%", "#0B0B11", TEXT,
                                           ("Segoe UI", 13, "bold"))
        self.progress_percent.grid(row=0, column=1, sticky="e", padx=(0, 16), pady=(13, 0))
        self.progress = ctk.CTkProgressBar(
            progress_card, height=10, corner_radius=6, fg_color="#1B1B26",
            progress_color=PURPLE)
        self.progress.grid(row=1, column=0, columnspan=2, sticky="ew", padx=16, pady=(9, 15))
        self.progress.set(0)

    def _show_popup(self, title, message, icon="✓", accent=PURPLE,
                    primary_text="OK", secondary_text=None,
                    on_primary=None, on_secondary=None):
        """Styled modal popup that matches the app theme."""
        popup = ctk.CTkToplevel(self)
        popup.title(title)
        popup.configure(fg_color=CARD)
        popup.overrideredirect(True)
        popup.grab_set()

        width, height = 430, 252
        self.update_idletasks()
        x = self.winfo_rootx() + max(0, (self.winfo_width() - width) // 2)
        y = self.winfo_rooty() + max(0, (self.winfo_height() - height) // 2)
        popup.geometry(f"{width}x{height}+{x}+{y}")
        popup.attributes("-alpha", 0.0)

        bar = ctk.CTkFrame(popup, fg_color="transparent")
        bar.pack(fill="x", padx=16, pady=(14, 0))
        ctk.CTkLabel(bar, text=title, text_color=TEXT, font=("Segoe UI", 14, "bold")).pack(side="left")
        ctk.CTkButton(bar, text="✕", width=28, height=28, corner_radius=8, fg_color="transparent",
                      hover_color="#2A2A34", text_color=MUTED, command=popup.destroy).pack(side="right")

        def start_move(event):
            popup._drag_x, popup._drag_y = event.x, event.y

        def on_drag(event):
            popup.geometry(f"+{popup.winfo_x() + event.x - popup._drag_x}"
                           f"+{popup.winfo_y() + event.y - popup._drag_y}")

        bar.bind("<Button-1>", start_move)
        bar.bind("<B1-Motion>", on_drag)

        body = ctk.CTkFrame(popup, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=24, pady=(6, 18))

        ctk.CTkLabel(body, text=icon, width=54, height=54, corner_radius=27, fg_color=accent,
                     text_color="#0B0B0F", font=("Segoe UI", 26, "bold")).pack(pady=(8, 12))
        ctk.CTkLabel(body, text=message, text_color="#C9C9D6", font=("Segoe UI", 12),
                     justify="left", wraplength=360).pack()

        buttons = ctk.CTkFrame(body, fg_color="transparent")
        buttons.pack(pady=(18, 0))

        def run(callback):
            def handler():
                try:
                    if callback:
                        callback()
                finally:
                    popup.destroy()
            return handler

        if secondary_text:
            ctk.CTkButton(buttons, text=secondary_text, width=130, height=38, corner_radius=9,
                          fg_color="#20202A", hover_color="#2A2A34",
                          text_color=TEXT, font=("Segoe UI", 11, "bold"),
                          command=run(on_secondary)).pack(side="left", padx=(0, 10))

        ctk.CTkButton(buttons, text=primary_text, width=130, height=38, corner_radius=9,
                      fg_color=PURPLE, hover_color=PURPLE_HOVER, text_color=TEXT,
                      font=("Segoe UI", 11, "bold"), command=run(on_primary)).pack(side="left")

        popup.bind("<Escape>", lambda event: popup.destroy())

        def fade(step=0):
            if step <= 10:
                popup.attributes("-alpha", step / 10)
                popup.after(18, lambda: fade(step + 1))

        popup.lift()
        popup.update_idletasks()
        self._enable_alt_tab(popup)
        popup.focus_force()
        fade()
        return popup

    def _enable_alt_tab(self, window):
        """Makes an overrideredirect popup visible in Alt+Tab and the taskbar."""
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            user32.GetParent.argtypes = [wintypes.HWND]
            user32.GetParent.restype = wintypes.HWND
            user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.GetWindowLongW.restype = ctypes.c_long
            user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
            user32.SetWindowLongW.restype = ctypes.c_long
            user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                            ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                            ctypes.c_uint]

            GWL_EXSTYLE = -20
            WS_EX_TOOLWINDOW = 0x00000080
            WS_EX_APPWINDOW = 0x00040000
            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_NOZORDER = 0x0004
            SWP_FRAMECHANGED = 0x0020

            # Tk's real top-level handle is the wrapper returned by GetParent.
            hwnd = user32.GetParent(window.winfo_id()) or window.winfo_id()
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            new_style = (style & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW
            if new_style != style:
                user32.SetWindowLongW(hwnd, GWL_EXSTYLE, new_style)
                user32.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                                    SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_FRAMECHANGED)
        except Exception:
            pass


    def _build_results_panel(self, parent):
        header = tk.Frame(parent, bg=BG)
        header.grid(row=6, column=0, sticky="ew", pady=(30, 10))
        header.grid_columnconfigure(0, weight=1)
        flat_label(header, "RESULTS", BG, FAINT, ("Segoe UI", 9, "bold")).grid(
            row=0, column=0, sticky="w")
        self.results_count = flat_label(header, "", BG, MUTED, ("Segoe UI", 10))
        self.results_count.grid(row=0, column=1, sticky="e", padx=(0, 12))
        ctk.CTkButton(header, text="Open folder", width=100, height=30, corner_radius=9,
                      fg_color="#1C1C26", hover_color="#282834",
                      text_color=TEXT, font=("Segoe UI", 10, "bold"),
                      command=self._open_output_folder).grid(row=0, column=2, sticky="e")

        self.results_frame = ctk.CTkScrollableFrame(
            parent, height=300, fg_color=CARD, corner_radius=16,
            scrollbar_button_color="#2B2B34", scrollbar_button_hover_color="#3A3A46",
        )
        self.results_frame.grid(row=7, column=0, sticky="ew")
        self._results_placeholder = flat_label(
            self.results_frame,
            "No results yet. Generate clips and the AI trust report will appear here.",
            CARD, MUTED, ("Segoe UI", 11))
        self._results_placeholder.pack(pady=48)

    def _trust_style(self, confidence):
        return {
            "high": ("HIGH TRUST", GREEN, "#102820"),
            "medium": ("MEDIUM TRUST", "#F4C56A", "#352A12"),
            "low": ("LOW TRUST", "#FF9696", "#351923"),
        }.get(confidence, ("LOW TRUST", "#FF9696", "#351923"))

    def _score_color(self, score):
        if score >= 70:
            return GREEN
        if score >= 45:
            return "#F4C56A"
        return "#FF9696"

    def _breakdown_bar(self, parent, label, value, column):
        ctk.CTkLabel(parent, text=label, text_color=FAINT, font=("Segoe UI", 10)).grid(
            row=0, column=column, sticky="w", padx=(0, 6))
        bar = ctk.CTkProgressBar(parent, width=110, height=8, corner_radius=4,
                                 fg_color="#262633", progress_color=PURPLE)
        bar.set(max(0, min(100, int(value or 0))) / 100.0)
        bar.grid(row=0, column=column + 1, sticky="w", padx=(0, 6))
        ctk.CTkLabel(parent, text=str(int(value or 0)), text_color=TEXT,
                     font=("Segoe UI", 10, "bold")).grid(row=0, column=column + 2, sticky="w",
                                                         padx=(0, 18))

    def _result_card(self, parent, clip, rank):
        score = int(clip.get("score", 0))
        trust_text, trust_color, trust_bg = self._trust_style(clip.get("confidence", "low"))
        length = clip.get("length_seconds")
        if length is None and "start" in clip and "end" in clip:
            length = round(clip["end"] - clip["start"])

        card = ctk.CTkFrame(parent, fg_color="#0E0E15", corner_radius=14)
        card.pack(fill="x", padx=12, pady=7)
        card.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(card, text=str(rank), text_color="#C9B6FF", fg_color=PURPLE_DARK, width=34, height=34,
                     corner_radius=9, font=("Segoe UI", 12, "bold")).grid(
            row=0, column=0, sticky="w", padx=(16, 12), pady=(16, 4))
        ctk.CTkLabel(card, text=str(score), text_color=self._score_color(score),
                     font=("Segoe UI", 22, "bold")).grid(row=0, column=1, sticky="w", pady=(12, 4))
        ctk.CTkLabel(card, text=trust_text, text_color=trust_color, fg_color=trust_bg,
                     corner_radius=10, padx=10, pady=4, font=("Segoe UI", 9, "bold")).grid(
            row=0, column=2, sticky="w", padx=(14, 0), pady=(18, 4))

        right_info = ctk.CTkFrame(card, fg_color="transparent")
        right_info.grid(row=0, column=3, sticky="e", padx=16, pady=(12, 4))
        if length is not None:
            ctk.CTkLabel(right_info, text=f"{int(length)}s", text_color=TEXT, fg_color="#20202A",
                         corner_radius=8, padx=10, pady=4, font=("Segoe UI", 11, "bold")).pack(side="right")
        if clip.get("hook") is not None:
            ctk.CTkLabel(right_info, text="AI scored", text_color="#858596",
                         font=("Segoe UI", 9)).pack(side="right", padx=10)

        if clip.get("hook") is not None:
            bars = ctk.CTkFrame(card, fg_color="transparent")
            bars.grid(row=1, column=0, columnspan=4, sticky="w", padx=16, pady=(4, 0))
            self._breakdown_bar(bars, "Hook", clip.get("hook"), 0)
            self._breakdown_bar(bars, "Retention", clip.get("retention"), 3)
            self._breakdown_bar(bars, "Payoff", clip.get("payoff"), 6)

        ctk.CTkLabel(card, text=clip.get("reason", ""), text_color="#B9B9C8", anchor="w", justify="left",
                     wraplength=760, font=("Segoe UI", 11)).grid(
            row=2, column=0, columnspan=4, sticky="ew", padx=16, pady=(8, 2))

        file_label = clip.get("file") or clip.get("path") or ""
        preview = (clip.get("text") or "")[:140]
        caption = "  |  ".join(part for part in (file_label, preview) if part)
        if caption:
            ctk.CTkLabel(card, text=caption, text_color="#7D7D8E", anchor="w", justify="left",
                         wraplength=760, font=("Segoe UI", 9)).grid(
                row=3, column=0, columnspan=4, sticky="ew", padx=16, pady=(0, 12))

    def _render_results(self):
        for widget in self.results_frame.winfo_children():
            widget.destroy()
        entries = sorted(self._result_clips, key=lambda clip: clip.get("score", 0), reverse=True)
        self.results_count.configure(text=f"{len(entries)} clip(s)" if entries else "")
        if not entries:
            self._results_placeholder = flat_label(
                self.results_frame,
                "No results yet.\nGenerate clips and the AI trust report will appear here.",
                CARD, MUTED, ("Segoe UI", 11), justify="center")
            self._results_placeholder.pack(pady=48)
            return
        for rank, clip in enumerate(entries, start=1):
            self._result_card(self.results_frame, clip, rank)

    def _append_result(self, clip):
        self._result_clips.append(clip)
        self._render_results()

    def _show_results(self, clips):
        self._result_clips = [
            clip if isinstance(clip, dict) else {"file": os.path.basename(str(clip)), "score": 0}
            for clip in clips
        ]
        self._render_results()

    def _open_output_folder(self):
        output = self.output_path.get()
        if os.path.isdir(output):
            os.startfile(output)
        else:
            self._show_popup("Nothing to open yet", f"The folder does not exist yet:\n{output}", icon="!", accent="#F4C56A")

    def _select_tool(self, tool):
        self.selected_tool = tool
        available = tool == "Auto Clipper"
        self._refresh_tool_cards()
        if available:
            self.action_title.configure(text="Auto Clipper settings")
            self.action_note.configure(text="The AI finds the best moments. Your clip length is applied exactly.")
            self.run_button.configure(state="normal", text="✦  Generate clips")
        else:
            self.action_title.configure(text=f"{tool} is coming soon")
            self.action_note.configure(text="The interface is ready, but this tool needs its own editing engine before it can run.")
            self.run_button.configure(state="disabled", text="Coming soon")

    def _paint_tool_card(self, card, color):
        """Recolours a tool card and the plain labels sitting on it."""
        card.configure(fg_color=color)
        for label in getattr(card, "tool_text", ()):
            label.configure(bg=color)

    def _refresh_tool_cards(self):
        for title, card in self.tool_cards.items():
            active = title == self.selected_tool
            self._paint_tool_card(card, CARD_ACTIVE if active else CARD)

    def _update_score(self, value):
        score = int(round(float(value)))
        self.score_value.set(score)
        self.score_display.configure(text=str(score))

    def _update_duration(self, value):
        duration = int(round(float(value)))
        self.duration_value.set(duration)
        self.duration_display.configure(text=f"{duration}s")

    def _update_size_label(self, value):
        size = int(round(float(value)))
        self.caption_size.set(size)
        self.size_label.configure(text=f"Caption size: {size}")
        self._refresh_preview()

    def _on_caption_toggle(self):
        self._refresh_preview()

    def _render_preview_for_ratio(self):
        """Crops the cached frame to the selected ratio, then redraws instantly."""
        raw = self._preview_raw_img
        if raw is None:
            return
        ratio = self.aspect_ratio.get()
        self._preview_ratio_img = crop_image_to_ratio(raw, ratio) or raw
        self._refresh_preview()

    def _refresh_preview(self):
        """Redraws the cached preview using the current ratio, caption size and toggle."""
        img = self._preview_ratio_img
        if img is None:
            return

        try:
            ui_scale = ctk.ScalingTracker.get_widget_scaling(self.preview_frame) or 1.0
        except Exception:
            ui_scale = 1.0

        frame_w = self.preview_frame.winfo_width()
        frame_h = self.preview_frame.winfo_height()
        if frame_w < 50 or frame_h < 50:
            frame_w = int(250 * ui_scale)
            frame_h = int(386 * ui_scale)

        # Work in physical pixels so the frame/caption land where they should,
        # then hand CTkImage a logical size (it multiplies by UI scaling again).
        max_w = max(40, frame_w - int(24 * ui_scale))
        max_h = max(40, frame_h - int(58 * ui_scale))

        fit = min(max_w / img.width, max_h / img.height)
        display_w = max(1, int(img.width * fit))
        display_h = max(1, int(img.height * fit))
        preview = img.resize((display_w, display_h), Image.LANCZOS)

        ratio = self.aspect_ratio.get()
        if self.captions.get():
            # Match the real export: caption size is relative to the output frame.
            target_height = {"9:16": 1920, "16:9": 1080}.get(
                ratio, self._preview_source_height or 1080)
            font_size = max(8, int(round(self.caption_size.get() * display_h
                                         / max(target_height, 1))))
            font = self._preview_font(font_size)
            if font is not None:
                draw = ImageDraw.Draw(preview)
                text = "SAMPLE"
                stroke = max(1, font_size // 12)
                box = draw.textbbox((0, 0), text, font=font, stroke_width=stroke)
                x = (display_w - (box[2] - box[0])) / 2 - box[0]
                y = (display_h - (box[3] - box[1])) / 2 - box[1]
                draw.text((x, y), text, font=font, fill="#FFFF00",
                          stroke_width=stroke, stroke_fill="#000000")

        self.current_preview_img = ctk.CTkImage(
            light_image=preview, dark_image=preview,
            size=(max(1, round(display_w / ui_scale)),
                  max(1, round(display_h / ui_scale))))
        self.preview_img.configure(image=self.current_preview_img, text="")

        dims = {"9:16": "1080×1920", "16:9": "1920×1080"}.get(ratio)
        self.preview_ratio_label.configure(text=f"{ratio} · {dims}" if dims else "Original")

    def _preview_font(self, size):
        size = max(6, min(int(size), 200))
        font_path = self._preview_font_path()
        if font_path:
            try:
                return ImageFont.truetype(font_path, size)
            except Exception:
                pass
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()

    def _preview_font_path(self):
        for candidate in (
            r"C:\Windows\Fonts\arialbd.ttf",
            r"C:\Windows\Fonts\segoeuib.ttf",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ):
            if os.path.exists(candidate):
                return candidate
        return None

    # ---------------------------------------------------------------- link info
    def _on_url_changed(self, *_):
        self._sync_source_placeholder()
        self._schedule_link_fetch()

    def _schedule_link_fetch(self):
        """Debounced: runs once the pasted text settles on a real link."""
        job, self._link_fetch_job = self._link_fetch_job, None
        if job is not None:
            try:
                self.after_cancel(job)
            except Exception:
                pass
        if not looks_like_link(self.video_url.get()):
            self._link_fetch_token += 1  # ignore reads still in flight
            self._link_fetch_inflight = False
            self._hide_link_banner()
            if not self.video_path.get().strip():
                # A half-typed or cleared link must not leave the panel on
                # "Loading preview..." from the link that was there before.
                self._reset_preview("No Preview")
            return
        self._link_fetch_job = self.after(380, self._begin_link_fetch)

    def _begin_link_fetch(self):
        self._link_fetch_job = None
        url = self.video_url.get().strip()
        if not looks_like_link(url):
            return
        cached = self._link_cache.get(url)
        if cached is not None:
            self._link_fetch_inflight = False
            self._show_link_ready(cached)
            return
        if self._link_fetch_inflight and self._link_fetch_url == url:
            return
        self._link_fetch_token += 1
        token = self._link_fetch_token
        self._link_fetch_url = url
        self._link_fetch_inflight = True
        self._show_link_fetching()
        threading.Thread(target=self._link_info_worker, args=(url, token),
                         daemon=True).start()

    def _link_info_worker(self, url, token):
        try:
            info = extract_link_info(url)
        except Exception as exc:
            self.events.put(("link_error", (token, url, short_error(exc))))
            return
        self.events.put(("link_info", (token, url, info)))

    def _show_link_banner(self, text, color, background, icon):
        self._stop_link_animation()
        self.link_banner.configure(fg_color=background)
        self.link_banner_icon.configure(text=icon, text_color=color)
        self.link_banner_label.configure(text=text, text_color=color)
        if not self.link_banner.winfo_manager():
            self.link_banner.grid()

    def _show_link_fetching(self):
        self._show_link_banner("Fetching video info", "#B9A8FF", "#141122", "◔")
        self._link_banner_dots = 0
        self._tick_link_dots()

    def _tick_link_dots(self):
        self._link_banner_dots = self._link_banner_dots % 3 + 1
        self.link_banner_label.configure(
            text="Fetching video info" + "." * self._link_banner_dots)
        self._link_banner_job = self.after(420, self._tick_link_dots)

    def _stop_link_animation(self):
        job, self._link_banner_job = self._link_banner_job, None
        if job is not None:
            try:
                self.after_cancel(job)
            except Exception:
                pass

    def _show_link_ready(self, info):
        self._show_link_banner(format_link_title(info.get("title", "")),
                               "#6EE7A8", "#0E1C16", "✓")

    def _show_link_error(self, message):
        text = "Couldn't read that link"
        if message:
            text = f"{text}. {message}"
        if len(text) > 130:
            text = text[:129].rstrip(" ,.;:-") + "…"
        self._show_link_banner(text, "#FF9696", "#1F1416", "!")

    def _hide_link_banner(self):
        self._stop_link_animation()
        if self.link_banner.winfo_manager():
            self.link_banner.grid_remove()

    def _fetch_thumbnail(self, url, thumb_url=""):
        """Returns thumbnail bytes for a link without extracting metadata again.

        Only image URLs are fetched here. Running a second yt-dlp extraction
        just to find the thumbnail could stall for good (bot checks, rate
        limits, expiring URLs), which left the panel stuck on "Loading
        preview...". The metadata pass already ran for this link, so reuse the
        thumbnail it found, and fall back to YouTube's predictable image URLs.
        """
        candidates = []
        for candidate in (thumb_url, (self._link_cache.get(url) or {}).get("thumbnail")):
            if candidate and candidate not in candidates:
                candidates.append(candidate)
        for candidate in candidates:
            data = self._download_image(candidate)
            if data:
                return data
        match = re.search(r"(?:v=|/shorts/|/embed/|youtu\.be/)([A-Za-z0-9_-]{11})", url)
        if match:
            for quality in ("maxresdefault", "hqdefault"):
                data = self._download_image(
                    f"https://i.ytimg.com/vi/{match.group(1)}/{quality}.jpg", min_bytes=5000)
                if data:
                    return data
        return None

    @staticmethod
    def _download_image(thumb_url, min_bytes=1000, timeout=12):
        """Reads one image URL, returning None instead of raising or hanging."""
        if not thumb_url:
            return None
        try:
            request = urllib.request.Request(thumb_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = response.read()
        except Exception:
            return None
        return data if len(data) > min_bytes else None

    def _update_preview(self, _=None, scroll: bool = True):
        self._preview_autoscroll = scroll
        video = self.video_path.get().strip()
        url = self.video_url.get().strip()

        if not video and url.startswith(("https://", "http://")):
            if self._preview_source_key == ("url", url) and self._preview_raw_img is not None:
                self._render_preview_for_ratio()
                return
            self._preview_raw_img = None
            self._preview_ratio_img = None
            self._preview_source_key = None
            self._preview_token += 1
            token = self._preview_token
            # CustomTkinter ignores image=None and leaves the old PhotoImage on
            # the label, so clear it with "" and drop our reference afterwards.
            self.preview_img.configure(image="", text="Loading preview...")
            self.current_preview_img = None
            self.preview_ratio_label.configure(text="")
            self._arm_preview_deadline(token, url)
            cached = self._link_cache.get(url)
            if cached is not None:
                self._start_url_preview(url, token, str(cached.get("thumbnail") or ""))
            elif not (self._link_fetch_inflight and self._link_fetch_url == url):
                # No metadata job is running for this link yet. Start one; its
                # result arrives as a "link_info" event and carries the
                # thumbnail URL with it.
                self._begin_link_fetch()
            return

        self._preview_token += 1
        self._cancel_preview_deadline()

        if not video:
            self._reset_preview("No Video Selected")
            return

        if not os.path.isfile(video):
            self._reset_preview("Invalid File")
            return

        key = ("file", os.path.abspath(video))
        if self._preview_source_key != key or self._preview_raw_img is None:
            try:
                img, source_height = get_preview_frame(video, "Original", with_size=True,
                                                        max_size=480)
            except Exception as exc:
                self._reset_preview("Preview failed")
                self._write_log(f"Preview error: {exc}")
                return
            if img is None:
                self._reset_preview("No Preview")
                return
            self._preview_raw_img = img
            self._preview_source_key = key
            self._preview_source_height = source_height or 1080
            self._write_log("Preview ready.")
        self._render_preview_for_ratio()
        self.after(60, self._scroll_to_preview)

    def _install_smooth_scroll(self):
        """Scrolls the content in short eased frames instead of 96 px jumps.

        CustomTkinter moves the canvas and every widget inside it as real
        Windows child windows, and a forced redraw of all of them costs ~30 ms
        per wheel notch - that is what made scrolling feel stuck. Here a wheel
        notch only moves a target, and a ticker walks the visible offset toward
        it in ~15 ms frames (~4 ms each) so the content glides. Drags and
        clicks on the scrollbar still land exactly where the user points.
        """
        self.unbind_all("<MouseWheel>")
        self.bind_all("<MouseWheel>", self._on_mouse_wheel)
        # results_frame keeps its canvas; the workspace scrollbar was wired in
        # _build (_on_workspace_scroll).
        for area in (self.results_frame,):
            try:
                area._scrollbar.configure(command=partial(self._on_scroll_command, area))
            except Exception:
                pass
        self._install_repaint()

    def _install_repaint(self):
        """Looks up RedrawWindow, used to repaint a scrolled frame."""
        self._redraw = None
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            user32.RedrawWindow.argtypes = (wintypes.HWND, ctypes.c_void_p,
                                            ctypes.c_void_p, wintypes.UINT)
            user32.RedrawWindow.restype = wintypes.BOOL
        except Exception:
            return
        self._redraw = user32.RedrawWindow

    def _scroll_area_at(self, widget):
        """Finds which scroll area the pointer is over, if any."""
        node = widget
        while node is not None:
            if isinstance(node, (ctk.CTkSlider, ctk.CTkTextbox, ctk.CTkScrollbar)):
                return None
            if node is self.workspace or node is self.results_frame:
                return node
            if isinstance(node, ctk.CTkScrollableFrame):
                return node if node in (self.workspace, self.results_frame) else None
            if node is getattr(self, "rail", None):
                # The rail floats over the workspace, so the wheel over the
                # sidebar should still scroll the page.
                return self.workspace
            node = getattr(node, "master", None)
        return None

    def _on_mouse_wheel(self, event):
        area = self._scroll_area_at(event.widget)
        if area is None:
            return "break"
        delta = int(getattr(event, "delta", 0) or 0)
        if not delta:
            return
        steps = delta / 120 if abs(delta) >= 120 else (1 if delta > 0 else -1)
        self._add_scroll(area, -steps * SCROLL_NOTCH_PX)
        return "break"

    def _on_scroll_command(self, area, *args):
        """Receives the scrollbar's moveto/scroll commands."""
        if not args:
            return
        if args[0] == "moveto":
            try:
                self._jump_scroll(area, float(args[1]))
            except (TypeError, ValueError):
                pass
        elif args[0] == "scroll":
            try:
                amount = float(args[1])
            except (IndexError, TypeError, ValueError):
                return
            unit = args[2] if len(args) > 2 else "units"
            self._add_scroll(area, amount * (SCROLL_NOTCH_PX * 3 if unit == "pages" else 24))

    def _on_workspace_scroll(self, *args):
        """Receives the workspace scrollbar's moveto/scroll commands."""
        if not args:
            return
        content, maximum = self._scroll_metrics(self.workspace)
        if content <= 0:
            return
        if args[0] == "moveto":
            try:
                fraction = float(args[1])
            except (TypeError, ValueError):
                return
            self._scroll_targets.pop(self.workspace, None)
            self._scroll_pos = max(0.0, min(maximum, fraction * content))
        elif args[0] == "scroll":
            try:
                amount = float(args[1])
            except (IndexError, TypeError, ValueError):
                return
            unit = args[2] if len(args) > 2 else "units"
            self._add_scroll(self.workspace,
                             amount * (SCROLL_NOTCH_PX * 3 if unit == "pages" else 24))
        self._place_content()

    def _scroll_metrics(self, area):
        """Returns (content height, furthest offset) for a scroll area."""
        if area is self.workspace:
            content = float(self.content_column.winfo_reqheight()) + 76.0
            viewport = float(self.workspace.winfo_height())
            return content, max(0.0, content - viewport)
        canvas = getattr(area, "_parent_canvas", None)
        if canvas is None:
            return 0.0, 0.0
        box = canvas.bbox("all")
        content = float(box[3] - box[1]) if box else 0.0
        viewport = float(canvas.winfo_height())
        return content, max(0.0, content - viewport)

    def _scroll_offset(self, area, content):
        if area is self.workspace:
            return self._scroll_pos if content > 0 else 0.0
        canvas = getattr(area, "_parent_canvas", None)
        if canvas is None or content <= 0:
            return 0.0
        top, _bottom = canvas.yview()
        return top * content

    def _add_scroll(self, area, pixels):
        """Adds wheel distance to the target and starts the frame ticker."""
        content, maximum = self._scroll_metrics(area)
        if content <= 0:
            return
        base = self._scroll_targets.get(area)
        if base is None:
            base = self._scroll_offset(area, content)
        self._scroll_targets[area] = max(0.0, min(maximum, base + pixels))
        if self._scroll_job is None:
            self._scroll_tick()

    def _jump_scroll(self, area, fraction):
        """Scrollbar drag: land exactly where the user put the thumb.

        Dragging fires a stream of moveto events, so this only asks Tk to
        redraw on idle; forcing a synchronous repaint per event was what made
        dragging the thumb lag.
        """
        content, maximum = self._scroll_metrics(area)
        canvas = getattr(area, "_parent_canvas", None)
        if canvas is None or content <= 0:
            return
        target = max(0.0, min(maximum, float(fraction) * content))
        self._scroll_targets.pop(area, None)
        canvas.yview_moveto(max(0.0, min(1.0, target / content)))
        try:
            canvas.update_idletasks()
        except Exception:
            pass

    def _scroll_tick(self):
        """Moves every scrolling area one eased step toward its target."""
        self._scroll_job = None
        keep_going = False
        for area, target in list(self._scroll_targets.items()):
            if area is self.workspace:
                content, maximum = self._scroll_metrics(area)
                if content <= 0:
                    self._scroll_targets.pop(area, None)
                    continue
                target = max(0.0, min(maximum, target))
                current = self._scroll_offset(area, content)
                remaining = target - current
                if abs(remaining) < 0.5:
                    self._scroll_targets.pop(area, None)
                    continue
                if abs(remaining) <= SCROLL_SNAP_PX:
                    self._scroll_targets.pop(area, None)
                    self._scroll_pos = target
                    self._place_content()
                    continue
                step = remaining * SCROLL_EASE
                if abs(step) > SCROLL_MAX_STEP:
                    step = SCROLL_MAX_STEP if step > 0 else -SCROLL_MAX_STEP
                self._scroll_pos = current + step
                self._place_content()
                keep_going = True
                continue
            canvas = getattr(area, "_parent_canvas", None)
            if canvas is None:
                self._scroll_targets.pop(area, None)
                continue
            content, maximum = self._scroll_metrics(area)
            if content <= 0:
                self._scroll_targets.pop(area, None)
                continue
            target = max(0.0, min(maximum, target))
            current = self._scroll_offset(area, content)
            remaining = target - current
            if abs(remaining) < 0.5:
                self._scroll_targets.pop(area, None)
                continue
            if abs(remaining) <= SCROLL_SNAP_PX:
                self._scroll_targets.pop(area, None)
                canvas.yview_moveto(max(0.0, min(1.0, target / content)))
                try:
                    canvas.update_idletasks()
                except Exception:
                    pass
                continue
            # Ease out: big gaps move fast, the last pixels land on the snap.
            step = remaining * SCROLL_EASE
            if abs(step) > SCROLL_MAX_STEP:
                step = SCROLL_MAX_STEP if step > 0 else -SCROLL_MAX_STEP
            canvas.yview_moveto(max(0.0, min(1.0, (current + step) / content)))
            try:
                # Tk repaints the moved canvas and its child windows on idle;
                # pumping it here is the paint step for this frame.
                canvas.update_idletasks()
            except Exception:
                pass
            keep_going = True
        if keep_going:
            self._scroll_job = self.after(SCROLL_FRAME_MS, self._scroll_tick)

    def _repaint_area(self, area):
        """Paints a frame right away, used when the offset jumps."""
        try:
            self.update_idletasks()
        except Exception:
            return
        if self._redraw is None:
            return
        try:
            # RDW_INVALIDATE | RDW_ALLCHILDREN | RDW_UPDATENOW
            self._redraw(area.winfo_id(), None, None, 0x0001 | 0x0080 | 0x0100)
        except Exception:
            self._redraw = None

    def _scroll_to_preview(self):
        """Scrolls the workspace so the settings and preview panel is in view."""
        try:
            self.update_idletasks()
            content, maximum = self._scroll_metrics(self.workspace)
            if content <= 0:
                return
            absolute = self.action.winfo_rooty() - self.workspace.winfo_rooty()
            self._scroll_targets.pop(self.workspace, None)
            self._scroll_pos = max(0.0, min(maximum, absolute - 8))
            self._place_content()
            self._repaint_area(self.workspace)
        except Exception:
            pass

    def _reset_preview(self, message):
        self._cancel_preview_deadline()
        self._preview_token += 1
        self._preview_raw_img = None
        self._preview_ratio_img = None
        self._preview_source_key = None
        self.preview_img.configure(image="", text=message)
        self.current_preview_img = None
        self.preview_ratio_label.configure(text="")

    def _arm_preview_deadline(self, token, url):
        """Guarantees the panel leaves "Loading preview..." eventually."""
        self._cancel_preview_deadline()
        self._preview_job = self.after(
            PREVIEW_DEADLINE_MS, lambda: self._preview_deadline_hit(token, url))

    def _cancel_preview_deadline(self):
        job, self._preview_job = self._preview_job, None
        if job is not None:
            try:
                self.after_cancel(job)
            except Exception:
                pass

    def _preview_deadline_hit(self, token, url):
        self._preview_job = None
        if token != self._preview_token:
            return
        if self.video_url.get().strip() != url:
            return
        if self._preview_raw_img is not None:
            return
        self._reset_preview("Preview unavailable")

    def _start_url_preview(self, url, token, thumb_url=""):
        threading.Thread(target=self._load_url_preview, args=(url, token, thumb_url),
                         daemon=True).start()

    def _recover_link_preview(self, url):
        """Tries image-only thumbnails even after the metadata read failed."""
        if self.video_url.get().strip() != url:
            return
        if self._preview_raw_img is not None or self._preview_source_key == ("url", url):
            return
        self._preview_token += 1
        token = self._preview_token
        self._arm_preview_deadline(token, url)
        self._start_url_preview(url, token)

    def _load_url_preview(self, url, token, thumb_url=""):
        try:
            thumb_bytes = self._fetch_thumbnail(url, thumb_url)
            if not thumb_bytes:
                self.events.put(("preview_error", (token, url, "No preview for this link")))
                return
            with Image.open(io.BytesIO(thumb_bytes)) as raw:
                img = raw.convert("RGB")
            self.events.put(("preview", (token, url, img)))
        except Exception as exc:
            self.events.put(("preview_error", (token, url, "Preview failed")))
            self.events.put(("log", f"URL preview error: {exc}"))

    def _choose_video(self):
        path = filedialog.askopenfilename(title="Choose a long-form video", filetypes=[
            ("Video files", "*.mp4 *.mov *.mkv *.avi *.webm"), ("All files", "*.*")])
        if path:
            self.video_path.set(path)
            size_mb = os.path.getsize(path) / (1024 * 1024)
            self.video_url.set(f"Local file: {os.path.basename(path)} ({size_mb:.1f} MB)")
            self.source_entry.configure(text_color=TEXT)
            self._update_preview()

    def _use_link(self):
        url = self.video_url.get().strip()
        if not url.startswith(("https://", "http://")):
            self._show_popup("A public link is needed", "Paste a complete public video link beginning with https://", icon="!", accent="#F4C56A")
            return
        self.video_path.set("")
        self.source_entry.configure(text_color=TEXT)
        self._write_log("Video link selected. It will download when you generate clips.")
        self._update_preview()

    def _write_log(self, message, color=None):
        """Shows the current stage; the bar and the % show overall progress."""
        text = " ".join(str(message).split())
        if not text:
            return
        self._advance_progress(text)
        text = self._stage_text(text)
        if len(text) > 120:
            text = text[:117].rstrip() + "..."
        self.progress_stage.configure(text=text, fg=color or "#A0A0B4")
        if self._job_running:
            # The sidebar card mirrors the job, so it updates with every line.
            self._set_rail(text, color=color or PURPLE)

    def _advance_progress(self, message):
        match = re.search(r"rendering (\d+)\s*/\s*(\d+)", message, re.IGNORECASE)
        if match:
            self._clip_index = max(1, int(match.group(1)))
            self._clip_total = max(1, int(match.group(2)))
        percent, base, span = job_progress(message, self._clip_base, self._clip_span)
        self._clip_base, self._clip_span = base, span
        if percent is not None:
            self._set_progress(percent)

    def _stage_text(self, message):
        """Rewrites technical pipeline lines as plain stage names."""
        low = message.lower()
        if "downloading source video" in low:
            return "Downloading the source video"
        if "reframed clip" in low and "%" in low:
            return f"Rendering clip {self._clip_index} of {self._clip_total}"
        if "adding captions" in low and "%" in low:
            return f"Adding captions to clip {self._clip_index} of {self._clip_total}"
        if low.startswith("rendering ") and "/" in low:
            return f"Rendering clip {self._clip_index} of {self._clip_total}"
        return message

    def _set_progress(self, percent):
        """Moves the bar forward only, so noise can never make it jump back."""
        percent = max(0.0, min(100.0, float(percent)))
        if percent < self._progress_value:
            return
        self._progress_value = percent
        self.progress.set(percent / 100)
        self.progress_percent.configure(text=f"{int(round(percent))}%")
        self._set_rail(percent=percent)

    def _set_rail(self, text=None, color=None, percent=None):
        """Keeps the sidebar Auto Clipper card in step with the running job."""
        if text is not None:
            label = " ".join(str(text).split())
            if len(label) > 28:
                label = label[:25].rstrip() + "..."
            self.rail_state.configure(text=label, fg=color or "#B6B6C8")
            self.rail_dot.configure(fg=color or GREEN)
        if percent is not None:
            percent = max(0.0, min(100.0, float(percent)))
            self.rail_bar.set(percent / 100)
            self.rail_percent.configure(text=f"{int(round(percent))}%")

    def _reset_progress(self, message):
        self._progress_value = 0.0
        self._clip_base = 0.0
        self._clip_span = 0.0
        self._clip_index = 1
        self._clip_total = 1
        self.progress.set(0)
        self.progress_percent.configure(text="0%")
        self.progress_stage.configure(text=message, fg="#A0A0B4")
        self._set_rail(message, color=PURPLE, percent=0)

    def _report_callback_exception(self, exc_type, exc, tb):
        """Keeps a crashed UI callback from freezing the window silently.

        Packaged builds have no console, so tkinter's default handler wrote the
        traceback to a stream that does not exist: the click looked ignored and
        the button stayed on "Generating..." forever. Log it, put the window
        back in a usable state and tell the user what broke.
        """
        log_error("UI callback failed", exc)
        try:
            if self._job_running:
                self._job_running = False
                self.run_button.configure(state="normal", text="✦  Generate clips")
                self.status.configure(text="●  Needs attention", text_color="#FF9696",
                                      fg_color="#351923")
            self._write_log(f"ERROR: {exc}", color="#FF9696")
            self._set_rail("Needs attention", color="#FF9696")
            self._show_popup("Something went wrong", str(exc), icon="!", accent="#FF9696")
        except Exception:
            pass

    def _report_thread_exception(self, args):
        """Worker threads that die must leave a traceback in the log too."""
        log_error(f"Thread {getattr(args.thread, 'name', '?')} failed", args.exc_value)

    def _start(self):
        entered_source = self.video_url.get().strip()
        source_url = entered_source if entered_source.startswith(("https://", "http://")) else ""
        video = "" if source_url else self.video_path.get().strip()
        output = self.output_path.get()
        if not os.path.isfile(video) and not source_url.startswith(("https://", "http://")):
            self._show_popup("Video needed", "Choose a valid video file or paste a public video link first.", icon="!", accent="#F4C56A")
            return
        self.run_button.configure(state="disabled", text="Generating...")
        self._job_running = True
        self.status.configure(text="●  Processing", text_color="#F4C56A", fg_color="#352A12")
        self._result_clips = []
        self._render_results()
        self._reset_progress("Preparing the job...")
        log_line(f"Job started: video={video!r} url={source_url!r} output={output!r} "
                 f"score={self.score_value.get()} length={self.duration_value.get()}s "
                 f"ratio={self.aspect_ratio.get()} captions={bool(self.captions.get())} "
                 f"caption_size={self.caption_size.get()}")
        threading.Thread(target=self._run_job,
                         args=(video, source_url, output, self.score_value.get(), self.duration_value.get(),
                               self.aspect_ratio.get(), self.captions.get(),
                               self.caption_size.get()),
                         daemon=True).start()

    def _run_job(self, video, source_url, output, score, duration, aspect_ratio, captions, caption_size):
        try:
            report = lambda message: self.events.put(("log", message))
            on_clip = lambda clip: self.events.put(("clip", clip))
            if source_url:
                with tempfile.TemporaryDirectory(prefix="viral_clipper_source_") as source_folder:
                    source_video = download_public_video(source_url, source_folder, status_callback=report)
                    clips = auto_generate_viral_clips(source_video, min_score=score, output_folder=output,
                                                       captions=captions, fallback_duration=duration,
                                                       aspect_ratio=aspect_ratio,
                                                       caption_size=caption_size,
                                                       on_clip=on_clip, status_callback=report)
            else:
                clips = auto_generate_viral_clips(video, min_score=score, output_folder=output,
                                                   captions=captions, fallback_duration=duration,
                                                   aspect_ratio=aspect_ratio,
                                                   caption_size=caption_size,
                                                   on_clip=on_clip, status_callback=report)
            self.events.put(("done", clips))
        except Exception as exc:
            log_error("Generate clips failed", exc)
            self.events.put(("error", str(exc)))

    def _read_events(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                try:
                    self._handle_event(kind, value)
                except Exception as exc:
                    # One bad event must never stop the pump; otherwise every
                    # later preview, clip and update would silently never show.
                    try:
                        self._write_log(f"Interface error: {exc}")
                    except Exception:
                        pass
        except queue.Empty:
            pass
        self.after(100, self._read_events)

    def _handle_event(self, kind, value):
        if kind == "log":
            self._write_log(value)
            log_line(value)
        elif kind == "show_window":
            self._bring_to_front()
        elif kind == "clip":
            self._append_result(value)
        elif kind == "done":
            self._job_running = False
            self.run_button.configure(state="normal", text="✦  Generate clips")
            if not value:
                # Nothing was exported: say why instead of a green "Clips ready"
                # popup that claims success and creates no file.
                self.status.configure(text="●  No clips this time", text_color="#F4C56A",
                                      fg_color="#352A12")
                self._set_progress(100)
                self._write_log("No clips this time - nothing scored above the Minimum score.",
                                color="#F4C56A")
                self._set_rail("No clips this time", color="#F4C56A", percent=100)
                self._show_results([])
                self._show_popup(
                    "No clips this time",
                    "Super finished but did not find a moment above your Minimum score, "
                    "so nothing was saved.\n\nLower Minimum score and generate again.",
                    icon="!", accent="#F4C56A")
                return
            self.status.configure(text="●  Complete", text_color=GREEN, fg_color="#102820")
            self._set_progress(100)
            self._write_log(f"Finished - created {len(value)} clip(s).", color=GREEN)
            self._set_rail(f"Complete - {len(value)} clip(s) ready", color=GREEN, percent=100)
            self._show_results(value)
            self._show_popup("Clips ready", f"Created {len(value)} clip(s).\n\nSaved to:\n{self.output_path.get()}",
                             icon="✓", accent=GREEN, secondary_text="Open folder",
                             on_secondary=self._open_output_folder)
        elif kind == "preview":
            token, url, image = value
            if token != self._preview_token or self.video_url.get().strip() != url:
                return  # a newer link or a local file is showing now
            self._cancel_preview_deadline()
            self._preview_raw_img = image
            self._preview_source_key = ("url", url)
            self._preview_source_height = image.height or 1080
            self._render_preview_for_ratio()
            if self._preview_autoscroll:
                self.after(60, self._scroll_to_preview)
        elif kind == "preview_error":
            token, url, message = value
            if token != self._preview_token or self.video_url.get().strip() != url:
                return
            self._reset_preview(message)
        elif kind == "link_info":
            token, url, info = value
            if token == self._link_fetch_token:
                self._link_fetch_inflight = False
                self._link_cache[url] = info
                while len(self._link_cache) > 32:
                    self._link_cache.pop(next(iter(self._link_cache)))
                self._show_link_ready(info)
                if self.video_url.get().strip() == url:
                    self._update_preview(scroll=False)
        elif kind == "link_error":
            token, url, message = value
            if token == self._link_fetch_token:
                self._link_fetch_inflight = False
                self._show_link_error(message)
                self._recover_link_preview(url)
        elif kind == "error":
            self._job_running = False
            self.run_button.configure(state="normal", text="✦  Generate clips")
            self.status.configure(text="●  Needs attention", text_color="#FF9696", fg_color="#351923")
            self._write_log("ERROR: " + value, color="#FF9696")
            self._set_rail("Needs attention", color="#FF9696")
            self._show_popup("Could not generate clips", value, icon="!", accent="#FF9696")


if __name__ == "__main__":
    if not _acquire_single_instance():
        # Another Super is already open: raise it instead of starting a twin.
        if not single_instance.signal_running_instance():
            single_instance.focus_running_instance()
        sys.exit(0)
    ViralClipperApp().mainloop()
