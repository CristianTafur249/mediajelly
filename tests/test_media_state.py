import multiprocessing
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import pytest

SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent / "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import mediajelly_db  # noqa: E402
import mediajelly_identity as identity  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "t.db"
    monkeypatch.setattr(mediajelly_db, "_get_db_path", lambda: db_path)
    return db_path


def _video(path: Path, size: int = 64 * 1024, settled: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(os.urandom(size))
    if settled:
        old = time.time() - 600
        os.utime(path, (old, old))
    return path


def _sql(db_path: Path, query: str, params=()):
    conn = sqlite3.connect(str(db_path))
    with conn:
        rows = conn.execute(query, params).fetchall()
    conn.close()
    return rows


def test_register_new_is_pending(db, tmp_path):
    video = _video(tmp_path / "a.mkv")
    rec, event = mediajelly_db.register(video)
    assert event == "new"
    assert rec.status == "pending"
    assert rec.attempts == 0
    assert rec.path == str(video)
    assert rec.fingerprint == identity.fingerprint(video)
    assert rec.derived_from is None


def test_register_rename_keeps_status(db, tmp_path):
    video = _video(tmp_path / "Show S01E01.mp4")
    rec, _ = mediajelly_db.register(video)
    mediajelly_db.set_status(rec.fingerprint, "done")

    renamed = tmp_path / "Show S01E01 [JA+EN] Café's, Part & 1.mp4"
    video.rename(renamed)
    rec2, event = mediajelly_db.register(renamed)

    assert event == "renamed"
    assert rec2.status == "done"
    assert rec2.fingerprint == rec.fingerprint
    assert mediajelly_db.get_by_path(renamed) is not None
    assert mediajelly_db.get_by_path(video) is None


def test_register_unchanged(db, tmp_path):
    video = _video(tmp_path / "a.mkv")
    mediajelly_db.register(video)
    rec, event = mediajelly_db.register(video)
    assert event == "unchanged"
    assert rec.status == "pending"


def test_register_unsettled_skipped(db, tmp_path):
    video = _video(tmp_path / "a.mkv", settled=False)
    assert mediajelly_db.register(video, require_settled=True) == (None, "unsettled")
    assert mediajelly_db.get_by_path(video) is None


def test_register_unsettled_allowed_when_not_required(db, tmp_path):
    video = _video(tmp_path / "a.mkv", settled=False)
    rec, event = mediajelly_db.register(video, require_settled=False)
    assert event == "new"
    assert rec is not None


def test_register_unreadable_is_unsettled(db, tmp_path):
    missing = tmp_path / "no-existe.mkv"
    assert mediajelly_db.register(missing, require_settled=False) == (None, "unsettled")


def test_register_duplicate_not_inserted(db, tmp_path):
    video = _video(tmp_path / "a.mkv")
    mediajelly_db.register(video)
    copy = tmp_path / "copia" / "a.mkv"
    copy.parent.mkdir()
    shutil.copy2(video, copy)

    rec, event = mediajelly_db.register(copy)

    assert event == "duplicate"
    assert rec.path == str(video)
    assert len(mediajelly_db.list_by_status("pending")) == 1
    assert mediajelly_db.get_by_path(copy) is None


def test_same_path_new_content_processing_is_adopted(db, tmp_path):
    video = _video(tmp_path / "a.mkv")
    rec, _ = mediajelly_db.register(video)
    mediajelly_db.set_status(rec.fingerprint, "processing")

    _video(video)  # mismo nombre, contenido nuevo
    rec2, event = mediajelly_db.register(video)

    assert event == "adopted"
    assert rec2.status == "processing"
    assert rec2.fingerprint != rec.fingerprint
    assert rec2.fingerprint == identity.fingerprint(video)
    assert len(_sql(db, "SELECT 1 FROM media_files")) == 1


def test_same_path_new_content_done_is_replaced_pending(db, tmp_path):
    video = _video(tmp_path / "a.mkv")
    rec, _ = mediajelly_db.register(video)
    mediajelly_db.set_status(rec.fingerprint, "done")

    _video(video)
    rec2, event = mediajelly_db.register(video)

    assert event == "replaced"
    assert rec2.status == "pending"
    assert rec2.fingerprint != rec.fingerprint
    assert len(_sql(db, "SELECT 1 FROM media_files")) == 1


def test_fingerprint_cache_is_used(db, tmp_path, monkeypatch):
    video = _video(tmp_path / "a.mkv")
    mediajelly_db.register(video)

    def _boom(_path):
        raise AssertionError("no debía recalcular la huella")

    monkeypatch.setattr(mediajelly_db, "fingerprint", _boom)
    rec, event = mediajelly_db.register(video)
    assert event == "unchanged"
    assert rec is not None


def test_record_rewrite_moves_state(db, tmp_path):
    video = _video(tmp_path / "a.mkv")
    rec, _ = mediajelly_db.register(video)
    old_fp = rec.fingerprint
    mediajelly_db.set_status(old_fp, "done")
    mediajelly_db.set_stream_language(old_fp, 1, "ja", "whisper")

    output = _video(tmp_path / "a.mp4")
    video.unlink()
    new_rec = mediajelly_db.record_rewrite(old_fp, output)

    assert new_rec.fingerprint == identity.fingerprint(output)
    assert new_rec.path == str(output)
    assert new_rec.derived_from == old_fp
    assert new_rec.status == "done"
    assert mediajelly_db.get_stream_languages(new_rec.fingerprint) == {1: "jpn"}
    assert mediajelly_db.get_stream_languages(old_fp) == {}
    assert _sql(db, "SELECT 1 FROM media_files WHERE fingerprint = ?", (old_fp,)) == []


def test_recover_processing_does_not_count_attempt(db, tmp_path):
    rec, _ = mediajelly_db.register(_video(tmp_path / "a.mkv"))
    mediajelly_db.set_status(rec.fingerprint, "processing")

    assert mediajelly_db.recover_processing() == 1

    after = mediajelly_db.list_by_status("pending")
    assert len(after) == 1
    assert after[0].attempts == 0
    assert mediajelly_db.list_by_status("processing") == []


def test_record_failure_quarantine(db, tmp_path):
    a, _ = mediajelly_db.register(_video(tmp_path / "a.mkv"))
    assert mediajelly_db.record_failure(a.fingerprint, "ffmpeg falló", resource=False) == "pending"
    assert mediajelly_db.record_failure(a.fingerprint, "ffmpeg falló", resource=False) == "pending"
    assert mediajelly_db.record_failure(a.fingerprint, "ffmpeg falló", resource=False) == "failed"
    assert mediajelly_db.list_by_status("failed")[0].attempts == 3

    b, _ = mediajelly_db.register(_video(tmp_path / "b.mkv"))
    for _ in range(5):
        assert mediajelly_db.record_failure(b.fingerprint, "sin GPU", resource=True) == "pending"
    rec_b = mediajelly_db.get_by_path(tmp_path / "b.mkv")
    assert rec_b.status == "pending"
    assert rec_b.attempts == 0
    assert _sql(db, "SELECT failure_type FROM media_files WHERE fingerprint = ?", (b.fingerprint,)) == [("resource",)]

    c, _ = mediajelly_db.register(_video(tmp_path / "c.mkv"))
    _sql(
        db,
        "UPDATE media_files SET first_failed_at = datetime('now', '-25 hours') WHERE fingerprint = ?",
        (c.fingerprint,),
    )
    assert mediajelly_db.record_failure(c.fingerprint, "ffmpeg falló", resource=False) == "failed"


def test_first_failed_at_set_only_once(db, tmp_path):
    a, _ = mediajelly_db.register(_video(tmp_path / "a.mkv"))
    mediajelly_db.record_failure(a.fingerprint, "uno", resource=False)
    _sql(
        db,
        "UPDATE media_files SET first_failed_at = '2000-01-01 00:00:00' WHERE fingerprint = ?",
        (a.fingerprint,),
    )
    assert mediajelly_db.record_failure(a.fingerprint, "dos", resource=False) == "failed"
    assert _sql(db, "SELECT first_failed_at, last_error FROM media_files") == [("2000-01-01 00:00:00", "dos")]


def test_mark_missing_and_purge(db, tmp_path):
    rec, _ = mediajelly_db.register(_video(tmp_path / "a.mkv"))

    assert mediajelly_db.mark_missing_except(set()) == 1
    assert mediajelly_db.get_by_path(tmp_path / "a.mkv").status == "missing"
    # Una fila ya `missing` no se vuelve a contar.
    assert mediajelly_db.mark_missing_except(set()) == 0

    assert mediajelly_db.purge_missing() == 0
    _sql(
        db,
        "UPDATE media_files SET missing_since = datetime('now', '-31 days') WHERE fingerprint = ?",
        (rec.fingerprint,),
    )
    assert mediajelly_db.purge_missing() == 1
    assert _sql(db, "SELECT 1 FROM media_files") == []


def test_mark_missing_keeps_existing(db, tmp_path):
    a = _video(tmp_path / "a.mkv")
    b = _video(tmp_path / "b.mkv")
    mediajelly_db.register(a)
    mediajelly_db.register(b)
    assert mediajelly_db.mark_missing_except({str(a)}) == 1
    assert mediajelly_db.get_by_path(a).status == "pending"
    assert mediajelly_db.get_by_path(b).status == "missing"


def test_missing_restored_on_reappear(db, tmp_path):
    video = _video(tmp_path / "a.mkv")
    rec, _ = mediajelly_db.register(video)
    mediajelly_db.set_status(rec.fingerprint, "done")
    mediajelly_db.mark_missing_except(set())

    moved = tmp_path / "otra" / "a.mkv"
    moved.parent.mkdir()
    video.rename(moved)
    rec2, event = mediajelly_db.register(moved)

    assert event == "renamed"
    assert rec2.status == "done"
    assert _sql(db, "SELECT missing_since FROM media_files") == [(None,)]

    mediajelly_db.mark_missing_except(set())
    rec3, event = mediajelly_db.register(moved)
    assert event == "unchanged"
    assert rec3.status == "done"


def test_normalize_lang():
    assert mediajelly_db.normalize_lang("ja") == "jpn"
    assert mediajelly_db.normalize_lang("en") == "eng"
    assert mediajelly_db.normalize_lang("es") == "spa"
    assert mediajelly_db.normalize_lang("fr") == "fre"
    assert mediajelly_db.normalize_lang("de") == "ger"
    assert mediajelly_db.normalize_lang("it") == "ita"
    assert mediajelly_db.normalize_lang("pt") == "por"
    assert mediajelly_db.normalize_lang("") == "und"
    assert mediajelly_db.normalize_lang("SPA") == "spa"
    assert mediajelly_db.normalize_lang("und") == "und"


def test_stream_languages_round_trip(db, tmp_path):
    rec, _ = mediajelly_db.register(_video(tmp_path / "a.mkv"))
    mediajelly_db.set_stream_language(rec.fingerprint, 1, "jpn", "whisper")
    mediajelly_db.set_stream_language(rec.fingerprint, 2, "EN", "tag")
    mediajelly_db.set_stream_language(rec.fingerprint, 1, "ja", "whisper")
    assert mediajelly_db.get_stream_languages(rec.fingerprint) == {1: "jpn", 2: "eng"}


def test_list_by_status_requires_status(db):
    with pytest.raises(ValueError):
        mediajelly_db.list_by_status()


def test_subtitle_queue_replace_and_remove(db, tmp_path):
    a = _video(tmp_path / "a.mkv")
    b = _video(tmp_path / "b.mkv")
    c = _video(tmp_path / "c.mkv")
    for p in (a, b, c):
        mediajelly_db.register(p)

    mediajelly_db.replace_subtitle_queue([str(a), str(b)])
    assert mediajelly_db.get_subtitle_queue() == [a, b]

    mediajelly_db.replace_subtitle_queue([str(b), str(c)])
    assert mediajelly_db.get_subtitle_queue() == [b, c]

    mediajelly_db.remove_from_subtitle_queue([str(b)])
    assert mediajelly_db.get_subtitle_queue() == [c]


def test_subtitle_queue_follows_rename(db, tmp_path):
    a = _video(tmp_path / "a.mkv")
    mediajelly_db.register(a)
    mediajelly_db.replace_subtitle_queue([str(a)])
    renamed = tmp_path / "a [ES].mkv"
    a.rename(renamed)
    mediajelly_db.register(renamed)
    assert mediajelly_db.get_subtitle_queue() == [renamed]


def test_schema_version_is_3(db):
    mediajelly_db.init_db()
    assert _sql(db, "SELECT version FROM schema_version") == [(3,)]
    mediajelly_db.init_db()
    assert _sql(db, "SELECT version FROM schema_version") == [(3,)]


def _register_in_child(db_path: str, video_path: str, queue) -> None:
    sys.path.insert(0, SCRIPTS_DIR)
    import mediajelly_db as child_db

    child_db._get_db_path = lambda: Path(db_path)
    try:
        _, event = child_db.register(Path(video_path), require_settled=False)
        queue.put(("ok", event))
    except Exception as exc:  # pragma: no cover - solo informa al padre
        queue.put(("error", repr(exc)))


def test_concurrent_register_two_processes(db, tmp_path):
    video = _video(tmp_path / "a.mkv", size=4 * 1024 * 1024, settled=False)
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    procs = [ctx.Process(target=_register_in_child, args=(str(db), str(video), queue)) for _ in range(2)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=120)
    results = [queue.get(timeout=10) for _ in procs]

    assert all(p.exitcode == 0 for p in procs)
    assert all(status == "ok" for status, _ in results), results
    assert sorted(event for _, event in results) == ["new", "unchanged"]
    assert len(_sql(db, "SELECT 1 FROM media_files")) == 1


def test_subtitle_queue_skips_unregistered_paths(db, tmp_path):
    known = _video(tmp_path / "a.mkv")
    mediajelly_db.register(known)
    copying = _video(tmp_path / "copiando.mkv", settled=False)

    mediajelly_db.replace_subtitle_queue([str(known), str(copying)])

    assert mediajelly_db.get_subtitle_queue() == [known]
    assert mediajelly_db.get_by_path(copying) is None


def test_success_resets_failure_counters(db, tmp_path):
    rec, _ = mediajelly_db.register(_video(tmp_path / "a.mkv"))
    mediajelly_db.record_failure(rec.fingerprint, "ffmpeg falló", resource=False)
    _sql(
        db,
        "UPDATE media_files SET first_failed_at = datetime('now', '-25 hours') WHERE fingerprint = ?",
        (rec.fingerprint,),
    )
    mediajelly_db.set_status(rec.fingerprint, "done")
    assert _sql(
        db,
        "SELECT attempts, first_failed_at, failure_type FROM media_files WHERE fingerprint = ?",
        (rec.fingerprint,),
    ) == [(0, None, None)]

    assert mediajelly_db.record_failure(rec.fingerprint, "ffmpeg falló", resource=False) == "pending"
    assert mediajelly_db.get_by_path(tmp_path / "a.mkv").attempts == 1


def test_skipped_also_resets_failure_counters(db, tmp_path):
    rec, _ = mediajelly_db.register(_video(tmp_path / "a.mkv"))
    mediajelly_db.record_failure(rec.fingerprint, "x", resource=False)
    mediajelly_db.set_status(rec.fingerprint, "skipped")
    assert mediajelly_db.get_by_path(tmp_path / "a.mkv").attempts == 0


def test_failure_on_quarantined_row_keeps_failed(db, tmp_path):
    rec, _ = mediajelly_db.register(_video(tmp_path / "a.mkv"))
    for _ in range(3):
        mediajelly_db.record_failure(rec.fingerprint, "ffmpeg falló", resource=False)
    before = _sql(
        db,
        "SELECT status, attempts, first_failed_at, last_error, failure_type FROM media_files WHERE fingerprint = ?",
        (rec.fingerprint,),
    )
    assert before[0][0] == "failed"

    assert mediajelly_db.record_failure(rec.fingerprint, "sin GPU", resource=True) == "failed"
    assert mediajelly_db.record_failure(rec.fingerprint, "otro", resource=False) == "failed"

    after = _sql(
        db,
        "SELECT status, attempts, first_failed_at, last_error, failure_type FROM media_files WHERE fingerprint = ?",
        (rec.fingerprint,),
    )
    assert after == before


def test_mark_completed_fresh_output_registered(db, tmp_path):
    video = _video(tmp_path / "salida.mp4", settled=False)
    mediajelly_db.mark_completed(video)
    assert mediajelly_db.is_completed(video) is True


def test_completed_survives_rename(db, tmp_path):
    a = _video(tmp_path / "Better Call Saul S01E01.mkv")
    mediajelly_db.mark_completed(a)
    b = tmp_path / "Better Call Saul - S01E01 - Uno.mkv"
    a.rename(b)
    mediajelly_db.register(b)
    assert mediajelly_db.is_completed(b) is True
    assert b not in mediajelly_db.get_pending_files()


def test_add_pending_then_get(db, tmp_path):
    a = _video(tmp_path / "a.mkv")
    b = _video(tmp_path / "b.mkv")
    mediajelly_db.add_pending(a)
    mediajelly_db.add_pending(b)
    assert mediajelly_db.get_pending_files() == [a, b]
    mediajelly_db.mark_completed(a)
    assert mediajelly_db.get_pending_files() == [b]
    assert mediajelly_db.get_all_completed_files() == [a]


def test_wrappers_ignore_nonexistent_path(db, tmp_path):
    ghost = tmp_path / "no_existe.mkv"
    mediajelly_db.mark_completed(ghost)
    mediajelly_db.add_pending(ghost)
    assert mediajelly_db.is_completed(ghost) is False
    assert mediajelly_db.get_pending_files() == []


def test_remove_pending_marks_missing_if_gone(db, tmp_path):
    a = _video(tmp_path / "a.mkv")
    mediajelly_db.add_pending(a)
    a.unlink()
    mediajelly_db.remove_pending(a)
    assert mediajelly_db.get_pending_files() == []
    assert mediajelly_db.get_by_path(a).status == "missing"


def test_add_pending_does_not_requeue_renamed_done(db, tmp_path):
    a = _video(tmp_path / "a.mkv")
    mediajelly_db.mark_completed(a)
    b = tmp_path / "b.mkv"
    a.rename(b)
    mediajelly_db.add_pending(b)
    assert mediajelly_db.is_completed(b) is True
    assert b not in mediajelly_db.get_pending_files()


def test_add_pending_does_not_unquarantine_renamed_failed(db, tmp_path):
    a = _video(tmp_path / "a.mkv")
    rec, _ = mediajelly_db.register(a)
    for _ in range(3):
        mediajelly_db.record_failure(rec.fingerprint, "ffmpeg falló", resource=False)
    b = tmp_path / "b.mkv"
    a.rename(b)
    mediajelly_db.add_pending(b)
    assert mediajelly_db.get_by_path(b).status == "failed"
    assert b not in mediajelly_db.get_pending_files()


@pytest.mark.parametrize("status", ["done", "skipped", "failed"])
def test_add_pending_keeps_unchanged_final_state(db, tmp_path, status):
    """add_pending nunca devuelve a pending un archivo ya terminado, tampoco con evento unchanged."""
    a = _video(tmp_path / "a.mkv")
    rec, _ = mediajelly_db.register(a)
    mediajelly_db.set_status(rec.fingerprint, status)
    mediajelly_db.add_pending(a)
    assert mediajelly_db.get_by_path(a).status == status
    assert mediajelly_db.get_pending_files() == []


def test_add_pending_duplicate_keeps_original_done(db, tmp_path):
    a = _video(tmp_path / "a.mkv")
    mediajelly_db.mark_completed(a)
    copia = tmp_path / "copia.mkv"
    shutil.copy2(a, copia)
    mediajelly_db.add_pending(copia)
    assert mediajelly_db.get_by_path(a).status == "done"
    assert mediajelly_db.get_pending_files() == []


def test_mark_completed_duplicate_acts_on_original_row(db, tmp_path):
    a = _video(tmp_path / "a.mkv")
    rec, _ = mediajelly_db.register(a)
    copia = tmp_path / "copia.mkv"
    shutil.copy2(a, copia)
    mediajelly_db.mark_completed(copia)
    assert mediajelly_db.get_by_path(a).status == "done"
    assert mediajelly_db.get_by_path(a).fingerprint == rec.fingerprint


def test_remove_pending_existing_pending_becomes_skipped(db, tmp_path):
    a = _video(tmp_path / "a.mkv")
    mediajelly_db.add_pending(a)
    mediajelly_db.remove_pending(a)
    assert mediajelly_db.get_by_path(a).status == "skipped"
    assert mediajelly_db.get_pending_files() == []


def test_remove_pending_keeps_done(db, tmp_path):
    a = _video(tmp_path / "a.mkv")
    mediajelly_db.mark_completed(a)
    mediajelly_db.remove_pending(a)
    assert mediajelly_db.get_by_path(a).status == "done"


def test_remove_pending_missing_recovers_previous_status(db, tmp_path):
    a = _video(tmp_path / "a.mkv")
    mediajelly_db.add_pending(a)
    saved = a.read_bytes()
    mtime = a.stat().st_mtime
    a.unlink()
    mediajelly_db.remove_pending(a)
    assert mediajelly_db.get_by_path(a).status == "missing"
    a.write_bytes(saved)
    os.utime(a, (mtime, mtime))
    mediajelly_db.register(a)
    assert mediajelly_db.get_by_path(a).status == "pending"


def test_mark_missing_respects_roots(db, tmp_path):
    a = _video(tmp_path / "a" / "x.mkv")
    b = _video(tmp_path / "b" / "y.mkv")
    mediajelly_db.register(a)
    mediajelly_db.register(b)

    # Solo se marcan las filas bajo las raíces indicadas.
    assert mediajelly_db.mark_missing_except(set(), roots=[str(tmp_path / "a")]) == 1
    assert mediajelly_db.get_by_path(a).status == "missing"
    assert mediajelly_db.get_by_path(b).status == "pending"
    # Una raíz que solo comparte prefijo de texto no cuenta ("/a" vs "/ab").
    assert mediajelly_db.mark_missing_except(set(), roots=[str(tmp_path / "b") + "x"]) == 0
    assert mediajelly_db.mark_missing_except(set(), roots=[]) == 0


def test_rename_is_logged_with_old_and_new_name(db, tmp_path, caplog):
    video = _video(tmp_path / "Viejo.mkv")
    mediajelly_db.register(video)
    new = video.rename(tmp_path / "Nuevo.mkv")

    with caplog.at_level("INFO", logger="mediajelly_db"):
        _, event = mediajelly_db.register(new)
    assert event == "renamed"
    assert "Renombrado: Viejo.mkv → Nuevo.mkv" in caplog.text


# ---- Revisión final ----

def _legacy_history(db_path: Path, *paths) -> None:
    mediajelly_db.init_db()
    _sql(db_path, "DELETE FROM completed_files")
    for p in paths:
        _sql(db_path, "INSERT INTO completed_files(path, label, completed_at) VALUES (?, NULL, datetime('now'))", (str(p),))


def test_migration_not_pending_without_db(tmp_path, monkeypatch):
    db_path = tmp_path / "no-existe.db"
    monkeypatch.setattr(mediajelly_db, "_get_db_path", lambda: db_path)
    assert mediajelly_db.migration_pending() is False
    assert not db_path.exists()


def test_migration_not_pending_on_fresh_install(db):
    mediajelly_db.init_db()
    assert mediajelly_db.migration_pending() is False


def test_migration_pending_with_history_until_marked(db):
    _legacy_history(db, "/mediajelly/media/a.mkv")
    assert mediajelly_db.migration_pending() is True
    mediajelly_db.mark_legacy_migrated()
    assert mediajelly_db.migration_pending() is False
    assert mediajelly_db.get_meta("legacy_migrated")
    assert _sql(db, "SELECT key FROM meta") == [("legacy_migrated",)]


def test_record_rewrite_can_drop_languages(db, tmp_path):
    original = _video(tmp_path / "a.mkv")
    rec, _ = mediajelly_db.register(original)
    mediajelly_db.set_stream_language(rec.fingerprint, 1, "jpn", "whisper")
    output = _video(tmp_path / "a.mp4")
    original.unlink()

    new_rec = mediajelly_db.record_rewrite(rec.fingerprint, output, carry_languages=False)

    assert new_rec.path == str(output)
    assert mediajelly_db.get_stream_languages(new_rec.fingerprint) == {}
    assert mediajelly_db.get_stream_languages(rec.fingerprint) == {}


def test_record_rewrite_carries_languages_by_default(db, tmp_path):
    original = _video(tmp_path / "a.mp4")
    rec, _ = mediajelly_db.register(original)
    mediajelly_db.set_stream_language(rec.fingerprint, 1, "jpn", "whisper")
    original.write_bytes(os.urandom(64 * 1024))

    new_rec = mediajelly_db.record_rewrite(rec.fingerprint, original)

    assert new_rec.fingerprint != rec.fingerprint
    assert mediajelly_db.get_stream_languages(new_rec.fingerprint) == {1: "jpn"}


def test_language_candidates_are_done_or_skipped(db, tmp_path):
    files = {s: _video(tmp_path / f"{s}.mp4") for s in ("pending", "done", "skipped", "failed")}
    for status, path in files.items():
        rec, _ = mediajelly_db.register(path)
        mediajelly_db.set_status(rec.fingerprint, status)
    paths = sorted(r.path for r in mediajelly_db.list_language_candidates())
    assert paths == sorted([str(files["done"]), str(files["skipped"])])


def test_language_candidates_skip_retag_failures(db, tmp_path):
    a = _video(tmp_path / "a.mp4")
    rec, _ = mediajelly_db.register(a)
    mediajelly_db.set_status(rec.fingerprint, "done")
    mediajelly_db.record_retag_failure(rec.fingerprint, "error")
    assert mediajelly_db.list_language_candidates() == []


def test_language_attempt_waits_seven_days(db, tmp_path):
    a = _video(tmp_path / "a.mp4")
    rec, _ = mediajelly_db.register(a)
    mediajelly_db.set_status(rec.fingerprint, "done")
    mediajelly_db.record_language_attempt(rec.fingerprint)
    assert mediajelly_db.list_language_candidates() == []

    _sql(db, "UPDATE language_attempts SET attempted_at = datetime('now', '-6 days')")
    assert mediajelly_db.list_language_candidates() == []
    _sql(db, "UPDATE language_attempts SET attempted_at = datetime('now', '-8 days')")
    assert [r.path for r in mediajelly_db.list_language_candidates()] == [str(a)]
    assert mediajelly_db.LANGUAGE_RETRY_DAYS == 7
