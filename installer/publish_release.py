"""Publishes the built files in ``release`` to a GitHub release.

Usage::

    $env:GH_TOKEN = "github_pat_..."     # Contents: read and write
    python installer\\publish_release.py --repo OWNER/REPO

The token needs **Contents: Read and write** on that repository. Classic
tokens with the ``repo`` scope also work. Nothing is published until every
asset has uploaded successfully, so a failure leaves a draft release behind
that you can retry or delete.

Steps:

1. commits a short ``README.md`` if the repository has none,
2. creates (or reuses) a draft release for the version in ``update.json``,
3. uploads ``SuperSetup.exe``, the installer, ``Super.exe`` and ``update.json``,
4. publishes the release and checks the published sizes and digests.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

try:
    import requests
except ImportError:  # pragma: no cover - requests ships with the app
    sys.exit("requests is required: python -m pip install requests")

RELEASE_DIR = Path(__file__).resolve().parent.parent / "release"

README = """# {app}

Turn long videos into short, captioned 9:16 and 16:9 clips on Windows.

## Download

**[Download {app} for Windows]({site}/releases/latest)**

Run `SuperSetup.exe`, pick a drive, and it installs for your user (no admin
prompt). Installed copies update themselves automatically.

This repository hosts the official builds — see the
[Releases]({site}/releases) page.
"""

ASSET_HELP = """
**New users:** download `SuperSetup.exe`. It is a small bootstrapper that picks
a drive, downloads the full build, and installs per-user (no admin prompt).

**Updating:** installed copies update themselves automatically from this page.

Installed copies read `update.json` from the latest release, so keep that asset
in every release.
"""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class GitHub:
    def __init__(self, repo: str, token: str):
        self.repo = repo
        self.token = token
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Super-release-publisher",
        }

    def call(self, path: str, method: str = "GET",
             body: Optional[dict] = None) -> dict:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            "https://api.github.com" + path, data=data, method=method,
            headers={**self.headers, "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read()
        return json.loads(raw.decode() or "{}") if raw else {}

    def commit_readme(self, site: str, app: str) -> None:
        try:
            self.call(f"/repos/{self.repo}/contents/README.md")
            print("README.md already exists, leaving it alone")
            return
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
        base = self.call(f"/repos/{self.repo}")["default_branch"]
        result = self.call(f"/repos/{self.repo}/contents/README.md", "PUT", {
            "message": "Add README with download link",
            "content": base64.b64encode(
                README.format(app=app, site=site).encode()).decode(),
            "branch": base,
        })
        print("committed README.md:", result["commit"]["sha"][:7])

    def release_for(self, tag: str, version: str, notes: str,
                    site: str) -> dict:
        try:
            release = self.call(f"/repos/{self.repo}/releases/tags/{tag}")
            print(f"reusing release {tag} (draft={release['draft']})")
            return release
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
        base = self.call(f"/repos/{self.repo}")["default_branch"]
        body = (notes.strip() + "\n\n" if notes.strip() else "") + ASSET_HELP
        release = self.call(f"/repos/{self.repo}/releases", "POST", {
            "tag_name": tag,
            "target_commitish": base,
            "name": f"{version}",
            "body": body,
            "draft": True,
            "prerelease": False,
        })
        print(f"created draft release {tag} (id {release['id']})")
        return release

    def upload(self, release_id: int, path: Path) -> None:
        started = time.time()
        size = path.stat().st_size / 1024 / 1024
        print(f"uploading {path.name} ({size:.1f} MB)", flush=True)
        with open(path, "rb") as handle:
            response = requests.post(
                f"https://uploads.github.com/repos/{self.repo}"
                f"/releases/{release_id}/assets",
                params={"name": path.name},
                headers={**self.headers,
                         "Content-Type": "application/octet-stream"},
                data=handle, timeout=(30, 1800))
        if response.status_code not in (200, 201):
            raise RuntimeError(f"{path.name}: HTTP {response.status_code} "
                               f"{response.text[:300]}")
        asset = response.json()
        print(f"  done in {time.time() - started:.0f}s "
              f"({asset['size']:,} bytes)")

    def publish(self, release_id: int) -> dict:
        release = self.call(f"/repos/{self.repo}/releases/{release_id}")
        if release["draft"]:
            print("publishing the release")
            release = self.call(f"/repos/{self.repo}/releases/{release_id}",
                                "PATCH", {"draft": False})
        return release

    def verify(self, local: dict) -> bool:
        release = self.call(f"/repos/{self.repo}/releases/latest")
        print("latest release:", release["tag_name"], "|", release["name"])
        published = {asset["name"]: asset for asset in release["assets"]}
        ok = True
        for name, (size, digest) in local.items():
            asset = published.get(name)
            if asset is None:
                print(f"  {name}: MISSING")
                ok = False
                continue
            size_ok = asset["size"] == size
            digest_ok = True
            if asset.get("digest"):
                digest_ok = asset["digest"].lower() == f"sha256:{digest}"
            print(f"  {name}: size {'ok' if size_ok else 'MISMATCH'}, "
                  f"digest {'ok' if digest_ok else 'MISMATCH'}")
            ok = ok and size_ok and digest_ok
        return ok


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--tag", default="", help="defaults to v<version>")
    parser.add_argument("--release-dir", default=str(RELEASE_DIR))
    parser.add_argument("--no-readme", action="store_true",
                        help="skip the README commit")
    args = parser.parse_args(argv)

    token = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
             or "").strip()
    if not token:
        return _fail("Set GH_TOKEN to a token with Contents: read and write.")

    release_dir = Path(args.release_dir)
    manifest_path = release_dir / "update.json"
    if not manifest_path.is_file():
        return _fail(f"{manifest_path} is missing; run build_release.ps1 first.")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    version = str(manifest["version"])
    tag = args.tag or f"v{version}"
    setup_name = Path(manifest.get("installer", "")).name or \
        f"Super-Setup-{version}.exe"
    expected = ["SuperSetup.exe", setup_name, "Super.exe", "update.json"]

    files = [release_dir / name for name in expected]
    missing = [str(path) for path in files if not path.is_file()]
    if missing:
        return _fail("missing files: " + ", ".join(missing))

    local = {path.name: (path.stat().st_size, sha256_of(path)) for path in files}
    for name, (size, digest) in local.items():
        print(f"{name}: {size:,} bytes, sha256 {digest}")

    github = GitHub(args.repo, token)
    site = f"https://github.com/{args.repo}"

    if not args.no_readme:
        github.commit_readme(site, "Super")

    release = github.release_for(tag, version, str(manifest.get("notes", "")),
                                 site)
    existing = {asset["name"] for asset in release.get("assets", [])}
    for path in files:
        if path.name in existing:
            print(f"skipping {path.name}: already uploaded")
            continue
        github.upload(release["id"], path)

    release = github.publish(release["id"])
    print("published:", release["html_url"])

    print("\nchecking public URLs")
    for name in ("update.json", "SuperSetup.exe"):
        url = f"{site}/releases/latest/download/{name}"
        response = requests.head(url, allow_redirects=True, timeout=60)
        print(f"  {name}: HTTP {response.status_code}, "
              f"{response.headers.get('content-length', '?')} bytes")

    return 0 if github.verify(local) else 1


def _fail(message: str) -> int:
    print(message)
    return 1


if __name__ == "__main__":
    sys.exit(main())
