"""Writes and validates release/update.json for the 1.0.1 build."""
import hashlib
import json
from pathlib import Path

import updater

VERSION = "1.0.1"
BASE = f"https://github.com/SuperPolarsh/Super/releases/download/v{VERSION}"
RELEASE = Path("release")


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


exe = RELEASE / "Super.exe"
setup = RELEASE / f"Super-Setup-{VERSION}.exe"
for path in (exe, setup, RELEASE / "SuperSetup.exe"):
    assert path.is_file(), f"missing {path}"

manifest = {
    "version": VERSION,
    "url": f"{BASE}/Super.exe",
    "sha256": sha256_of(exe),
    "notes": "Super now checks for updates in its own start-up window and then "
             "opens in about half the time. The interface has a cleaner, "
             "borderless look with its own dark title bar, the sidebar shows "
             "the running job live, scrolling glides instead of jumping, and "
             "the log box is replaced by a progress bar with stage, percent, "
             "speed and time left. Super only ever runs one window: launching "
             "it again brings the open window back.",
    "mandatory": False,
    "installer": f"{BASE}/Super-Setup-{VERSION}.exe",
    "installer_sha256": sha256_of(setup),
}
(RELEASE / "update.json").write_text(json.dumps(manifest, indent=2),
                                    encoding="utf-8-sig")

# Prove the app and bootstrapper can read what we just wrote.
info = updater.validate_manifest(
    json.loads((RELEASE / "update.json").read_text(encoding="utf-8-sig")))
print("version:", info.version)
print("url:", info.url)
print("sha256:", info.sha256)
print("installer:", info.installer_url)
print("installer sha256:", info.installer_sha256)
print("notes:", info.notes)
print("is newer than 1.0.0:", updater.is_newer(info.version, "1.0.0"))
print("needs update 1.0.1 -> 1.0.1:", updater.is_newer(info.version, "1.0.1"))
for path in (exe, setup, RELEASE / "SuperSetup.exe"):
    print(f"{path.name}: {path.stat().st_size:,} bytes sha256 {sha256_of(path)}")
