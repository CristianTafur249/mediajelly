"""Detección de audio desfasado del video y aviso único por Telegram."""

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import mediajelly_db  # noqa: E402
import mediajelly_subtitle_translator as traductor_mod  # noqa: E402
from mediajelly_subtitle_translator import SubtitleTranslator  # noqa: E402


class _Logger:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _probe(audio):
    streams = [{"codec_type": "video", "duration": "1417.79"}]
    if audio is not None:
        streams.append({"codec_type": "audio", "duration": str(1417.79 + audio)})
    return {"returncode": 0, "stderr": "", "data": {"streams": streams}}


@pytest.fixture
def entorno(tmp_path, monkeypatch):
    video = tmp_path / "Avatar S01E01.mp4"
    video.write_bytes(os.urandom(64 * 1024))
    viejo = time.time() - 600
    os.utime(video, (viejo, viejo))
    mediajelly_db.register(video)

    traductor = SubtitleTranslator.__new__(SubtitleTranslator)
    traductor.logger = _Logger()
    llamadas = []
    monkeypatch.setattr(traductor_mod.subprocess, "run", lambda cmd, **k: llamadas.append(cmd))
    return traductor, video, llamadas


def test_desfase_mayor_marca_y_notifica_una_vez(entorno, monkeypatch):
    traductor, video, llamadas = entorno
    monkeypatch.setattr(traductor, "_probe_media", lambda v: _probe(-51.97))

    assert traductor._revisar_desfase(video) == pytest.approx(-51.97)
    traductor._revisar_desfase(video)

    avisos = mediajelly_db.listar_avisos("audio_desfasado")
    assert len(avisos) == 1
    assert avisos[0][2] == "audio -52 s respecto al video"
    assert len(llamadas) == 1
    assert llamadas[0][2:] == ["audio_desynced", "Avatar S01E01.mp4", "-52.0"]


def test_desfase_menor_no_marca(entorno, monkeypatch):
    traductor, video, llamadas = entorno
    monkeypatch.setattr(traductor, "_probe_media", lambda v: _probe(-0.3))
    traductor._revisar_desfase(video)
    assert mediajelly_db.listar_avisos() == []
    assert llamadas == []


def test_sin_audio_no_marca(entorno, monkeypatch):
    traductor, video, llamadas = entorno
    monkeypatch.setattr(traductor, "_probe_media", lambda v: _probe(None))
    assert traductor._revisar_desfase(video) is None
    assert mediajelly_db.listar_avisos() == []


def test_notifier_falla_no_rompe(entorno, monkeypatch):
    traductor, video, _ = entorno
    monkeypatch.setattr(traductor, "_probe_media", lambda v: _probe(-312.0))

    def falla(cmd, **k):
        raise OSError("sin python")

    monkeypatch.setattr(traductor_mod.subprocess, "run", falla)
    assert traductor._revisar_desfase(video) == pytest.approx(-312.0)
    assert len(mediajelly_db.listar_avisos("audio_desfasado")) == 1


def test_notifier_arma_el_mensaje(monkeypatch):
    import mediajelly_notifier

    notifier = mediajelly_notifier.TelegramNotifier.__new__(mediajelly_notifier.TelegramNotifier)
    enviados = []
    monkeypatch.setattr(notifier, "send_long_message", lambda m: enviados.append(m) or True, raising=False)
    assert notifier.notify_audio_desynced("Avatar S01E01.mp4", -51.97)
    assert "Audio desfasado: Avatar S01E01.mp4" in enviados[0]
    assert "52 s menos" in enviados[0]
