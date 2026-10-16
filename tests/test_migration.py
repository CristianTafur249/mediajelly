import hashlib
import json
import sqlite3
import sys
from argparse import Namespace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import mediajelly_db  # noqa: E402
import mediajelly_migrate as migrate  # noqa: E402

SHOW = "Serial Experiments Lain"


class _Logger:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


@pytest.fixture
def env(tmp_path, monkeypatch):
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    db_path = tmp_dir / "mediajelly.db"
    monkeypatch.setattr(mediajelly_db, "_get_db_path", lambda: db_path)
    media = tmp_path / "media"
    media.mkdir()
    return Namespace(tmp_dir=tmp_dir, db=db_path, media=media)


def _video(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(path.name.encode() * 200)
    return path


def _legacy_completed(*paths) -> None:
    mediajelly_db.init_db()
    conn = sqlite3.connect(str(mediajelly_db._get_db_path()))
    with conn:
        conn.executemany(
            "INSERT INTO completed_files(path, label, completed_at) VALUES (?, NULL, datetime('now'))",
            [(str(p),) for p in paths],
        )
    conn.close()


def _sha(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def _snapshot():
    conn = sqlite3.connect(str(mediajelly_db._get_db_path()))
    media = conn.execute("SELECT fingerprint, path, status, attempts FROM media_files ORDER BY path").fetchall()
    langs = conn.execute("SELECT fingerprint, stream_index, lang FROM stream_languages ORDER BY 1, 2").fetchall()
    conn.close()
    return media, langs


def test_episode_key():
    assert migrate.episode_key(f"{SHOW} - S01E01 Communication HDTV-720p.mp4") == (SHOW.lower(), 1, 1)
    assert migrate.episode_key("Perfect Blue (1997) Bluray-1080p.mkv") == "perfect blue"


def test_dry_run_writes_nothing(env, capsys):
    done = _video(env.media / SHOW / f"{SHOW} - S01E02 Girls.mp4")
    _video(env.media / SHOW / f"{SHOW} - S01E03 Psyche.mp4")
    _legacy_completed(done)
    before = _sha(env.db)

    assert migrate.main(["--media-root", str(env.media)]) == 0

    assert _sha(env.db) == before
    out = capsys.readouterr().out
    assert "simulación" in out.lower()
    assert "done: 1" in out and "pending: 1" in out
    assert not any(p.name.startswith("migration-backup") for p in env.tmp_dir.iterdir())


def test_apply_twice_same_result(env):
    done = _video(env.media / SHOW / f"{SHOW} - S01E02 Girls.mp4")
    _video(env.media / SHOW / f"{SHOW} - S01E03 Psyche.mp4")
    _legacy_completed(done)
    (env.tmp_dir / "language_cache.json").write_text(json.dumps({str(done): {"1": "ja"}}))

    assert migrate.main(["--media-root", str(env.media), "--apply"]) == 0
    first = _snapshot()
    assert migrate.main(["--media-root", str(env.media), "--apply"]) == 0

    assert _snapshot() == first
    assert mediajelly_db.get_by_path(done).status == "done"
    backups = [p for p in env.tmp_dir.iterdir() if p.name.startswith("migration-backup")]
    assert backups and all((b / "mediajelly.db").exists() for b in backups)


def test_renamed_completed_episode_becomes_done(env):
    old = env.media / SHOW / f"{SHOW} - S01E01 Communication HDTV-720p.mp4"
    new = _video(env.media / SHOW / f"{SHOW} - S01E01 Communication HDTV-1080p.mp4")
    _legacy_completed(old)

    plan = migrate.build_plan(env.media, env.tmp_dir)
    assert [(r[0], r[2]) for r in plan.rows] == [(str(new), "done")]
    assert plan.doubtful == [str(new)]

    migrate.apply_plan(plan, env.tmp_dir / "respaldo")
    assert mediajelly_db.get_by_path(new).status == "done"


def test_redownloaded_mkv_does_not_inherit_done(env):
    """El procesador siempre produce .mp4: un .mkv nunca es una salida procesada (Dr. STONE "Proper")."""
    old = env.media / SHOW / f"{SHOW} - S01E01 Communication HDTV-720p.mp4"
    new = _video(env.media / SHOW / f"{SHOW} - S01E01 Communication Bluray-1080p Proper.mkv")
    _legacy_completed(old)

    plan = migrate.build_plan(env.media, env.tmp_dir)
    assert [(r[0], r[2]) for r in plan.rows] == [(str(new), "pending")]
    assert plan.doubtful == []


def test_failed_and_pending_states(env):
    failed = _video(env.media / "Peli" / "Perfect Blue (1997) Bluray-1080p.mkv")
    pending = _video(env.media / "Peli" / "Paprika (2006) Bluray-1080p.mkv")
    _legacy_completed()
    (env.tmp_dir / "failed-compression.txt").write_text(f"{failed} | 2026-10-01 10:00 | ffmpeg falló\n")

    plan = migrate.build_plan(env.media, env.tmp_dir)
    assert dict((r[0], r[2]) for r in plan.rows) == {str(failed): "failed", str(pending): "pending"}
    assert plan.doubtful == []


def test_language_cache_old_path_imported(env):
    rel_old = f"{SHOW}/{SHOW} - S01E14 Myself [IT].mp4"
    new = _video(env.media / SHOW / f"{SHOW} - S01E14 Myself [ES] [IT].mp4")
    _legacy_completed()
    (env.tmp_dir / "language_cache.json").write_text(json.dumps({rel_old: {"1": "ja"}}))

    plan = migrate.build_plan(env.media, env.tmp_dir)
    migrate.apply_plan(plan, env.tmp_dir / "respaldo")

    rec = mediajelly_db.get_by_path(new)
    assert mediajelly_db.get_stream_languages(rec.fingerprint) == {1: "jpn"}


def test_undetected_language_not_imported(env):
    vid = _video(env.media / SHOW / f"{SHOW} - S01E05 Distortion.mp4")
    _legacy_completed()
    (env.tmp_dir / "language_cache.json").write_text(json.dumps({str(vid): {"1": "und", "2": "en"}}))

    plan = migrate.build_plan(env.media, env.tmp_dir)
    migrate.apply_plan(plan, env.tmp_dir / "respaldo")

    rec = mediajelly_db.get_by_path(vid)
    assert mediajelly_db.get_stream_languages(rec.fingerprint) == {2: "eng"}


def test_wrong_media_root_aborts(env, capsys):
    _video(env.media / SHOW / f"{SHOW} - S01E02 Girls.mp4")
    _legacy_completed(f"/otra/raiz/{SHOW}/{SHOW} - S01E02 Girls.mp4", f"/otra/raiz/{SHOW}/{SHOW} - S01E04 Religion.mp4")
    before = _sha(env.db)

    assert migrate.main(["--media-root", str(env.media), "--apply"]) != 0

    assert "Las rutas guardadas no coinciden con --media-root; ¿se está corriendo fuera del contenedor?" in capsys.readouterr().out
    assert _sha(env.db) == before


def test_apply_does_not_downgrade_existing_state(env):
    vid = _video(env.media / SHOW / f"{SHOW} - S01E06 Kids.mp4")
    _legacy_completed()
    rec, _ = mediajelly_db.register(vid, require_settled=False)
    mediajelly_db.set_status(rec.fingerprint, "failed")

    migrate.apply_plan(migrate.build_plan(env.media, env.tmp_dir), env.tmp_dir / "respaldo")

    assert mediajelly_db.get_by_path(vid).status == "failed"


def test_apply_aborts_when_cron_lock_busy(env, capsys):
    import fcntl
    import os

    _legacy_completed()
    fd = os.open(str(env.tmp_dir / "cron_runner.lock"), os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert migrate.main(["--media-root", str(env.media), "--apply"]) != 0
    finally:
        os.close(fd)
    assert "ocupado" in capsys.readouterr().out


def test_translator_reads_queue_from_db(env):
    import mediajelly_subtitle_translator as translator_module

    a = _video(env.media / "a.mp4")
    b = _video(env.media / "b.mp4")
    for v in (a, b):
        mediajelly_db.register(v, require_settled=False)
    mediajelly_db.replace_subtitle_queue([str(a), str(b)])

    translator = translator_module.SubtitleTranslator.__new__(translator_module.SubtitleTranslator)
    translator.logger = _Logger()
    args = Namespace(paths=[])
    assert translator_module._handle_resume_logic(args, translator) is None
    assert args.paths == [str(a), str(b)]

    translator._remove_from_pending_subtitles([str(a)])
    assert mediajelly_db.get_subtitle_queue() == [b]


# --- Ronda de corrección 1 ---


def test_unreadable_history_aborts(env, capsys):
    _video(env.media / SHOW / f"{SHOW} - S01E02 Girls.mp4")
    assert not env.db.exists()

    assert migrate.main(["--media-root", str(env.media), "--apply"]) != 0
    assert "historial" in capsys.readouterr().out.lower()
    assert not env.db.exists()

    conn = sqlite3.connect(str(env.db))
    conn.execute("CREATE TABLE otra (x)")
    conn.close()
    assert migrate.main(["--media-root", str(env.media)]) != 0
    assert "historial" in capsys.readouterr().out.lower()


def test_empty_history_aborts_without_flag(env, capsys):
    vid = _video(env.media / SHOW / f"{SHOW} - S01E02 Girls.mp4")
    _legacy_completed()

    assert migrate.main(["--media-root", str(env.media), "--apply"]) != 0
    assert "--allow-empty-history" in capsys.readouterr().out
    assert mediajelly_db.get_by_path(vid) is None

    assert migrate.main(["--media-root", str(env.media), "--apply", "--allow-empty-history"]) == 0
    assert mediajelly_db.get_by_path(vid).status == "pending"


def test_completed_txt_is_history(env):
    done = _video(env.media / SHOW / f"{SHOW} - S01E02 Girls.mp4")
    _legacy_completed()
    (env.tmp_dir / "completed.txt").write_text(f"{done}\n")

    plan = migrate.build_plan(env.media, env.tmp_dir)
    assert [(r[0], r[2]) for r in plan.rows] == [(str(done), "done")]
    assert plan.completed_total == 1


def test_remake_in_other_folder_not_done(env):
    old = env.media / "Peliculas" / "Dune (1984)" / "Dune (1984) Bluray-1080p.mkv"
    new = _video(env.media / "Peliculas" / "Dune (2021)" / "Dune (2021) Bluray-1080p.mkv")
    _legacy_completed(old)

    plan = migrate.build_plan(env.media, env.tmp_dir)
    assert [(r[0], r[2]) for r in plan.rows] == [(str(new), "pending")]
    assert plan.doubtful == []


def test_same_name_series_other_folder_not_done(env):
    old = env.media / "anime" / "Monster" / f"Monster - S01E01 Herr Dr. Tenma HDTV-720p.mp4"
    new = _video(env.media / "series" / "Monster" / f"Monster - S01E01 Pilot WEBDL-1080p.mp4")
    _legacy_completed(old)

    plan = migrate.build_plan(env.media, env.tmp_dir)
    assert [(r[0], r[2]) for r in plan.rows] == [(str(new), "pending")]


def test_old_version_still_on_disk_not_done(env):
    old = _video(env.media / SHOW / f"{SHOW} - S01E01 Communication HDTV-720p.mp4")
    new = _video(env.media / SHOW / f"{SHOW} - S01E01 Communication HDTV-1080p.mp4")
    _legacy_completed(old)

    plan = migrate.build_plan(env.media, env.tmp_dir)
    assert dict((r[0], r[2]) for r in plan.rows) == {str(old): "done", str(new): "pending"}
    assert plan.doubtful == []


def test_ambiguous_candidates_not_done(env):
    old = env.media / SHOW / f"{SHOW} - S01E01 Communication HDTV-720p.mp4"
    a = _video(env.media / SHOW / f"{SHOW} - S01E01 Communication HDTV-1080p.mp4")
    b = _video(env.media / SHOW / f"{SHOW} - S01E01 Communication WEBDL-1080p.mkv")
    _legacy_completed(old)

    plan = migrate.build_plan(env.media, env.tmp_dir)
    assert dict((r[0], r[2]) for r in plan.rows) == {str(a): "pending", str(b): "pending"}
    assert plan.doubtful == []


def test_language_by_episode_requires_same_folder(env):
    other = _video(env.media / "series" / "Monster" / "Monster - S01E01 Pilot WEBDL-1080p.mp4")
    a = _video(env.media / SHOW / f"{SHOW} - S01E14 Myself [ES] [IT].mp4")
    b = _video(env.media / SHOW / f"{SHOW} - S01E14 Myself WEBDL.mkv")
    _legacy_completed()
    (env.tmp_dir / "language_cache.json").write_text(json.dumps({
        "anime/Monster/Monster - S01E01 Herr Dr. Tenma.mp4": {"1": "ja"},
        f"{SHOW}/{SHOW} - S01E14 Myself [IT].mp4": {"1": "ja"},
    }))

    plan = migrate.build_plan(env.media, env.tmp_dir)
    assert str(other) not in plan.languages
    assert str(a) not in plan.languages and str(b) not in plan.languages


def test_apply_keeps_existing_stream_language(env):
    vid = _video(env.media / SHOW / f"{SHOW} - S01E07 Society.mp4")
    _legacy_completed()
    rec, _ = mediajelly_db.register(vid, require_settled=False)
    mediajelly_db.set_stream_language(rec.fingerprint, 1, "spa", "whisper")
    (env.tmp_dir / "language_cache.json").write_text(json.dumps({str(vid): {"1": "ja", "2": "en"}}))

    migrate.apply_plan(migrate.build_plan(env.media, env.tmp_dir), env.tmp_dir / "respaldo")

    assert mediajelly_db.get_stream_languages(rec.fingerprint) == {1: "spa", 2: "eng"}


def test_apply_row_error_continues(env, monkeypatch, capsys):
    bad = _video(env.media / SHOW / f"{SHOW} - S01E08 Rumors.mp4")
    good = _video(env.media / SHOW / f"{SHOW} - S01E09 Protocol.mp4")
    _legacy_completed(bad, good)
    plan = migrate.build_plan(env.media, env.tmp_dir)
    real = mediajelly_db.set_status
    bad_fp = dict((r[0], r[1]) for r in plan.rows)[str(bad)]

    def flaky(fp, status):
        if fp == bad_fp:
            raise sqlite3.OperationalError("disk I/O error")
        real(fp, status)

    monkeypatch.setattr(mediajelly_db, "set_status", flaky)
    migrate.apply_plan(plan, env.tmp_dir / "respaldo")

    assert mediajelly_db.get_by_path(good).status == "done"
    assert plan.failed_rows == [str(bad)]
    assert "1 fila(s) fallaron" in capsys.readouterr().out
