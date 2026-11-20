"""Update checks and in-place self-replacement for the packaged app.

The update manifest is a small JSON file hosted over HTTPS:

    {
      "version": "1.1.0",
      "url": "https://example.com/downloads/Super.exe",
      "sha256": "<64 hex chars>",
      "notes": "What changed",
      "mandatory": false
    }

``Super.exe`` is replaced once the current process exits, so updates work
without an installer and without admin rights (per-user installs only).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

CREATE_NO_WINDOW = 0x08000000
CHUNK_SIZE = 256 * 1024
MAX_MANIFEST_BYTES = 256 * 1024


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    url: str
    sha256: str = ""
    notes: str = ""
    mandatory: bool = False
    installer_url: str = ""
    installer_sha256: str = ""


def _require_https(url: str, label: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"{label} must be a public https:// link.")


def parse_version(text: str) -> tuple:
    """Turns 'v1.2.3-beta' into (1, 2, 3)."""
    match = re.search(r"(\d+(?:\.\d+)*)", str(text or ""))
    if not match:
        return (0,)
    return tuple(int(part) for part in match.group(1).split("."))


def is_newer(latest: str, current: str) -> bool:
    a, b = parse_version(latest), parse_version(current)
    length = max(len(a), len(b))
    a += (0,) * (length - len(a))
    b += (0,) * (length - len(b))
    return a > b


def validate_manifest(data) -> UpdateInfo:
    if not isinstance(data, dict):
        raise ValueError("Update manifest must be a JSON object.")
    version = str(data.get("version", "")).strip()
    url = str(data.get("url", "")).strip()
    if not parse_version(version):
        raise ValueError("Update manifest is missing a valid version.")
    _require_https(url, "Update URL")
    checksum = str(data.get("sha256", "")).strip().lower()
    if checksum and not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise ValueError("Update checksum must be a sha256 hex digest.")

    installer = str(data.get("installer", "")).strip()
    installer_checksum = str(data.get("installer_sha256", "")).strip().lower()
    if installer:
        _require_https(installer, "Installer URL")
    if installer_checksum and not re.fullmatch(r"[0-9a-f]{64}", installer_checksum):
        raise ValueError("Installer checksum must be a sha256 hex digest.")

    return UpdateInfo(
        version=version,
        url=url,
        sha256=checksum,
        notes=str(data.get("notes", "")).strip(),
        mandatory=bool(data.get("mandatory", False)),
        installer_url=installer,
        installer_sha256=installer_checksum,
    )


def fetch_manifest(manifest_url: str, timeout: float = 10) -> dict:
    """Downloads and parses the manifest, bypassing stale intermediary caches."""
    parts = urllib.parse.urlsplit(manifest_url)
    query = dict(urllib.parse.parse_qsl(parts.query))
    query["t"] = str(int(__import__("time").time()))
    url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))
    request = urllib.request.Request(url, headers={
        "User-Agent": "Super-Updater",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(MAX_MANIFEST_BYTES)
    return json.loads(raw.decode("utf-8-sig"))


def check_for_update(current_version: str, manifest_url: str,
                     timeout: float = 10) -> Optional[UpdateInfo]:
    if not manifest_url:
        return None
    info = validate_manifest(fetch_manifest(manifest_url, timeout=timeout))
    return info if is_newer(info.version, current_version) else None


def sha256_of(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()


def update_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return Path(base) / "Super" / "updates"


def download_file(url: str, target, sha256: str = "",
                  progress: Optional[Callable[[int, int], None]] = None,
                  timeout: float = 60) -> Path:
    """Downloads ``url`` to ``target``, reusing and verifying cached files.

    ``sha256`` is the expected digest; when it is provided a mismatching
    download is deleted and rejected. ``progress`` receives (done, total)
    where ``total`` is 0 if the server does not send Content-Length.
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    expected = (sha256 or "").strip().lower()
    partial = target.with_suffix(target.suffix + ".part")

    if target.is_file() and expected and sha256_of(target) == expected:
        return target

    request = urllib.request.Request(url, headers={"User-Agent": "Super-Updater"})
    digest = hashlib.sha256()
    done = 0
    with urllib.request.urlopen(request, timeout=timeout) as response, open(partial, "wb") as handle:
        total = int(response.headers.get("Content-Length") or 0)
        while True:
            block = response.read(CHUNK_SIZE)
            if not block:
                break
            handle.write(block)
            digest.update(block)
            done += len(block)
            if progress:
                progress(done, total)

    if expected and digest.hexdigest() != expected:
        partial.unlink(missing_ok=True)
        raise ValueError("The downloaded file did not match its checksum.")

    partial.replace(target)
    return target


def download_update(info: UpdateInfo, destination=None,
                    progress: Optional[Callable[[int, int], None]] = None,
                    timeout: float = 60) -> Path:
    """Downloads the new exe, verifies its checksum and returns its path."""
    folder = Path(destination) if destination else update_dir()
    safe_version = re.sub(r"[^0-9A-Za-z._-]", "_", info.version) or "latest"
    target = download_file(info.url, folder / f"Super-{safe_version}.exe",
                           info.sha256, progress=progress, timeout=timeout)
    # Keep only the newest cached build.
    for stale in folder.glob("Super-*.exe"):
        if stale != target:
            try:
                stale.unlink()
            except OSError:
                pass
    return target


def can_self_update(target_exe=None) -> tuple:
    """Checks that the running app is a frozen exe in a writable folder."""
    if not getattr(sys, "frozen", False):
        return False, "Automatic updates only run in the installed Super app."
    target = Path(target_exe or sys.executable)
    if not target.is_file():
        return False, "Super could not find its own executable."
    probe = target.parent / f".super-write-test-{os.getpid()}"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError:
        return False, ("Super is installed in a folder this user cannot write to. "
                       "Reinstall it for the current user to enable updates.")
    return True, ""


REPLACEMENT_SCRIPT = """@echo off
setlocal
set "APPPID=%~1"
set "NEWEXE=%~2"
set "TARGET=%~3"
set /a waited=0
:wait
tasklist /FI "PID eq %APPPID%" /NH 2>NUL | findstr /C:"%APPPID%" >NUL
if errorlevel 1 goto copy
set /a waited+=1
if %waited% geq 200 goto copy
ping -n 2 127.0.0.1 >NUL
goto wait
:copy
set /a tries=0
:retry
copy /Y "%NEWEXE%" "%TARGET%" >NUL 2>&1
if not errorlevel 1 goto launch
set /a tries+=1
if %tries% geq 60 goto launch
ping -n 2 127.0.0.1 >NUL
goto retry
:launch
if not "%~4"=="" for %%I in ("%TARGET%") do > "%%~dpIversion.txt" echo %~4
start "" "%TARGET%"
del "%~f0" >NUL 2>&1
"""


def write_update_script(app_pid, new_exe, target_exe, version="") -> Path:
    """Writes the helper that swaps the exe after this process exits."""
    script = Path(tempfile.gettempdir()) / f"super-update-{int(app_pid)}.cmd"
    script.write_text(REPLACEMENT_SCRIPT, encoding="utf-8")
    return script


def apply_update(new_exe, app_pid=None, target_exe=None, version="") -> Path:
    """Spawns a hidden helper that swaps in the new exe after this process exits."""
    new_exe = Path(new_exe)
    target = Path(target_exe or sys.executable)
    if not new_exe.is_file():
        raise FileNotFoundError(f"Update file is missing: {new_exe}")
    pid = int(app_pid or os.getpid())
    version = re.sub(r"[^0-9A-Za-z._-]", "", str(version or ""))
    script = write_update_script(pid, new_exe, target, version)

    subprocess.Popen(
        ["cmd", "/c", str(script), str(pid), str(new_exe), str(target), str(version)],
        creationflags=CREATE_NO_WINDOW,
        close_fds=True,
    )
    return script
