import updater
from bootstrapper import (Drive, default_install_dir, drive_bar_color,
                          drive_detail, drive_label, folder_writable,
                          format_eta, format_gigabytes, format_megabytes,
                          format_speed, install_folder_for, installer_args,
                          progress_summary, shorten_path, usage_fraction)


def test_installer_args_run_inno_silently_into_the_chosen_folder(tmp_path):
    args = installer_args(tmp_path / "Super-Setup-1.2.0.exe", tmp_path / "Programs" / "Super")
    assert args[1:] == [
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/SP-",
        "/CLOSEAPPLICATIONS",
        f"/DIR={tmp_path / 'Programs' / 'Super'}",
    ]


def test_shorten_path_keeps_short_paths_untouched():
    path = r"C:\Users\Sam\AppData\Local\Programs\Super"
    assert shorten_path(path) == path


def test_shorten_path_collapses_the_middle_but_keeps_the_tail():
    path = r"C:\Users\Administrator\AppData\Local\Programs\Very Long Folder Name\Super"
    short = shorten_path(path, max_chars=40)
    assert len(short) <= 40
    assert short.startswith("C:\\")
    assert "…" in short
    assert short.endswith("Super")


def test_format_megabytes_rounds_down_to_whole_megabytes():
    assert format_megabytes(0) == "0 MB"
    assert format_megabytes(1024 * 1024 * 332) == "332 MB"


def test_default_install_dir_is_per_user(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert default_install_dir() == tmp_path / "Programs" / "Super"


def test_manifest_parses_installer_fields():
    info = updater.validate_manifest({
        "version": "1.2.0",
        "url": "https://example.com/Super.exe",
        "sha256": "a" * 64,
        "installer": "https://example.com/Super-Setup-1.2.0.exe",
        "installer_sha256": "b" * 64,
    })
    assert info.installer_url.endswith("Super-Setup-1.2.0.exe")
    assert info.installer_sha256 == "b" * 64


def test_format_gigabytes_keeps_two_decimals():
    assert format_gigabytes(0) == "0.00 GB"
    assert format_gigabytes(int(8.13 * 1024 ** 3)) == "8.13 GB"


def test_format_speed_uses_megabytes_per_second():
    assert format_speed(20.7 * 1024 * 1024) == "20.7 MB/s"
    assert format_speed(0) == "0.0 MB/s"


def test_format_eta_reads_naturally():
    assert format_eta(20) == "about 20 s left"
    assert format_eta(1) == "about 1 s left"
    assert format_eta(0) == "about 1 s left"
    assert format_eta(120) == "about 2 min left"
    assert format_eta(3600) == "about 1 h left"
    assert format_eta(3900) == "about 1 h 5 min left"


def test_progress_summary_matches_the_download_line():
    done, total = int(39.0 * 1024 * 1024), 446 * 1024 * 1024
    rate = 20.7 * 1024 * 1024
    text = progress_summary(done, total, rate)
    assert text.startswith("39.0 MB of 446 MB · 20.7 MB/s · about ")
    assert text.endswith("s left")


def test_progress_summary_without_a_content_length():
    assert progress_summary(5 * 1024 * 1024, 0, 0) == "5 MB downloaded"


def test_usage_fraction_and_bar_colour():
    assert usage_fraction(0, 100) == 1.0
    assert usage_fraction(50, 100) == 0.5
    assert usage_fraction(0, 0) == 0.0
    assert drive_bar_color(0.98) == "#F05A5A"
    assert drive_bar_color(0.66).startswith("#")


def test_drive_card_text():
    drive = Drive("D", int(161.81 * 1024 ** 3), int(476.92 * 1024 ** 3))
    assert drive_label("d") == "Drive D"
    assert drive_detail(drive) == "161.81 GB free of 476.92 GB"
    assert round(drive.used_fraction, 3) == round(1 - 161.81 / 476.92, 3)
    assert drive_detail(Drive("Z", 0, 0)) == "Free space unknown"


def test_install_folder_for_keeps_the_system_drive_per_user(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert install_folder_for("C", system_drive="C:") == tmp_path / "Programs" / "Super"


def test_install_folder_for_other_drives_is_a_top_level_folder():
    assert install_folder_for("d", system_drive="C:") == __import__("pathlib").Path("D:\\Super")


def test_install_folder_for_rejects_junk():
    assert install_folder_for("", system_drive="C:").name == "Super"


def test_folder_writable_accepts_a_user_folder(tmp_path):
    assert folder_writable(tmp_path / "Super")


def test_folder_writable_rejects_a_path_that_cannot_be_created(tmp_path):
    blocker = tmp_path / "file.txt"
    blocker.write_text("x", encoding="utf-8")
    assert not folder_writable(blocker / "Super")

