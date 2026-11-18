"""Avisos por huella: audio desfasado y subtítulo a revisar."""

import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import mediajelly_db  # noqa: E402


def _video(path: Path, size: int = 64 * 1024) -> Path:
    path.write_bytes(os.urandom(size))
    viejo = time.time() - 600
    os.utime(path, (viejo, viejo))
    return path


def test_marcar_es_idempotente(tmp_path):
    rec, _ = mediajelly_db.register(_video(tmp_path / "a.mkv"))

    assert mediajelly_db.marcar_aviso(rec.fingerprint, "audio_desfasado", "audio -52 s") is True
    assert mediajelly_db.marcar_aviso(rec.fingerprint, "audio_desfasado", "audio -53 s") is False

    avisos = mediajelly_db.listar_avisos()
    assert len(avisos) == 1
    fp, tipo, detalle, creado, path = avisos[0]
    assert (fp, tipo, detalle, path) == (rec.fingerprint, "audio_desfasado", "audio -53 s", str(tmp_path / "a.mkv"))
    assert creado


def test_listar_por_tipo(tmp_path):
    rec, _ = mediajelly_db.register(_video(tmp_path / "a.mkv"))
    mediajelly_db.marcar_aviso(rec.fingerprint, "audio_desfasado")
    mediajelly_db.marcar_aviso(rec.fingerprint, "revisar_subtitulo", "cobertura 0.52 < 0.75")

    assert [a[1] for a in mediajelly_db.listar_avisos("revisar_subtitulo")] == ["revisar_subtitulo"]
    assert len(mediajelly_db.listar_avisos()) == 2


def test_quitar_aviso(tmp_path):
    rec, _ = mediajelly_db.register(_video(tmp_path / "a.mkv"))
    mediajelly_db.marcar_aviso(rec.fingerprint, "revisar_subtitulo")
    mediajelly_db.quitar_aviso(rec.fingerprint, "revisar_subtitulo")
    assert mediajelly_db.listar_avisos() == []
    # Quitar uno que no existe no falla
    mediajelly_db.quitar_aviso(rec.fingerprint, "revisar_subtitulo")


def test_tipo_invalido_falla():
    with pytest.raises(ValueError):
        mediajelly_db.marcar_aviso("x", "otro")


def test_aviso_sigue_a_la_huella(tmp_path):
    video = _video(tmp_path / "a.mkv")
    rec, _ = mediajelly_db.register(video)
    mediajelly_db.marcar_aviso(rec.fingerprint, "audio_desfasado")

    salida = _video(tmp_path / "a.mp4")
    video.unlink()
    nuevo = mediajelly_db.record_rewrite(rec.fingerprint, salida)

    assert [(a[0], a[4]) for a in mediajelly_db.listar_avisos()] == [(nuevo.fingerprint, str(salida))]


def test_aviso_se_borra_con_la_huella(tmp_path):
    rec, _ = mediajelly_db.register(_video(tmp_path / "a.mkv"))
    mediajelly_db.marcar_aviso(rec.fingerprint, "audio_desfasado")
    mediajelly_db.mark_missing_except(set())
    conn = sqlite3.connect(str(mediajelly_db._get_db_path()))
    with conn:
        conn.execute("UPDATE media_files SET missing_since = datetime('now', '-31 days')")
    conn.close()

    assert mediajelly_db.purge_missing() == 1
    assert mediajelly_db.listar_avisos() == []
