import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import mediajelly_db  # noqa: E402
import mediajelly_processor as processor_module  # noqa: E402
from mediajelly_processor import MediaJellyProcessor  # noqa: E402


class _Logger:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def test_small_skip_sets_skipped_status(tmp_path, monkeypatch):
    """Un .mp4 < 50MB se omite y queda en skipped para salir de pendientes."""
    db_path = tmp_path / "t.db"
    monkeypatch.setattr(mediajelly_db, "_get_db_path", lambda: db_path)

    video = tmp_path / "Show S01E01 HDTV-1080p.mp4"
    video.write_bytes(b"\0" * 1024)
    old = time.time() - 600
    os.utime(video, (old, old))
    mediajelly_db.register(video)

    def _no_mark(*args, **kwargs):
        raise AssertionError("no debía llamarse mark_completed")

    monkeypatch.setattr(processor_module, "mark_completed", _no_mark, raising=False)

    proc = MediaJellyProcessor.__new__(MediaJellyProcessor)
    proc.logger = _Logger()
    proc.completed_file = tmp_path / "completed.txt"
    proc.failed_file = tmp_path / "failed-compression.txt"
    proc._already_conforms = lambda p: False
    proc._get_video_resolution = lambda p: None
    proc._is_uhd_resolution = lambda r: False
    proc.set_resource_limits = lambda **k: None

    result = proc.compress_single_file(str(video))

    assert result["success"] is True
    assert result["skipped"] is True
    assert mediajelly_db.get_by_path(video).status == "skipped"
    assert not proc.completed_file.exists()
