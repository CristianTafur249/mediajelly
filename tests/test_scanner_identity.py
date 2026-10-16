import logging
import os
import sys
import time
from pathlib import Path

import pytest

SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent / "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import mediajelly_db  # noqa: E402
from mediajelly_scanner import MediaScanner  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "t.db"
    monkeypatch.setattr(mediajelly_db, "_get_db_path", lambda: db_path)
    return db_path


@pytest.fixture
def scanner(db, tmp_path):
    s = MediaScanner.__new__(MediaScanner)
    s.logger = logging.getLogger("test_scanner_identity")
    s.tmp_dir = tmp_path / "tmp"
    s.tmp_dir.mkdir()
    return s


def _video(path: Path, size: int = 64 * 1024) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(os.urandom(size))
    old = time.time() - 600
    os.utime(path, (old, old))
    return path


def test_scan_rename_not_new(scanner, tmp_path):
    media = tmp_path / "media"
    video = _video(media / "Show S01E01.mp4")

    total, new = scanner.scan_media_directories([media])
    assert (total, new) == (1, 1)
    mediajelly_db.mark_completed(video)
    assert mediajelly_db.is_completed(video)

    renamed = video.rename(media / "Show - S01E01 - Titulo.mp4")
    total, new = scanner.scan_media_directories([media])
    assert (total, new) == (1, 0)
    assert mediajelly_db.is_completed(renamed)
    assert not mediajelly_db.get_pending_files()


def test_scan_marks_missing(scanner, tmp_path):
    media = tmp_path / "media"
    keep = _video(media / "a.mp4")
    gone = _video(media / "b.mp4")
    scanner.scan_media_directories([media])
    gone.unlink()

    scanner.scan_media_directories([media])
    assert mediajelly_db.get_by_path(gone).status == "missing"
    assert mediajelly_db.get_by_path(keep).status != "missing"


def test_subtitle_queue_written_to_db(scanner, tmp_path, monkeypatch):
    media = tmp_path / "media"
    a = _video(media / "a.mp4")
    b = _video(media / "b.mp4")
    scanner.scan_media_directories([media])
    monkeypatch.setattr(scanner, "_has_spanish_subtitle", lambda v: v.name == "b.mp4")

    assert scanner.scan_subtitle_directories([media]) == 1
    assert mediajelly_db.get_subtitle_queue() == [a]
    assert not (scanner.tmp_dir / "pending_subtitles.txt").exists()


def _two_dirs_done(scanner, tmp_path):
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    vid_a = _video(dir_a / "x.mp4")
    vid_b = _video(dir_b / "y.mp4")
    scanner.scan_media_directories([dir_a, dir_b])
    mediajelly_db.mark_completed(vid_a)
    mediajelly_db.mark_completed(vid_b)
    return dir_a, dir_b, vid_a, vid_b


def test_scan_unmounted_dir_does_not_mark_missing(scanner, tmp_path):
    dir_a, dir_b, vid_a, vid_b = _two_dirs_done(scanner, tmp_path)
    vid_b.unlink()
    dir_b.rmdir()

    scanner.scan_media_directories([dir_a, dir_b])
    assert mediajelly_db.get_by_path(vid_b).status == "done"


def test_scan_empty_dir_does_not_mark_missing(scanner, tmp_path):
    dir_a, dir_b, vid_a, vid_b = _two_dirs_done(scanner, tmp_path)
    vid_b.unlink()

    scanner.scan_media_directories([dir_a, dir_b])
    assert mediajelly_db.get_by_path(vid_b).status == "done"


def test_scan_marks_missing_only_under_scanned_root(scanner, tmp_path):
    dir_a, dir_b, vid_a, vid_b = _two_dirs_done(scanner, tmp_path)
    extra = _video(dir_a / "z.mp4")
    scanner.scan_media_directories([dir_a, dir_b])
    extra.unlink()
    vid_b.unlink()
    _video(dir_b / "otro.mp4")

    scanner.scan_media_directories([dir_a, dir_b])
    assert mediajelly_db.get_by_path(extra).status == "missing"
    assert mediajelly_db.get_by_path(vid_a).status == "done"
    assert mediajelly_db.get_by_path(vid_b).status == "missing"
