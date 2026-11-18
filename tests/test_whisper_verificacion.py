"""Audio normalizado antes de transcribir y verificación de transcripciones en español."""

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import mediajelly_db  # noqa: E402
import mediajelly_whisper_extractor as extractor_mod  # noqa: E402
from mediajelly_whisper_extractor import WhisperAudioExtractorMixin  # noqa: E402


class _Logger:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


@pytest.fixture
def mixin(tmp_path):
    instancia = WhisperAudioExtractorMixin.__new__(WhisperAudioExtractorMixin)
    instancia.logger = _Logger()
    instancia.tmp_dir = tmp_path / "tmp"
    instancia.tmp_dir.mkdir()
    return instancia


def _resultado(cobertura_hasta, idioma="es", logprob=-0.3):
    return {
        "segments": [{"start": 0.0, "end": cobertura_hasta, "text": "hola, ¿cómo estás?"}],
        "language": idioma,
        "logprob_medio": logprob,
    }


# --- Audio normalizado -------------------------------------------------------


def _comandos(monkeypatch, mixin, metodo, tmp_path):
    vistos = []

    def fake_run(cmd, *a, **k):
        vistos.append(cmd)
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(extractor_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(mixin, "_validate_extracted_audio", lambda audio: True, raising=False)
    getattr(mixin, metodo)(tmp_path / "v.mkv", tmp_path / "a.wav")
    return vistos


@pytest.mark.parametrize("metodo", ["_extract_audio_normal", "_extract_audio_corruption_tolerant"])
def test_extraccion_normaliza_la_voz(monkeypatch, mixin, tmp_path, metodo):
    (cmd,) = _comandos(monkeypatch, mixin, metodo, tmp_path)
    filtro = cmd[cmd.index("-af") + 1]
    assert filtro.startswith("aresample=async=1:first_pts=0,")
    assert "dynaudnorm" in filtro and "loudnorm" in filtro


def test_remux_no_usa_el_filtro(monkeypatch, mixin, tmp_path):
    (tmp_path / "a.temp.aac").write_bytes(b"x")
    for cmd in _comandos(monkeypatch, mixin, "_extract_audio_remux", tmp_path):
        assert "loudnorm" not in " ".join(cmd)


# --- Verificación y reintento ------------------------------------------------


def _preparar(monkeypatch, mixin, robusto):
    llamadas = []

    def transcribir(audio, robusto_flag=False):
        llamadas.append(robusto_flag)
        if isinstance(robusto, Exception):
            raise robusto
        return robusto

    monkeypatch.setattr(mixin, "_transcribe_with_faster_whisper", lambda audio, robusto=False: transcribir(audio, robusto))
    monkeypatch.setattr(mixin, "_leer_tramos_voz", lambda audio: [(0.0, 10.0)])
    monkeypatch.setattr(extractor_mod, "FASTER_WHISPER_AVAILABLE", True)
    return llamadas


def test_espanol_con_buena_cobertura_no_reintenta(monkeypatch, mixin, tmp_path):
    llamadas = _preparar(monkeypatch, mixin, _resultado(10.0))
    resultado, ev = mixin._verificar_y_reintentar(tmp_path / "a.wav", _resultado(9.5))
    assert ev.ok
    assert llamadas == []
    assert resultado["segments"][0]["end"] == 9.5


def test_espanol_con_baja_cobertura_reintenta_y_usa_el_mejor(monkeypatch, mixin, tmp_path):
    llamadas = _preparar(monkeypatch, mixin, _resultado(9.0))
    resultado, ev = mixin._verificar_y_reintentar(tmp_path / "a.wav", _resultado(4.0))
    assert llamadas == [True]
    assert ev.ok
    assert ev.cobertura == pytest.approx(0.9)
    assert resultado["segments"][0]["end"] == 9.0


def test_reintento_peor_conserva_el_primero(monkeypatch, mixin, tmp_path):
    _preparar(monkeypatch, mixin, _resultado(2.0))
    resultado, ev = mixin._verificar_y_reintentar(tmp_path / "a.wav", _resultado(4.0))
    assert not ev.ok
    assert resultado["segments"][0]["end"] == 4.0


def test_reintento_falla_conserva_original(monkeypatch, mixin, tmp_path):
    _preparar(monkeypatch, mixin, RuntimeError("ffmpeg"))
    original = _resultado(4.0)
    resultado, ev = mixin._verificar_y_reintentar(tmp_path / "a.wav", original)
    assert resultado is original
    assert not ev.ok


def test_no_espanol_no_se_verifica(monkeypatch, mixin, tmp_path):
    def no_llamar(audio):
        raise AssertionError("no debe medir la voz")

    monkeypatch.setattr(mixin, "_leer_tramos_voz", no_llamar)
    original = _resultado(1.0, idioma="ja")
    assert mixin._verificar_y_reintentar(tmp_path / "a.wav", original) == (original, None)


def test_vad_falla_no_bloquea(monkeypatch, mixin, tmp_path):
    def falla(audio):
        raise RuntimeError("sin onnx")

    monkeypatch.setattr(mixin, "_leer_tramos_voz", falla)
    original = _resultado(1.0)
    assert mixin._verificar_y_reintentar(tmp_path / "a.wav", original) == (original, None)


# --- Aviso en la BD ----------------------------------------------------------


def _video(path: Path) -> Path:
    path.write_bytes(os.urandom(64 * 1024))
    viejo = time.time() - 600
    os.utime(path, (viejo, viejo))
    return path


def _extraer(monkeypatch, mixin, video, cobertura_reintento):
    monkeypatch.setattr(extractor_mod, "WHISPER_AVAILABLE", True)
    monkeypatch.setattr(mixin, "_extract_audio_from_video", lambda v, a: a.write_bytes(b"wav") or True)
    monkeypatch.setattr(mixin, "_transcription_hints", lambda v: None, raising=False)
    monkeypatch.setattr(mixin, "_improve_extracted_subtitles_quality", lambda srt, idioma: None, raising=False)
    monkeypatch.setattr(mixin, "_transcribe_audio_with_whisper", lambda a: _resultado(4.0))
    _preparar(monkeypatch, mixin, _resultado(cobertura_reintento))
    return mixin.extract_subtitles_from_audio(video)


def test_marca_revisar_si_ambos_fallan(monkeypatch, mixin, tmp_path):
    video = _video(tmp_path / "Notas 002.mkv")
    mediajelly_db.register(video)

    srt = _extraer(monkeypatch, mixin, video, 3.0)

    assert srt == tmp_path / "Notas 002.es.srt"
    avisos = mediajelly_db.listar_avisos("revisar_subtitulo")
    assert len(avisos) == 1
    assert "cobertura" in avisos[0][2]


def test_quita_revisar_si_la_nueva_pasa(monkeypatch, mixin, tmp_path):
    video = _video(tmp_path / "Notas 002.mkv")
    rec, _ = mediajelly_db.register(video)
    mediajelly_db.marcar_aviso(rec.fingerprint, "revisar_subtitulo", "viejo")

    _extraer(monkeypatch, mixin, video, 9.5)

    assert mediajelly_db.listar_avisos() == []


def test_video_sin_registrar_no_marca(monkeypatch, mixin, tmp_path):
    video = _video(tmp_path / "Suelto.mkv")
    assert _extraer(monkeypatch, mixin, video, 3.0) == tmp_path / "Suelto.es.srt"
    assert mediajelly_db.listar_avisos() == []
