"""Subtítulos nombrados con su idioma: {stem}.{xx}.srt, sin duplicar el español."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from mediajelly_politica import idioma_de_subtitulo  # noqa: E402
from mediajelly_scanner import MediaScanner  # noqa: E402
from mediajelly_subtitle_translator import SubtitleTranslator  # noqa: E402


class _Logger:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


@pytest.fixture
def translator(tmp_path):
    instance = SubtitleTranslator.__new__(SubtitleTranslator)
    instance.logger = _Logger()
    instance.tmp_dir = tmp_path / "tmp"
    instance.tmp_dir.mkdir()
    instance.allow_retranslate = False
    instance._probe_cache = {}
    return instance


def _write_srt(path, cues=300, spacing=4.0, text="Hello there"):
    def ts(seconds):
        return f"{int(seconds // 3600):02d}:{int(seconds % 3600 // 60):02d}:{int(seconds % 60):02d},000"

    blocks = [f"{i + 1}\n{ts(i * spacing)} --> {ts(i * spacing + 2)}\n{text}\n" for i in range(cues)]
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path


def _video(tmp_path, nombre="Show.mkv"):
    video = tmp_path / nombre
    video.touch()
    return video


@pytest.mark.parametrize(
    "nombre, stem, esperado",
    [
        ("Show.ja.srt", "Show", ("ja", None)),
        ("Show.en.sdh.srt", "Show", ("en", "sdh")),
        ("Show.es-419.srt", "Show", ("es-419", None)),
        ("Show.hi.srt", "Show", ("hi", None)),
        ("Show.srt", "Show", None),
        ("Show.temp.srt", "Show", None),
        ("Other.ja.srt", "Show", None),
    ],
)
def test_idioma_de_subtitulo(nombre, stem, esperado):
    assert idioma_de_subtitulo(nombre, stem) == esperado


def test_whisper_japones_se_guarda_como_ja(translator, tmp_path):
    video = _video(tmp_path)
    temp = _write_srt(translator.tmp_dir / "Show_123.srt")

    destino = translator._move_srt_to_video_folder(temp, video, "ja")

    assert destino == tmp_path / "Show.ja.srt"
    assert destino.exists()
    assert not (tmp_path / "Show.srt").exists()


def test_whisper_espanol_solo_es_srt(translator, tmp_path, monkeypatch):
    video = _video(tmp_path)
    temp = _write_srt(translator.tmp_dir / "Show_123.srt", text="Creo que el plan de la casa es para los que van")
    monkeypatch.setattr(translator, "detect_srt_language", lambda srt: "es")
    monkeypatch.setattr(translator, "_improve_translated_subtitles_quality", lambda srt: None)

    destino = translator._move_srt_to_video_folder(temp, video, "es")
    final = translator.translate_srt(destino)

    assert destino == tmp_path / "Show.es.srt"
    assert final == destino
    assert sorted(p.name for p in tmp_path.glob("*.srt")) == ["Show.es.srt"]


def test_whisper_sin_idioma_queda_generico(translator, tmp_path):
    video = _video(tmp_path)
    temp = _write_srt(translator.tmp_dir / "Show_123.srt")

    assert translator._move_srt_to_video_folder(temp, video, None) == tmp_path / "Show.srt"


def test_destino_embebido_con_etiqueta(translator, tmp_path):
    video = _video(tmp_path)
    srt = _write_srt(translator.tmp_dir / "cand.srt")
    assert translator._destino_srt(video, srt, "jpn") == tmp_path / "Show.ja.srt"


def test_idioma_desconocido_queda_generico(translator, tmp_path, monkeypatch):
    video = _video(tmp_path)
    srt = _write_srt(translator.tmp_dir / "cand.srt")
    monkeypatch.setattr(translator, "detect_srt_language", lambda s: "unknown")
    assert translator._destino_srt(video, srt, None) == tmp_path / "Show.srt"


def test_zh_cn_se_normaliza(translator, tmp_path, monkeypatch):
    video = _video(tmp_path)
    srt = _write_srt(translator.tmp_dir / "cand.srt")
    monkeypatch.setattr(translator, "detect_srt_language", lambda s: "zh-cn")
    assert translator._destino_srt(video, srt, "und") == tmp_path / "Show.zh.srt"


@pytest.mark.parametrize(
    "nombre, esperado",
    [
        ("X.ja.srt", "X"),
        ("X.en.sdh.srt", "X"),
        ("X.es-419.srt", "X"),
        ("X.hi.srt", "X"),
        ("X.es.srt", "X"),
        ("Avatar S03E01 [ES].srt", "Avatar S03E01 [ES]"),
        ("Doctor.Who.srt", "Doctor.Who"),
    ],
)
def test_prepare_output_filename_quita_sufijos(translator, tmp_path, nombre, esperado):
    assert translator._prepare_output_filename(tmp_path / nombre) == esperado


def test_prepare_output_filename_respeta_el_nombre_del_video(translator, tmp_path):
    # "Show.It" termina en lo que parece un código de idioma, pero es el nombre del video
    _video(tmp_path, "Show.It.mkv")
    assert translator._prepare_output_filename(tmp_path / "Show.It.srt") == "Show.It"
    assert translator._prepare_output_filename(tmp_path / "Show.It.ja.srt") == "Show.It"


def test_find_existing_encuentra_ja_srt(translator, tmp_path, monkeypatch):
    video = _video(tmp_path)
    ja = _write_srt(tmp_path / "Show.ja.srt")
    monkeypatch.setattr(translator, "_media_duration", lambda v: 1200.0)
    assert translator.find_existing_srt_file(video) == ja


def test_find_existing_prefiere_en(translator, tmp_path, monkeypatch):
    video = _video(tmp_path)
    _write_srt(tmp_path / "Show.ja.srt")
    en = _write_srt(tmp_path / "Show.en.srt")
    monkeypatch.setattr(translator, "_media_duration", lambda v: 1200.0)
    assert translator.find_existing_srt_file(video) == en


def test_find_existing_prefiere_idioma_sobre_generico(translator, tmp_path, monkeypatch):
    video = _video(tmp_path)
    _write_srt(tmp_path / "Show.srt")
    ja = _write_srt(tmp_path / "Show.ja.srt")
    monkeypatch.setattr(translator, "_media_duration", lambda v: 1200.0)
    assert translator.find_existing_srt_file(video) == ja


def test_find_existing_ignora_espanol_y_sdh(translator, tmp_path, monkeypatch):
    video = _video(tmp_path)
    _write_srt(tmp_path / "Show.es-419.srt")
    _write_srt(tmp_path / "Show.en.sdh.srt")
    _write_srt(tmp_path / "Show.hi.srt")
    monkeypatch.setattr(translator, "_media_duration", lambda v: 1200.0)
    assert translator.find_existing_srt_file(video) is None


def test_find_existing_respalda_ja_insuficiente(translator, tmp_path, monkeypatch):
    video = _video(tmp_path)
    ja = _write_srt(tmp_path / "Show.ja.srt", cues=20, spacing=70.0)
    monkeypatch.setattr(translator, "_media_duration", lambda v: 1440.0)

    assert translator.find_existing_srt_file(video) is None
    assert not ja.exists()
    assert (tmp_path / "Show.ja.srt.bak").exists()


def test_limpieza_borra_ja_vacio_pero_no_uno_reciente(translator, tmp_path):
    video = _video(tmp_path)
    vacio = tmp_path / "Show.ja.srt"
    vacio.write_text("1\n", encoding="utf-8")
    translator._check_and_cleanup_incomplete_srt(video)
    assert not vacio.exists()

    otro = _video(tmp_path, "Otro.mkv")
    reciente = _write_srt(tmp_path / "Otro.en.srt")
    translator._check_and_cleanup_incomplete_srt(otro)
    assert reciente.exists()


def test_scanner_reconoce_espanol_regional(tmp_path, monkeypatch):
    scanner = MediaScanner.__new__(MediaScanner)
    scanner.logger = _Logger()

    class _SinEmbebidos:
        returncode = 0
        stdout = '{"streams": []}'

    monkeypatch.setattr("mediajelly_scanner.subprocess.run", lambda *a, **k: _SinEmbebidos())

    video = _video(tmp_path)
    _write_srt(tmp_path / "Show.es-419.srt")
    assert scanner._has_spanish_subtitle(video)

    otro = _video(tmp_path, "Otro.mkv")
    _write_srt(tmp_path / "Otro.ja.srt")
    assert not scanner._has_spanish_subtitle(otro)


def test_detectar_idioma_ignora_etiquetas_font(translator, tmp_path):
    pytest.importorskip("langdetect")
    lineas = [
        "I'm back here again.", "Sister, Sister. It appears our guest is still half-asleep.",
        "What is the matter, sir?", "Do you feel unwell?", "What's wrong, sir?",
        "Do you have a chronic illness flaring up?",
    ]
    bloques = [
        f'{i + 1}\n00:00:{i:02d},000 --> 00:00:{i:02d},900\n<font face="Gandhi Sans"size="75"><b>{t}</b></font>\n'
        for i, t in enumerate(lineas * 5)
    ]
    srt = tmp_path / "Show.srt"
    srt.write_text("\n".join(bloques), encoding="utf-8")
    assert translator.detect_srt_language(srt) == "en"
