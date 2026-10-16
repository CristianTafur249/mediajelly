"""Guarda de "migración pendiente": con historial anterior sin migrar no se procesa nada."""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import mediajelly_db  # noqa: E402
import mediajelly_language_detector as detector  # noqa: E402
import mediajelly_migrate as migrate  # noqa: E402
from mediajelly_cron_runner import MediaJellyCron  # noqa: E402
from mediajelly_processor import MediaJellyProcessor, ProcessingStats  # noqa: E402

MENSAJE = "Migración pendiente: ejecute mediajelly_migrate.py --apply"


class _Logger:
    def __init__(self):
        self.errors = []
        self.handlers = []

    def error(self, msg, *args, **kwargs):
        self.errors.append(str(msg))

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


@pytest.fixture
def db(tmp_path, monkeypatch):
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    db_path = tmp_dir / "mediajelly.db"
    monkeypatch.setattr(mediajelly_db, "_get_db_path", lambda: db_path)
    return db_path


def _legacy_history(*paths) -> None:
    mediajelly_db.init_db()
    conn = sqlite3.connect(str(mediajelly_db._get_db_path()))
    with conn:
        conn.executemany(
            "INSERT INTO completed_files(path, label, completed_at) VALUES (?, NULL, datetime('now'))",
            [(str(p),) for p in paths],
        )
    conn.close()


def _video(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(path.name.encode() * 200)
    return path


# ---- Marcador de la migración ----

def test_apply_marks_legacy_migrated(db, tmp_path):
    media = tmp_path / "media"
    done = _video(media / "Serie" / "Serie - S01E01.mp4")
    _legacy_history(done)
    assert mediajelly_db.migration_pending() is True

    assert migrate.main(["--media-root", str(media), "--apply"]) == 0

    assert mediajelly_db.migration_pending() is False
    assert mediajelly_db.get_meta("legacy_migrated")


def test_dry_run_does_not_mark(db, tmp_path):
    media = tmp_path / "media"
    done = _video(media / "Serie" / "Serie - S01E01.mp4")
    _legacy_history(done)

    assert migrate.main(["--media-root", str(media)]) == 0

    assert mediajelly_db.migration_pending() is True


def test_apply_with_failed_rows_does_not_mark(db, tmp_path, monkeypatch):
    media = tmp_path / "media"
    done = _video(media / "Serie" / "Serie - S01E01.mp4")
    _legacy_history(done)

    def boom(*a, **k):
        raise RuntimeError("fallo simulado")

    monkeypatch.setattr(migrate, "_apply_row", boom)
    assert migrate.main(["--media-root", str(media), "--apply"]) == 1

    assert mediajelly_db.migration_pending() is True


# ---- Guarda del cron runner ----

def _cron(tmp_path, monkeypatch, events):
    cron = MediaJellyCron.__new__(MediaJellyCron)
    cron.logger = _Logger()
    cron.tmp_path = tmp_path
    cron.lock_file = tmp_path / "cron_runner.lock"
    monkeypatch.setattr(cron, "run_scanner", lambda: events.append("scanner") or True)
    monkeypatch.setattr(cron, "run_language_detector", lambda: events.append("detector") or True)
    monkeypatch.setattr(cron, "_start_nfo_translator", lambda: None)
    monkeypatch.setattr(cron, "_start_subtitle_translator", lambda extra_args=None: None)
    monkeypatch.setattr(cron, "_finish_subtitle_translator", lambda job: True)
    monkeypatch.setattr(cron, "_finish_nfo_translator", lambda job: None)
    monkeypatch.setattr(cron, "_is_night_time", lambda: False)
    monkeypatch.setattr(cron, "run_processor", lambda: events.append("processor") or True)
    monkeypatch.setattr(cron, "_release_lock", lambda: events.append("lock:release"))
    return cron


def test_cron_cycle_blocked_while_migration_pending(db, tmp_path, monkeypatch):
    _legacy_history("/mediajelly/media/a.mkv")
    events = []
    cron = _cron(tmp_path, monkeypatch, events)

    assert cron.run_cycle() is False

    assert events == ["lock:release"]
    assert any(MENSAJE in e for e in cron.logger.errors)


def test_cron_cycle_runs_after_migration(db, tmp_path, monkeypatch):
    _legacy_history("/mediajelly/media/a.mkv")
    mediajelly_db.mark_legacy_migrated()
    events = []
    cron = _cron(tmp_path, monkeypatch, events)

    assert cron.run_cycle() is True

    assert events == ["scanner", "detector", "processor", "lock:release"]


def test_cron_cycle_runs_on_fresh_install(db, tmp_path, monkeypatch):
    events = []
    cron = _cron(tmp_path, monkeypatch, events)

    assert cron.run_cycle() is True
    assert "processor" in events


# ---- Guarda del procesador ----

def _processor(tmp_path):
    proc = MediaJellyProcessor.__new__(MediaJellyProcessor)
    proc.logger = _Logger()
    proc.tmp_dir = tmp_path
    proc.stats = ProcessingStats()
    proc.processed_files = {}
    proc.config = None
    return proc


def test_processor_blocked_while_migration_pending(db, tmp_path):
    _legacy_history("/mediajelly/media/a.mkv")
    proc = _processor(tmp_path)

    def forbid(*a, **k):
        raise AssertionError("no debía procesar con la migración pendiente")

    proc.cleanup_processed_files = forbid
    proc.scan_media_files = forbid

    stats = proc.run()

    assert stats is proc.stats
    assert any(MENSAJE in e for e in proc.logger.errors)


def test_processor_runs_on_fresh_install(db, tmp_path):
    proc = _processor(tmp_path)
    calls = []
    proc.cleanup_processed_files = lambda: calls.append("cleanup")
    proc.scan_media_files = lambda: calls.append("scan") or []
    proc._check_all_pending_processed = lambda: None

    proc.run()

    assert calls == ["cleanup", "scan"]


# ---- Guarda del detector de idiomas ----

def test_detector_blocked_while_migration_pending(db, monkeypatch):
    _legacy_history("/mediajelly/media/a.mkv")
    logs = []
    monkeypatch.setattr(detector, "log", lambda msg, level="INFO": logs.append((level, msg)))

    def forbid(*a, **k):
        raise AssertionError("no debía analizar con la migración pendiente")

    monkeypatch.setattr(detector, "adjust_pending_names", forbid)
    monkeypatch.setattr(detector, "scan_files_needing_detection", forbid)

    detector.main()

    assert ("ERROR", MENSAJE) in logs


def test_detector_runs_after_migration(db, monkeypatch):
    _legacy_history("/mediajelly/media/a.mkv")
    mediajelly_db.mark_legacy_migrated()
    calls = []
    monkeypatch.setattr(detector, "log", lambda msg, level="INFO": None)
    monkeypatch.setattr(detector, "adjust_pending_names", lambda: None)
    monkeypatch.setattr(detector, "scan_files_needing_detection", lambda: calls.append("scan") or [])

    detector.main()

    assert calls == ["scan"]
