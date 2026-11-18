"""Migración de subtítulos al esquema {stem}.{idioma}.srt y auditoría de desfase."""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import mediajelly_db  # noqa: E402
from mediajelly_renombrar_subs import aplicar, main, planear  # noqa: E402

SRT_EN = "1\n00:00:00,000 --> 00:00:01,000\nHello there\n"
SRT_ES = "1\n00:00:00,000 --> 00:00:01,000\nHola\n"


def _detectar(srt: Path) -> str:
    texto = srt.read_text(encoding="utf-8")
    return "en" if "Hello" in texto else "es" if "Hola" in texto else "unknown"


def _video(carpeta: Path, nombre="Show.mkv") -> Path:
    video = carpeta / nombre
    video.write_bytes(os.urandom(64 * 1024))
    viejo = time.time() - 600
    os.utime(video, (viejo, viejo))
    return video


def test_borra_srt_identico_a_es(tmp_path):
    _video(tmp_path)
    (tmp_path / "Show.srt").write_text(SRT_ES, encoding="utf-8")
    (tmp_path / "Show.es.srt").write_text(SRT_ES, encoding="utf-8")

    acciones = planear(tmp_path, _detectar)
    assert [(a.tipo, a.origen.name) for a in acciones] == [("borrar", "Show.srt")]
    assert aplicar(acciones)["borrar"] == 1
    assert sorted(p.name for p in tmp_path.glob("*.srt")) == ["Show.es.srt"]


def test_renombra_srt_ingles_a_en(tmp_path):
    _video(tmp_path)
    (tmp_path / "Show.srt").write_text(SRT_EN, encoding="utf-8")
    aplicar(planear(tmp_path, _detectar))
    assert (tmp_path / "Show.en.srt").exists()
    assert not (tmp_path / "Show.srt").exists()


def test_hi_ingles_pasa_a_en_sdh(tmp_path):
    _video(tmp_path)
    (tmp_path / "Show.hi.srt").write_text(SRT_EN, encoding="utf-8")
    aplicar(planear(tmp_path, _detectar))
    assert (tmp_path / "Show.en.sdh.srt").exists()


def test_destino_existente_se_omite(tmp_path):
    _video(tmp_path)
    (tmp_path / "Show.srt").write_text(SRT_EN, encoding="utf-8")
    (tmp_path / "Show.en.srt").write_text(SRT_EN + "\n", encoding="utf-8")

    acciones = planear(tmp_path, _detectar)
    assert [(a.tipo, a.motivo) for a in acciones] == [("omitir", "destino existe")]
    aplicar(acciones)
    assert (tmp_path / "Show.srt").exists()


def test_idioma_desconocido_se_omite(tmp_path):
    _video(tmp_path)
    (tmp_path / "Show.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\n♪\n", encoding="utf-8")
    assert [a.tipo for a in planear(tmp_path, _detectar)] == ["omitir"]


def test_dry_run_no_toca_nada(tmp_path):
    _video(tmp_path)
    (tmp_path / "Show.srt").write_text(SRT_EN, encoding="utf-8")
    assert main([str(tmp_path)], detectar=_detectar) == 0
    assert (tmp_path / "Show.srt").exists()
    assert not (tmp_path / "Show.en.srt").exists()


def test_auditar_desfase_lista_y_marca(tmp_path, capsys):
    video = _video(tmp_path, "Avatar S01E01.mp4")
    mediajelly_db.register(video)
    probe = {"streams": [{"codec_type": "video", "duration": "1417"}, {"codec_type": "audio", "duration": "1105"}]}

    main([str(tmp_path), "--auditar-desfase", "--aplicar"], detectar=_detectar, probar=lambda v: probe)

    assert "Audio desfasado: 1 video(s)" in capsys.readouterr().out
    avisos = mediajelly_db.listar_avisos("audio_desfasado")
    assert len(avisos) == 1
    assert avisos[0][2] == "audio -312 s respecto al video"


def test_idioma_dudoso_se_omite(tmp_path):
    _video(tmp_path)
    (tmp_path / "Show.srt").write_text(SRT_EN, encoding="utf-8")
    acciones = planear(tmp_path, lambda srt: "ca")
    assert [(a.tipo, a.motivo) for a in acciones] == [("omitir", "idioma dudoso (ca)")]
