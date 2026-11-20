"""Runtime helpers for the packaged Windows app."""
import datetime
import os
import subprocess
import sys
import traceback

_applied = False


def resource_path(*parts):
    """Resolves a bundled asset both in source runs and inside the onefile exe."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, *parts)


def default_output_dir():
    """A writable folder for generated clips, outside the install directory.

    The old default was relative to the working directory. Installed copies
    run with the install folder as their working directory, so clips landed
    inside ``app\\viral_clips`` and were removed by the uninstaller.
    """
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    videos = os.path.join(home, "Videos")
    return os.path.join(videos if os.path.isdir(videos) else home, "Super Clips")


def app_log_path():
    """Where Super writes its crash log.

    The packaged app has no console, so ``sys.stderr`` is ``None`` and Python
    tracebacks vanish. Every unexpected failure is appended here instead, so a
    bug always leaves evidence the user can send back.
    """
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return os.path.join(base, "Super", "logs", "super.log")


def env_file_paths():
    """Places a ``.env`` may live, most specific first.

    A source run finds it in the project folder; an installed copy runs from
    ``%LOCALAPPDATA%\\Programs\\Super\\app``, so the keys must be looked up next
    to the exe (and in the per-user Super folder) as well.
    """
    bases = []
    if getattr(sys, "frozen", False):
        bases.append(os.path.dirname(os.path.abspath(sys.executable)))
    bases.append(os.path.dirname(os.path.abspath(__file__)))
    bases.append(os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Super"))
    seen = []
    for base in bases:
        candidate = os.path.join(base, ".env")
        if candidate not in seen:
            seen.append(candidate)
    return seen


def load_env_files():
    """Loads API keys from the first ``.env`` that exists. Never raises."""
    try:
        from dotenv import load_dotenv
    except Exception:
        return
    for candidate in env_file_paths():
        if os.path.isfile(candidate):
            load_dotenv(candidate, override=False)
            return


def log_line(text):
    """Appends one plain line to the app log. Never raises."""
    try:
        path = app_log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"[{datetime.datetime.now().isoformat(timespec='seconds')}] {text}\n")
    except Exception:
        pass


def log_error(context, exc=None):
    """Appends a timestamped traceback to the app log. Never raises."""
    try:
        path = app_log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"\n[{datetime.datetime.now().isoformat(timespec='seconds')}] {context}\n")
            if exc is not None:
                traceback.print_exception(type(exc), exc, exc.__traceback__, file=handle)
            else:
                traceback.print_exc(file=handle)
    except Exception:
        pass


def hide_child_console_windows():
    """Stops ffmpeg/yt-dlp children from flashing console windows.

    The app is built with ``console=False``, so every console child process
    (ffmpeg, yt-dlp's merge step, audio compression) would otherwise pop up a
    black terminal window. This injects CREATE_NO_WINDOW into every Popen call,
    including ones made internally by moviepy and yt-dlp.
    """
    global _applied
    if _applied or os.name != "nt":
        return
    _applied = True

    original_init = subprocess.Popen.__init__
    create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE

    def hidden_init(self, *args, **kwargs):
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | create_no_window
        kwargs.setdefault("startupinfo", startupinfo)
        original_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = hidden_init
