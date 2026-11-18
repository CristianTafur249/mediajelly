"""Tests de las métricas de calidad de transcripción y del desfase de audio."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from mediajelly_calidad_subs import alucinaciones, cobertura_voz, desfase_audio, evaluar  # noqa: E402


def test_cobertura_parcial():
    cues = [{"start": 0.0, "end": 5.0, "text": "a"}]
    assert cobertura_voz(cues, [(0.0, 5.0), (10.0, 15.0)]) == pytest.approx(0.5)


def test_cobertura_no_cuenta_dos_veces_cues_solapados():
    cues = [{"start": 0.0, "end": 4.0, "text": "a"}, {"start": 2.0, "end": 6.0, "text": "b"}]
    assert cobertura_voz(cues, [(0.0, 10.0)]) == pytest.approx(0.6)


def test_cobertura_sin_voz_es_completa():
    assert cobertura_voz([], []) == 1.0


def test_alucinaciones_creditos_y_repeticiones():
    textos = ["Suscríbete al canal", "hola", "hola", "hola", "hola", "Subtítulos por la comunidad"]
    cues = [{"start": i, "end": i + 1, "text": t} for i, t in enumerate(textos)]
    assert alucinaciones(cues) == 4  # 2 créditos + 2 repeticiones (3.ª y 4.ª "hola")


def test_dos_repeticiones_no_son_bucle():
    cues = [{"start": i, "end": i + 1, "text": "sí"} for i in range(2)]
    assert alucinaciones(cues) == 0


def test_evaluar_falla_por_cobertura():
    ev = evaluar([{"start": 0, "end": 1, "text": "a"}], [(0, 1), (2, 10)], -0.3)
    assert not ev.ok
    assert any("cobertura" in m for m in ev.motivos)


def test_evaluar_ok():
    ev = evaluar([{"start": 0, "end": 10, "text": "a"}], [(0, 10)], -0.3)
    assert ev.ok
    assert ev.motivos == []


def test_evaluar_falla_por_logprob():
    assert not evaluar([{"start": 0, "end": 10, "text": "a"}], [(0, 10)], -1.2).ok


def test_evaluar_sin_logprob_no_lo_evalua():
    assert evaluar([{"start": 0, "end": 10, "text": "a"}], [(0, 10)], None).ok


def test_evaluar_falla_por_alucinaciones():
    cues = [{"start": i, "end": i + 1, "text": "amara.org"} for i in range(3)]
    ev = evaluar(cues, [(0, 3)], -0.2)
    assert not ev.ok
    assert any("alucinaciones" in m for m in ev.motivos)


def test_desfase_audio_corto():
    probe = {
        "format": {"duration": "1418"},
        "streams": [
            {"codec_type": "video", "duration": "1417.79"},
            {"codec_type": "audio", "duration": "1365.82"},
        ],
    }
    assert desfase_audio(probe) == pytest.approx(-51.97, abs=0.01)


def test_desfase_audio_toma_la_pista_mas_desfasada():
    probe = {
        "streams": [
            {"codec_type": "video", "duration": "100"},
            {"codec_type": "audio", "duration": "99.5"},
            {"codec_type": "audio", "duration": "130"},
        ]
    }
    assert desfase_audio(probe) == pytest.approx(30.0)


def test_desfase_audio_usa_format_si_el_video_no_tiene_duracion():
    probe = {"format": {"duration": "100"}, "streams": [{"codec_type": "video"}, {"codec_type": "audio", "duration": "90"}]}
    assert desfase_audio(probe) == pytest.approx(-10.0)


def test_desfase_audio_sin_datos():
    assert desfase_audio({"streams": [{"codec_type": "video"}]}) is None
    assert desfase_audio({"streams": [{"codec_type": "video", "duration": "10"}]}) is None
    assert desfase_audio({"streams": [{"codec_type": "audio", "duration": "10"}]}) is None
    assert desfase_audio(None) is None
