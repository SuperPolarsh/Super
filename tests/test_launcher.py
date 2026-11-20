from pathlib import Path

import launcher
import updater


def test_app_executable_lives_in_the_app_folder(tmp_path):
    assert launcher.app_executable(tmp_path) == tmp_path / "app" / "SuperApp.exe"


def test_installer_name_is_safe_for_a_file_name():
    assert launcher.installer_name("1.0.1") == "Super-Setup-1.0.1.exe"
    assert launcher.installer_name("1.0 beta/2") == "Super-Setup-1.0_beta_2.exe"


def test_a_newer_manifest_means_update():
    info = updater.UpdateInfo(version="1.0.2", url="https://example.com/Super.exe")
    assert launcher.needs_update(info, "1.0.1", app_present=True) is True


def test_the_same_version_is_not_an_update():
    info = updater.UpdateInfo(version="1.0.1", url="https://example.com/Super.exe")
    assert launcher.needs_update(info, "1.0.1", app_present=True) is False


def test_a_missing_app_folder_forces_the_full_install():
    # This is how a 1.0.x single-file install migrates to the folder layout.
    info = updater.UpdateInfo(version="1.0.1", url="https://example.com/Super.exe")
    assert launcher.needs_update(info, "1.0.1", app_present=False) is True


def test_no_manifest_means_no_update():
    assert launcher.needs_update(None, "1.0.1", app_present=True) is False


def test_install_helper_waits_then_installs_silently(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher.tempfile, "gettempdir", lambda: str(tmp_path))
    setup = tmp_path / "Super-Setup-1.0.2.exe"
    launcher_exe = tmp_path / "Super.exe"
    command = launcher.install_command(setup, launcher_exe, 4242)

    assert command[:2] == ["cmd", "/c"]
    assert command[3:] == ["4242", str(setup), str(launcher_exe)]
    script = Path(command[2]).read_text(encoding="utf-8")
    assert "/VERYSILENT" in script
    assert "/SUPPRESSMSGBOXES" in script
    assert "tasklist" in script  # waits for the launcher to exit first
    assert script.index("tasklist") < script.index("/VERYSILENT")


def test_short_errors_are_trimmed():
    assert launcher.short_error(ValueError("x" * 500)).endswith("...")
    assert launcher.short_error(ValueError("bad " "path")) == "bad path"
