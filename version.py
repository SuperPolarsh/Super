"""Release metadata for the Super desktop app and bootstrapper.

Bump ``APP_VERSION`` before publishing a build, then run
``installer/build_release.ps1`` to produce the app, installer, bootstrapper
and update manifest.
"""
import os

APP_VERSION = "1.0.1"

# Public URL of the JSON manifest written by installer/build_release.ps1.
# Leave empty to disable update checks. The previous URL pointed at a repository
# that does not exist (every check returned HTTP 404), so checks are off until a
# real release server is published. Example:
#   https://github.com/<you>/<repo>/releases/latest/download/update.json
# Override at run time with the SUPER_UPDATE_URL environment variable.
UPDATE_MANIFEST_URL = ""

# Seconds to wait for the manifest / downloadable exe.
UPDATE_TIMEOUT = 10
