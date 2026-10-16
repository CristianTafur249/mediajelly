import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import mediajelly_db  # noqa: E402
import mediajelly_processor as processor_module  # noqa: E402
from mediajelly_processor import MediaJellyProcessor, ProcessingStats  # noqa: E402


class _Logger:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


@pytest.fixture
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "t.db"
    monkeypatch.setattr(mediajelly_db, "_get_db_path", lambda: db_path)
    return db_path


def _video(path: Path, size: int = 64 * 1024) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(os.urandom(size))
    old = time.time() - 600
    os.utime(path, (old, old))
    return path


def _processor(tmp_path) -> MediaJellyProcessor:
    proc = MediaJellyProcessor.__new__(MediaJellyProcessor)
    proc.logger = _Logger()
    proc.no_spanish_logger = _Logger()
    proc.tmp_dir = tmp_path
    proc.completed_file = tmp_path / "completed.txt"
    proc.failed_file = tmp_path / "failed-compression.txt"
    proc.stats = ProcessingStats()
    proc.processed_files = {}
    proc.config = None
    return proc


def _status(path: Path) -> str:
    rec = mediajelly_db.get_by_path(path)
    return rec.status if rec else ""


def _forbid(*args, **kwargs):
    raise AssertionError("no debía llamarse a ffmpeg")


def test_conforming_file_marked_done_without_ffmpeg(db, tmp_path, monkeypatch):
    video = _video(tmp_path / "Show S01E01.mp4")
    mediajelly_db.register(video)

    monkeypatch.setattr(processor_module.subprocess, "run", _forbid)
    proc = _processor(tmp_path)
    proc._already_conforms = lambda p: True
    proc._execute_compression_attempt = _forbid
    proc.set_resource_limits = _forbid

    result = proc.compress_single_file(str(video))

    assert result["success"] is True
    assert result["skipped"] is True
    assert _status(video) == "done"
    assert not proc.completed_file.exists()


def test_unregistered_conforming_file_is_registered_and_done(db, tmp_path, monkeypatch):
    video = _video(tmp_path / "Nuevo.mp4")
    proc = _processor(tmp_path)
    proc._already_conforms = lambda p: True
    proc._execute_compression_attempt = _forbid

    result = proc.compress_single_file(str(video))

    assert result["success"] is True
    assert _status(video) == "done"


def _conforms_proc(tmp_path, codec="h264", kbps=5000, langs=None):
    proc = _processor(tmp_path)
    proc._get_video_codec = lambda p: codec
    proc._get_video_bitrate = lambda p: None if kbps is None else {"bitrate_kbps": kbps}
    proc._get_video_resolution = lambda p: {"width": 1920, "height": 1080}
    calls = []

    def detect(p, allow_whisper_fallback=True):
        calls.append(allow_whisper_fallback)
        return True, [0], [], dict(langs if langs is not None else {0: "jpn"})

    proc.detect_language_streams = detect
    proc._detect_calls = calls
    return proc


def test_already_conforms_true_for_efficient_mp4(tmp_path):
    proc = _conforms_proc(tmp_path, codec="hevc", kbps=8999, langs={0: "jpn", 1: "eng"})
    proc._has_processed_marker = lambda p: True
    assert proc._already_conforms(tmp_path / "a.mp4") is True
    assert proc._detect_calls == [False]


def test_efficient_mp4_without_marker_does_not_conform(tmp_path):
    """Una descarga nueva .mp4 eficiente no lleva la marca: se procesa igual."""
    proc = _conforms_proc(tmp_path, codec="hevc", kbps=8999, langs={0: "jpn", 1: "eng"})
    proc._has_processed_marker = lambda p: False
    assert proc._already_conforms(tmp_path / "a.mp4") is False


@pytest.mark.parametrize(
    "name,codec,kbps,langs",
    [
        ("a.mkv", "h264", 5000, {0: "jpn"}),
        ("a.mp4", "mpeg4", 5000, {0: "jpn"}),
        ("a.mp4", "h264", 9000, {0: "jpn"}),
        ("a.mp4", "h264", None, {0: "jpn"}),
        ("a.mp4", "av1", 5000, {0: "jpn", 1: "und"}),
    ],
)
def test_already_conforms_false_cases(tmp_path, name, codec, kbps, langs):
    proc = _conforms_proc(tmp_path, codec=codec, kbps=kbps, langs=langs)
    assert proc._already_conforms(tmp_path / name) is False


def test_non_conforming_file_goes_to_processing(db, tmp_path):
    video = _video(tmp_path / "Show S01E02.mkv")
    rec, _ = mediajelly_db.register(video)
    proc = _processor(tmp_path)
    proc._already_conforms = lambda p: False
    seen = {}

    def stop(*a, **k):
        seen["status"] = _status(video)
        raise processor_module.CompressionError("alto")

    proc._get_video_resolution = stop

    result = proc.compress_single_file(str(video))

    assert seen["status"] == "processing"
    assert proc._original_fp == rec.fingerprint
    assert result["error"]


def test_postponed_file_returns_to_pending(db, tmp_path):
    video = _video(tmp_path / "Show S01E03.mkv")
    mediajelly_db.register(video)
    proc = _processor(tmp_path)
    proc._already_conforms = lambda p: False
    proc._get_video_resolution = lambda p: None
    proc._is_uhd_resolution = lambda r: False
    proc.set_resource_limits = lambda **k: None
    temp = tmp_path / "Show S01E03.compressed.mp4"
    proc._prepare_compression = lambda p, resolution=None: (
        True, {"use_gpu": False, "has_spanish": True}, temp, [0], [], {0: "jpn"}
    )
    proc._check_sufficient_resources = lambda p: (False, "poca memoria")
    proc._execute_compression_attempt = _forbid

    result = proc.compress_single_file(str(video))

    assert result["postponed"] is True
    assert _status(video) == "pending"


def test_failure_counts_attempt_and_quarantines(db, tmp_path):
    video = _video(tmp_path / "Show S01E04.mkv")
    mediajelly_db.register(video)
    proc = _processor(tmp_path)

    for _ in range(3):
        proc._update_stats_from_result({"error": "códec inválido"}, video, [], [])

    rec = mediajelly_db.get_by_path(video)
    assert rec.status == "failed"
    assert rec.attempts == 3
    assert proc.processed_files == {}


def test_resource_failure_not_counted(db, tmp_path):
    video = _video(tmp_path / "Show S01E05.mkv")
    mediajelly_db.register(video)
    proc = _processor(tmp_path)
    error = f"Falló compresión ({processor_module.RESOURCE_FAILURE_MARKERS[0]}): {video}"

    for _ in range(3):
        proc._update_stats_from_result({"error": error}, video, [], [])

    rec = mediajelly_db.get_by_path(video)
    assert rec.status == "pending"
    assert rec.attempts == 0


def test_failure_of_unregistered_file_registers_it(db, tmp_path):
    video = _video(tmp_path / "Show S01E06.mkv")
    proc = _processor(tmp_path)

    proc._update_stats_from_result({"error": "códec inválido"}, video, [], [])

    rec = mediajelly_db.get_by_path(video)
    assert rec.status == "pending"
    assert rec.attempts == 1


def test_success_result_does_not_touch_processed_files(db, tmp_path):
    video = _video(tmp_path / "Show S01E07.mp4")
    proc = _processor(tmp_path)

    proc._update_stats_from_result({"compressed": True}, video, [], [])

    assert proc.processed_files == {}
    assert proc.stats.files_compressed == 1


def test_processing_left_by_crash_is_recovered(db, tmp_path):
    video = _video(tmp_path / "Show S01E08.mkv")
    rec, _ = mediajelly_db.register(video)
    mediajelly_db.set_status(rec.fingerprint, "processing")

    proc = _processor(tmp_path)
    seen = {}

    def cleanup():
        seen.setdefault("status", _status(video))

    proc.cleanup_processed_files = cleanup
    proc.scan_media_files = lambda: []
    proc._check_all_pending_processed = lambda: None

    proc.run()

    assert seen["status"] == "pending"
    assert _status(video) == "pending"


def test_finalize_smaller_moves_state_to_mp4(db, tmp_path):
    original = _video(tmp_path / "Show S01E09.mkv", size=200 * 1024)
    rec, _ = mediajelly_db.register(original)
    mediajelly_db.set_status(rec.fingerprint, "processing")
    temp = tmp_path / "Show S01E09.compressed.mp4"
    temp.write_bytes(os.urandom(100 * 1024))

    proc = _processor(tmp_path)
    proc._original_fp = rec.fingerprint
    proc.detect_language_streams = lambda p, **k: (True, [0], [], {0: "jpn"})

    result = proc._finalize_compression_smaller(temp, original, False, 200 * 1024, 100 * 1024, 1.0)

    final = tmp_path / "Show S01E09.mp4"
    assert result["success"] is True
    assert not original.exists()
    new = mediajelly_db.get_by_path(final)
    assert new.status == "done"
    assert new.derived_from == rec.fingerprint
    assert mediajelly_db.get_by_path(original) is None
    assert not proc.completed_file.exists()


def test_finalize_larger_moves_state_to_mp4(db, tmp_path):
    original = _video(tmp_path / "Show S01E10.avi", size=100 * 1024)
    rec, _ = mediajelly_db.register(original)
    mediajelly_db.set_status(rec.fingerprint, "processing")
    temp = tmp_path / "Show S01E10.compressed.mp4"
    temp.write_bytes(os.urandom(150 * 1024))

    proc = _processor(tmp_path)
    proc._original_fp = rec.fingerprint
    proc.detect_language_streams = lambda p, **k: (True, [0], [], {0: "spa"})

    proc._finalize_compression_larger(temp, original, False, 100 * 1024, 150 * 1024)

    final = tmp_path / "Show S01E10.mp4"
    assert not original.exists()
    assert mediajelly_db.get_by_path(final).status == "done"
    assert len(mediajelly_db.list_by_status("pending", "processing", "done")) == 1


def test_und_audio_after_compression_marks_done_not_pending(db, tmp_path):
    original = _video(tmp_path / "Show S01E11.mkv", size=200 * 1024)
    rec, _ = mediajelly_db.register(original)
    mediajelly_db.set_status(rec.fingerprint, "processing")
    temp = tmp_path / "Show S01E11.compressed.mp4"
    temp.write_bytes(os.urandom(100 * 1024))

    proc = _processor(tmp_path)
    proc._original_fp = rec.fingerprint
    proc.detect_language_streams = lambda p, **k: (False, [], [], {0: "und"})

    proc._finalize_compression_smaller(temp, original, False, 200 * 1024, 100 * 1024, 1.0)

    final = tmp_path / "Show S01E11.mp4"
    assert _status(final) == "done"
    assert mediajelly_db.get_pending_files() == []
    assert str(final) in proc.stats.no_spanish


def test_uhd_never_conforms(tmp_path):
    proc = _conforms_proc(tmp_path, codec="hevc", kbps=8000, langs={0: "jpn"})
    proc._get_video_resolution = lambda p: {"width": 3840, "height": 2160}
    assert proc._already_conforms(tmp_path / "a.mp4") is False


def test_broken_process_pool_is_resource_failure(db, tmp_path):
    from concurrent.futures.process import BrokenProcessPool

    video = _video(tmp_path / "Show S01E12.mkv")
    mediajelly_db.register(video)
    proc = _processor(tmp_path)

    class _Future:
        def result(self):
            raise BrokenProcessPool("A process in the process pool was terminated abruptly")

    for _ in range(3):
        proc._process_single_future_result(_Future(), video, [], [])

    rec = mediajelly_db.get_by_path(video)
    assert rec.status == "pending"
    assert rec.attempts == 0


# ---- Revisión final ----

def test_already_conforms_false_without_audio_languages(tmp_path):
    """{} sale ante un error de ffprobe o un archivo sin audio: no basta para marcar done."""
    proc = _conforms_proc(tmp_path, codec="h264", kbps=5000, langs={})
    assert proc._already_conforms(tmp_path / "a.mp4") is False


def test_already_conforms_false_without_resolution(tmp_path):
    proc = _conforms_proc(tmp_path, codec="h264", kbps=5000, langs={0: "jpn"})
    proc._get_video_resolution = lambda p: None
    assert proc._already_conforms(tmp_path / "a.mp4") is False


def test_compression_rewrite_does_not_carry_languages(db, tmp_path):
    """Los índices de pista del original no valen para la salida recomprimida."""
    original = _video(tmp_path / "Show S01E12.mkv", size=200 * 1024)
    rec, _ = mediajelly_db.register(original)
    mediajelly_db.set_stream_language(rec.fingerprint, 2, "jpn", "whisper")
    mediajelly_db.set_status(rec.fingerprint, "processing")
    temp = tmp_path / "Show S01E12.compressed.mp4"
    temp.write_bytes(os.urandom(100 * 1024))

    proc = _processor(tmp_path)
    proc._original_fp = rec.fingerprint
    proc.detect_language_streams = lambda p, **k: (True, [0], [], {1: "jpn"})

    proc._finalize_compression_smaller(temp, original, False, 200 * 1024, 100 * 1024, 1.0)

    new = mediajelly_db.get_by_path(tmp_path / "Show S01E12.mp4")
    assert new.status == "done"
    assert mediajelly_db.get_stream_languages(new.fingerprint) == {}


def _corrupt_compressed(tmp_path, stem):
    temp = tmp_path / f"{stem}.compressed.mp4"
    temp.write_bytes(b"x" * 1024)  # < 10 MB: se considera corrupto sin ffprobe
    return temp


def test_clean_incomplete_uses_db_failed_state(db, tmp_path):
    original = _video(tmp_path / "Show S01E13.mkv")
    rec, _ = mediajelly_db.register(original)
    mediajelly_db.set_status(rec.fingerprint, "failed")
    temp = _corrupt_compressed(tmp_path, "Show S01E13")
    (tmp_path / "failed-compression.txt").write_text("")  # el archivo de texto ya no manda

    proc = _processor(tmp_path)
    proc.media_dir = tmp_path
    proc._is_file_in_excluded_folder = lambda f: False

    assert proc._clean_incomplete_compressed_files() == (1, 0)
    assert not temp.exists()
    assert _status(original) == "failed"


def test_clean_incomplete_ignores_failed_txt(db, tmp_path):
    original = _video(tmp_path / "Show S01E14.mkv")
    rec, _ = mediajelly_db.register(original)
    mediajelly_db.set_status(rec.fingerprint, "processing")
    _corrupt_compressed(tmp_path, "Show S01E14")
    (tmp_path / "failed-compression.txt").write_text(f"{original}\n")

    proc = _processor(tmp_path)
    proc.media_dir = tmp_path
    proc._is_file_in_excluded_folder = lambda f: False

    assert proc._clean_incomplete_compressed_files() == (1, 1)
    assert _status(original) == "pending"


def test_pending_mkv_with_done_mp4_sibling_stays_pending(db, tmp_path):
    """Sin la heurística del .mp4 hermano: un .mkv nuevo no se descarta por nombre."""
    mp4 = _video(tmp_path / "Show S01E15.mp4")
    mediajelly_db.mark_completed(mp4)
    mkv = _video(tmp_path / "Show S01E15.mkv")
    mediajelly_db.add_pending(mkv)

    proc = _processor(tmp_path)
    proc.cleanup_processed_files()

    assert _status(mkv) == "pending"
    assert _status(mp4) == "done"
