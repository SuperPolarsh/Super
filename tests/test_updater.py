import functools
import hashlib
import http.server
import os
import socketserver
import subprocess
import threading

import pytest

import updater


def test_parse_version_handles_prefixes_and_suffixes():
    assert updater.parse_version("1.2.3") == (1, 2, 3)
    assert updater.parse_version("v2.0") == (2, 0)
    assert updater.parse_version("release-1.4.10-beta") == (1, 4, 10)
    assert updater.parse_version("") == (0,)


def test_is_newer_compares_numerically():
    assert updater.is_newer("1.10.0", "1.9.9")
    assert updater.is_newer("2.0", "1.99.99")
    assert not updater.is_newer("1.0.0", "1.0.0")
    assert not updater.is_newer("v1.0", "1.0.0")
    assert not updater.is_newer("0.9.9", "1.0")


def test_validate_manifest_accepts_complete_manifest():
    info = updater.validate_manifest({
        "version": "1.2.0",
        "url": "https://example.com/Super.exe",
        "sha256": "a" * 64,
        "notes": "Fixes",
        "mandatory": True,
        "installer": "https://example.com/Super-Setup-1.2.0.exe",
        "installer_sha256": "b" * 64,
    })
    assert info.version == "1.2.0"
    assert info.sha256 == "a" * 64
    assert info.mandatory is True
    assert info.installer_url.endswith("Super-Setup-1.2.0.exe")
    assert info.installer_sha256 == "b" * 64


def test_validate_manifest_allows_manifests_without_a_bootstrapper_payload():
    info = updater.validate_manifest({
        "version": "1.2.0",
        "url": "https://example.com/Super.exe",
    })
    assert info.installer_url == ""
    assert info.installer_sha256 == ""


@pytest.mark.parametrize("payload", [
    {},
    {"version": "1.0.0"},
    {"version": "1.0.0", "url": "http://example.com/Super.exe"},
    {"version": "1.0.0", "url": "file:///C:/Super.exe"},
    {"version": "1.0.0", "url": "https://example.com/Super.exe", "sha256": "not-a-hash"},
    {"version": "1.0.0", "url": "https://example.com/Super.exe",
     "installer": "http://example.com/Super-Setup.exe"},
    {"version": "1.0.0", "url": "https://example.com/Super.exe",
     "installer": "https://example.com/Super-Setup.exe",
     "installer_sha256": "not-a-hash"},
])
def test_validate_manifest_rejects_unsafe_or_incomplete_data(payload):
    with pytest.raises(ValueError):
        updater.validate_manifest(payload)


def test_sha256_of_matches_hashlib(tmp_path):
    file = tmp_path / "payload.bin"
    file.write_bytes(b"super update payload")
    assert updater.sha256_of(file) == hashlib.sha256(b"super update payload").hexdigest()


def test_can_self_update_is_false_outside_the_packaged_app():
    ok, reason = updater.can_self_update()
    assert ok is False
    assert reason


def test_check_for_update_is_disabled_without_a_manifest_url():
    assert updater.check_for_update("1.0.0", "") is None


def test_write_update_script_waits_then_replaces_and_restarts(tmp_path, monkeypatch):
    monkeypatch.setattr(updater.tempfile, "gettempdir", lambda: str(tmp_path))
    script = updater.write_update_script(4242, tmp_path / "new.exe",
                                         tmp_path / "Super.exe", "1.2.0")
    text = script.read_text(encoding="utf-8")
    assert "tasklist" in text
    assert "copy /Y" in text
    assert 'start "" "%TARGET%"' in text
    assert "version.txt" in text
    assert "%~4" in text
    assert script.name == "super-update-4242.cmd"


def test_download_file_verifies_checksum_and_reuses_the_cache(tmp_path):
    payload = b"super" * 40_000
    folder = tmp_path / "server"
    folder.mkdir()
    (folder / "payload.bin").write_bytes(payload)

    class Handler(http.server.SimpleHTTPRequestHandler):
        requests = 0

        def log_message(self, *args):
            pass

        def do_GET(self):
            type(self).requests += 1
            super().do_GET()

    handler = functools.partial(Handler, directory=str(folder))
    with socketserver.TCPServer(("127.0.0.1", 0), handler) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_address[1]}/payload.bin"
        digest = hashlib.sha256(payload).hexdigest()

        seen = []
        target = tmp_path / "Super-Setup.exe"
        path = updater.download_file(url, target, digest,
                                     progress=lambda done, total: seen.append((done, total)))
        assert path.read_bytes() == payload
        assert seen[-1][0] == len(payload)
        assert seen[-1][1] == len(payload)

        before = Handler.requests
        updater.download_file(url, target, digest)
        assert Handler.requests == before, "a valid cached download should not be fetched again"

        with pytest.raises(ValueError):
            updater.download_file(url, tmp_path / "bad.exe", "0" * 64)
        assert not (tmp_path / "bad.exe").exists()

        server.shutdown()


@pytest.mark.skipif(os.name != "nt", reason="The helper is a Windows batch file")
def test_update_helper_copies_the_exe_and_writes_the_version_marker(tmp_path):
    target = tmp_path / "Super.exe"
    new = tmp_path / "Super-new.exe"
    target.write_text("old-build", encoding="utf-8")
    new.write_text("new-build", encoding="utf-8")
    helper = tmp_path / "helper.cmd"
    helper.write_text(updater.REPLACEMENT_SCRIPT, encoding="utf-8")

    subprocess.run(
        ["cmd", "/c", str(helper), "4999999", str(new), str(target), "1.2.3"],
        capture_output=True, text=True, timeout=60)

    assert target.read_text(encoding="utf-8") == "new-build"
    assert (tmp_path / "version.txt").read_text(encoding="utf-8").strip() == "1.2.3"
