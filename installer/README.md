# Building and shipping Super

## How startup and updates work

Super is installed as two pieces:

```
%LOCALAPPDATA%\Programs\Super\
  Super.exe            small launcher: checks for updates, then starts the app
  app\SuperApp.exe     the actual app, unpacked once at install time
  app\_internal\...    Python, PyTorch, Whisper, OpenCV, ffmpeg, ...
```

`Super.exe` is the only thing users run. It shows the compact "Checking for
updates" window, then either "Super is up to date" → "Starting Super..." or
downloads and installs a newer build first. The heavy app lives in a folder so
start-up takes ~4-5 s instead of the ~12 s a 350 MB one-file build needs to
unpack itself on every launch.

A second double-click while Super is open raises the existing window: the app
holds a named mutex and a named show-event, and the launcher rings that event
and exits.

1. The launcher reads `UPDATE_MANIFEST_URL` from `version.py` on start.
2. The manifest is a tiny JSON file:

   ```json
   {
     "version": "1.1.0",
     "url": "https://example.com/downloads/Super.exe",
     "sha256": "…64 hex characters…",
     "installer": "https://example.com/downloads/Super-Setup-1.1.0.exe",
     "installer_sha256": "…64 hex characters…",
     "notes": "Faster previews and caption fixes.",
     "mandatory": false
   }
   ```

   `installer` is what the launcher downloads. `url`/`sha256` describe
   `Super.exe` itself and are kept for the release tooling and for 1.0.x
   builds that still have the old in-app updater.
3. If `version` is newer than `APP_VERSION`, the launcher downloads the
   installer, verifies `installer_sha256`, then a hidden `cmd` helper waits for
   the launcher to exit, runs the installer with `/VERYSILENT`, and starts
   `Super.exe` again.
4. If `app\SuperApp.exe` is missing (a 1.0.x install that only had `Super.exe`),
   the launcher treats that as needing a full install, which migrates the old
   layout automatically.
5. Downloads live in `%TEMP%\SuperUpdate` and are verified before use.

The launcher checks the network on every start; if the check fails (offline) it
starts the installed app anyway. No admin rights are needed because the
installer uses a per-user folder.

## The bootstrapper (`SuperSetup.exe`)

`SuperSetup.exe` is the small branded download users get. On a first run it opens
on a drive picker:

- every fixed disk is listed with its free space and a fullness bar,
- **Browse** picks any folder instead,
- **Installs to** shows the exact final path as you choose.

The system drive defaults to `%LOCALAPPDATA%\Programs\Super`, which needs no
admin rights and lets the launcher reinstall itself; other drives use
`<X>:\Super`.

It then reads the same manifest and:

1. compares `version` with the installed version in the registry,
2. opens the compact "Checking for updates" window,
3. downloads `installer` with live MB/s and time remaining, verifying
   `installer_sha256`,
4. runs Inno Setup silently into the chosen folder,
5. shows **Super &lt;version&gt; is installed** with **Open folder** and
   **Launch Super**.

If Super is already installed and current it opens straight on that same screen
with "You already have the latest version" instead of downloading 330 MB again.

`SuperSetup.exe` embeds `UPDATE_MANIFEST_URL` at build time. `build_release.ps1`
sets it from `-ManifestUrl`; the GitHub workflow fills it in automatically.
For testing a local build, override it at runtime:

```powershell
.\release\SuperSetup.exe --manifest "https://your-host/update.json"
# or
$env:SUPER_UPDATE_URL = "https://your-host/update.json"
.\release\SuperSetup.exe
```

`--install-dir "D:\Apps\Super"` installs somewhere else.

## One-time setup

1. Pick the public URL where `update.json` will live, for example:

   ```
   https://github.com/YOU/super/releases/latest/download/update.json
   ```

   The included GitHub workflow fills this in automatically. For manual builds,
   pass it as `-ManifestUrl` (see below) or set it in `version.py`.
2. Install Inno Setup 6 once on the build machine:

   ```powershell
   winget install -e --id JRSoftware.InnoSetup
   ```

## Release checklist

1. Bump `APP_VERSION` in `version.py` (for example `1.0.0` → `1.1.0`).
2. Build everything:

   ```powershell
   .\installer\build_release.ps1 `
     -AssetUrl "https://github.com/YOU/super/releases/download/v1.1.0/Super.exe" `
     -ManifestUrl "https://github.com/YOU/super/releases/latest/download/update.json" `
     -Notes "What changed in this build."
   ```

   This creates `release\Super.exe`, `release\app\`,
   `release\Super-Setup-<version>.exe`, `release\SuperSetup.exe`, and
   `release\update.json`. The installer URL is derived from `-AssetUrl`;
   override it with `-InstallerUrl` if the Setup lives somewhere else.
3. Upload **all four files** to the same place. The manifest references
   `Super.exe` and `Super-Setup-<version>.exe` by URL.
4. Give new users `SuperSetup.exe`. Existing users get the update from the
   launcher the next time they open Super.

### Publishing with GitHub (automated)

Push a tag that matches `APP_VERSION`:

```powershell
git tag v1.1.0
git push origin v1.1.0
```

`.github/workflows/release.yml` builds the exe, installer, bootstrapper and
manifest, publishes them as release assets, and points the app at:

```
https://github.com/<owner>/<repo>/releases/latest/download/update.json
```

### Publishing with one command

Drag-and-drop works, but a token lets the whole release publish itself:

```powershell
$env:GH_TOKEN = "github_pat_...";   # Contents: Read and write on the repo
python installer\publish_release.py --repo OWNER/REPO
```

It commits a short `README.md` if the repo has none, creates a **draft** release
for the version in `release\update.json`, uploads `SuperSetup.exe`, the
installer, `Super.exe` and `update.json`, publishes it, then checks the
published sizes and SHA-256 digests. Re-running is safe: assets that already
uploaded are skipped, so a failed run can just be repeated.

Create the token at <https://github.com/settings/personal-access-tokens/new>
with **Contents: Read and write**, limited to that one repository and a short
expiry, then revoke it once the release is live.

### No repo yet? First publish in four steps

You do not have to push source code or use the workflow. Browsers are enough.

1. On GitHub, create a **public** repo, for example `super`. Note the exact
   `owner/repo`, because URLs are case-sensitive in the build.
2. Bake the manifest URL into the build (this rewrites `version.py` and rebuilds
   everything, ~10 minutes):

   ```powershell
   .\installer\build_release.ps1 -Version 1.0.0 `
     -AssetUrl "https://github.com/OWNER/super/releases/download/v1.0.0/Super.exe" `
     -ManifestUrl "https://github.com/OWNER/super/releases/latest/download/update.json"
   ```

   Skipping `-ManifestUrl` produces a build that can never auto-update; the
   script warns you when that happens.
3. In the repo, open **Releases → Draft a new release**, set the tag to `v1.0.0`,
   and upload all four files from `release\`: `Super.exe`,
   `Super-Setup-1.0.0.exe`, `SuperSetup.exe`, `update.json`. Publish.
4. Send new users `SuperSetup.exe`. Done.

For every later release: bump `APP_VERSION`, rerun the same command with the new
version in both URLs, and upload the four new files as a new release. Installed
launchers notice the newer `update.json`, download the new installer, and
reinstall themselves.

The manifest URL is always:

```
https://github.com/<owner>/<repo>/releases/latest/download/update.json
```

`latest` means the newest published (non-prerelease) release, so keep releases in
order. The repo must be public: the updater cannot log in, so assets in a private
repo cannot be downloaded.

### Other hosts

Any static HTTPS host works (Cloudflare R2, S3, Backblaze B2, your own website).
Upload all four files, then point `UPDATE_MANIFEST_URL` at the manifest. There is
no special server software needed.

## Installer

`Super.iss` builds `Super-Setup-<version>.exe`, the payload the bootstrapper
downloads. It installs `Super.exe` and the `app\` folder to
`%LOCALAPPDATA%\Programs\Super` with no UAC prompt, adds Start Menu (and optional
desktop) shortcuts, and registers an uninstaller. That per-user location is what
allows the launcher to reinstall itself silently.

Two PyInstaller builds make the release:

```powershell
python -m PyInstaller ViralClipperAI.spec --noconfirm   # dist\SuperApp\ (onedir app)
python -m PyInstaller SuperLauncher.spec --noconfirm    # dist\Super.exe (small launcher)
```

`ViralClipperAI.spec` must stay onedir: a one-file build unpacks ~350 MB into
`%TEMP%` on every launch. `SuperLauncher.spec` stays tiny by excluding
torch/whisper/cv2/moviepy/numpy.

## Before inviting real users

- **Code signing matters.** An unsigned exe and unsigned updates will trigger
  SmartScreen warnings. Use a code-signing certificate (Azure Trusted Signing,
  SignPath, or a standard OV/EV certificate) and sign `Super.exe`, the Setup, and
  `SuperSetup.exe`.
- The updater checks HTTPS and a SHA-256 checksum, but neither proves who built
  the file. Signing is what proves that.
- If you later switch to an all-users install under `Program Files`, silent
  self-updates stop working without elevation. Keep the per-user install.
