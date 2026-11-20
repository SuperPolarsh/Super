# Builds Super.exe, the Windows installer, the bootstrapper, and the update
# manifest.
#
#   .\installer\build_release.ps1 -Version 1.1.0 `
#       -AssetUrl "https://github.com/you/super/releases/download/v1.1.0/Super.exe" `
#       -Notes "Faster previews and caption fixes."
#
# Outputs to .\release:
#   Super.exe              - small launcher (checks updates, starts the app)
#   app\                   - the app folder the launcher starts
#   Super-Setup-<ver>.exe  - installer payload used by the bootstrapper
#   SuperSetup.exe         - small branded bootstrapper users download
#   update.json            - manifest the launcher and bootstrapper read

[CmdletBinding()]
param(
    [string]$Version,
    [string]$AssetUrl,
    [string]$InstallerUrl,
    [string]$ManifestUrl,
    [string]$Notes = "Bug fixes and performance improvements.",
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

function Invoke-PyInstaller([string]$Spec) {
    # PyInstaller writes progress to stderr. Windows PowerShell 5.1 turns a
    # native command's stderr into a terminating error under "Stop", so relax
    # the preference just for the build and rely on the artifact check below.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        python -m PyInstaller $Spec --noconfirm
    }
    finally {
        $ErrorActionPreference = $previous
    }
}

Push-Location $root
try {
    if (-not $Version) {
        $match = Select-String -Path (Join-Path $root "version.py") `
            -Pattern 'APP_VERSION\s*=\s*"([^"]+)"' | Select-Object -First 1
        if (-not $match) { throw "Could not read APP_VERSION from version.py" }
        $Version = $match.Matches[0].Groups[1].Value
    }
    if (-not $AssetUrl) {
        $AssetUrl = "https://REPLACE-ME.example.com/Super-$Version.exe"
    }
    if (-not $InstallerUrl) {
        if ($AssetUrl -match "^(.*/)[^/]+$") {
            $InstallerUrl = $Matches[1] + "Super-Setup-$Version.exe"
        }
        else {
            $InstallerUrl = "https://REPLACE-ME.example.com/Super-Setup-$Version.exe"
        }
    }

    if ($ManifestUrl) {
        Write-Host "Pointing the updater at $ManifestUrl"
        python -c "import pathlib,json,re,sys; p=pathlib.Path('version.py'); t=p.read_text(encoding='utf-8'); t=re.sub(r'UPDATE_MANIFEST_URL = .*', 'UPDATE_MANIFEST_URL = ' + json.dumps(sys.argv[1]), t); p.write_text(t, encoding='utf-8')" $ManifestUrl
        if ($LASTEXITCODE -ne 0) { throw "Could not update UPDATE_MANIFEST_URL in version.py" }
    }
    else {
        # version.py reads SUPER_UPDATE_URL at import time, so clear it first to
        # see what URL is actually baked into the build.
        $savedUrl = $env:SUPER_UPDATE_URL
        Remove-Item Env:\SUPER_UPDATE_URL -ErrorAction SilentlyContinue
        $baked = (& python -c "import version; print(version.UPDATE_MANIFEST_URL)") -join ""
        if ($savedUrl) { $env:SUPER_UPDATE_URL = $savedUrl }
        if (-not $baked.Trim()) {
            Write-Warning "UPDATE_MANIFEST_URL is empty."
            Write-Warning "This build will not auto-update and SuperSetup will show 'Updates aren't configured'."
            Write-Warning "Pass -ManifestUrl to bake in the hosted update.json URL, then rebuild."
        }
    }

    if (-not $SkipBuild) {
        Write-Host "Building the app folder..."
        Invoke-PyInstaller "ViralClipperAI.spec"
        # PyInstaller occasionally reports exit code 1 after a successful build,
        # so verify the artifact itself instead of trusting the code.
        if (-not (Test-Path (Join-Path $root "dist\SuperApp\SuperApp.exe"))) {
            throw "PyInstaller did not produce dist\SuperApp\SuperApp.exe"
        }
        Write-Host "Building the launcher..."
        Invoke-PyInstaller "SuperLauncher.spec"
        if (-not (Test-Path (Join-Path $root "dist\Super.exe"))) {
            throw "PyInstaller did not produce dist\Super.exe"
        }
    }

    $exe = Join-Path $root "dist\Super.exe"
    if (-not (Test-Path $exe)) { throw "dist\Super.exe is missing" }
    $appDir = Join-Path $root "dist\SuperApp"
    if (-not (Test-Path (Join-Path $appDir "SuperApp.exe"))) {
        throw "dist\SuperApp\SuperApp.exe is missing"
    }

    $release = Join-Path $root "release"
    New-Item -ItemType Directory -Force -Path $release | Out-Null
    Copy-Item $exe (Join-Path $release "Super.exe") -Force
    $releaseApp = Join-Path $release "app"
    if (Test-Path $releaseApp) { Remove-Item $releaseApp -Recurse -Force }
    Copy-Item $appDir $releaseApp -Recurse -Force

    $iscc = @(
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $iscc) {
        $command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
        if ($command) { $iscc = $command.Source }
    }
    $setup = Join-Path $release "Super-Setup-$Version.exe"
    if ($iscc) {
        Write-Host "Building installer..."
        & $iscc "/DAppVersion=$Version" (Join-Path $root "installer\Super.iss") | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
        if (-not (Test-Path $setup)) {
            throw "Inno Setup did not produce $setup"
        }
    }
    else {
        Write-Warning "Inno Setup 6 was not found; installer was skipped."
        Write-Warning "Install it with: winget install -e --id JRSoftware.InnoSetup"
    }

    if ($setup -and (Test-Path $setup)) {
        Write-Host "Building bootstrapper..."
        Invoke-PyInstaller "SuperBootstrapper.spec"
        $bootstrapper = Join-Path $root "dist\SuperSetup.exe"
        if (-not (Test-Path $bootstrapper)) {
            throw "PyInstaller did not produce dist\SuperSetup.exe"
        }
        Copy-Item $bootstrapper (Join-Path $release "SuperSetup.exe") -Force
    }

    $hash = (Get-FileHash $exe -Algorithm SHA256).Hash.ToLower()
    $manifest = [ordered]@{
        version   = $Version
        url       = $AssetUrl
        sha256    = $hash
        notes     = $Notes
        mandatory = $false
    }
    if ($setup -and (Test-Path $setup)) {
        $manifest["installer"] = $InstallerUrl
        $manifest["installer_sha256"] = (Get-FileHash $setup -Algorithm SHA256).Hash.ToLower()
    }
    $manifest | ConvertTo-Json | Set-Content (Join-Path $release "update.json") -Encoding utf8

    Write-Host ""
    Write-Host "Release $Version ready in $release" -ForegroundColor Green
    Write-Host "  SuperSetup.exe            (send this to new users / download page)"
    if ($setup -and (Test-Path $setup)) {
        Write-Host "  Super-Setup-$Version.exe  (installer payload; host alongside the manifest)"
        Write-Host "  app\                      (app folder packed into that installer)"
    }
    Write-Host "  Super.exe              (launcher; upload alongside the installer)"
    Write-Host "  update.json            (host this at UPDATE_MANIFEST_URL)"
    Write-Host ""
    Write-Host "Put the manifest URL in version.py, bump APP_VERSION for the next release,"
    Write-Host "then rebuild so the shipped app knows where to look."
}
finally {
    Pop-Location
}
