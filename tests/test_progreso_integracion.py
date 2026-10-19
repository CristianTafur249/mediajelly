"""Los procesos largos informan su avance sin arriesgar el trabajo real."""
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import mediajelly_progreso as progreso  # noqa: E402
import mediajelly_processor as processor_module  # noqa: E402
import mediajelly_cron_runner as runner_module  # noqa: E402
import mediajelly_language_detector as detector_module  # noqa: E402
import mediajelly_whisper_extractor as whisper_module  # noqa: E402
from mediajelly_processor import MediaJellyProcessor  # noqa: E402
from mediajelly_cron_runner import MediaJellyCron  # noqa: E402


class _Logger:
    handlers: list = []

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


@pytest.fixture(autouse=True)
def _dir_progreso(tmp_path, monkeypatch):
    destino = tmp_path / "progreso"
    monkeypatch.setattr(progreso, "DIR_PROGRESO", destino)
    return destino


# --- Procesador -------------------------------------------------------------

SALIDA = "/tmp/salida_temporal.mp4"


def _proc(monkeypatch):
    proc = MediaJellyProcessor.__new__(MediaJellyProcessor)
    proc.logger = _Logger()
    comando = lambda *a, **k: ["ffmpeg", "-i", "entrada.mkv", "-c:v", "libx264", SALIDA]
    for nombre in ("_build_ffmpeg_command", "_build_fallback_ffmpeg_command",
                   "_build_remux_command", "_build_ffmpeg_command_no_subtitles"):
        monkeypatch.setattr(proc, nombre, comando)
    return proc


def _ffmpeg_falso(monkeypatch, llamadas, duracion="120.5", rc=0, stderr="", al_correr=None):
    def run(cmd, *args, **kwargs):
        if cmd[0] == "ffprobe":
            if isinstance(duracion, BaseException):
                raise duracion
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{duracion}\n", stderr="")
        llamadas.append(list(cmd))
        if al_correr:
            al_correr(cmd)
        respuesta = rc(cmd) if callable(rc) else rc
        error = stderr(cmd) if callable(stderr) else stderr
        return subprocess.CompletedProcess(cmd, respuesta, stdout="", stderr=error)

    monkeypatch.setattr(processor_module.subprocess, "run", run)


def _comprimir(proc, use_gpu=False, use_remux=False):
    return proc._execute_compression_attempt(
        Path("/media/Pelicula (2020).mkv"), Path(SALIDA), use_gpu, [1], [], use_remux
    )


def test_comando_compresion_incluye_progress(monkeypatch, _dir_progreso):
    llamadas = []
    estado_durante = {}

    def al_correr(cmd):
        estado_durante.update(progreso.leer()["etapas"][0].__dict__)

    _ffmpeg_falso(monkeypatch, llamadas, al_correr=al_correr)
    process, _ = _comprimir(_proc(monkeypatch))

    assert process.returncode == 0
    cmd = llamadas[0]
    assert "-progress" in cmd and "-nostats" in cmd
    assert cmd[cmd.index("-progress") + 1] == str(_dir_progreso / f"compresion-{os.getpid()}.ffmpeg")
    assert cmd[-1] == SALIDA
    assert estado_durante["etapa"] == f"compresion-{os.getpid()}"
    assert estado_durante["archivo"] == "Pelicula (2020).mkv"
    assert estado_durante["duracion_s"] == 120.5
    assert estado_durante["detalle"] == "CPU (libx264)"
    assert not (_dir_progreso / f"compresion-{os.getpid()}.json").exists()


@pytest.mark.parametrize("use_gpu,use_remux,esperado", [
    (True, False, "GPU (VAAPI)"),
    (False, True, "Remux"),
    (False, False, "CPU (libx264)"),
])
def test_detalle_de_compresion_segun_metodo(monkeypatch, use_gpu, use_remux, esperado):
    detalles = []
    _ffmpeg_falso(monkeypatch, [], al_correr=lambda c: detalles.append(progreso.leer()["etapas"][0].detalle))
    _comprimir(_proc(monkeypatch), use_gpu=use_gpu, use_remux=use_remux)
    assert detalles == [esperado]


def test_ffprobe_que_falla_no_impide_comprimir(monkeypatch):
    llamadas, duraciones = [], []
    _ffmpeg_falso(monkeypatch, llamadas, duracion=subprocess.TimeoutExpired("ffprobe", 30),
                  al_correr=lambda c: duraciones.append(progreso.leer()["etapas"][0].duracion_s))
    process, _ = _comprimir(_proc(monkeypatch))
    assert process.returncode == 0 and len(llamadas) == 1
    assert duraciones == [None]


def test_ffprobe_sin_duracion_valida_da_none(monkeypatch):
    duraciones = []
    _ffmpeg_falso(monkeypatch, [], duracion="N/A",
                  al_correr=lambda c: duraciones.append(progreso.leer()["etapas"][0].duracion_s))
    _comprimir(_proc(monkeypatch))
    assert duraciones == [None]


def test_error_de_ffmpeg_tambien_termina_la_etapa(monkeypatch, _dir_progreso):
    def run(cmd, *a, **k):
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, stdout="10\n", stderr="")
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(processor_module.subprocess, "run", run)
    with pytest.raises(subprocess.TimeoutExpired):
        _comprimir(_proc(monkeypatch))
    assert not (_dir_progreso / f"compresion-{os.getpid()}.json").exists()


def test_sin_carpeta_de_progreso_ffmpeg_corre_sin_progress(monkeypatch, tmp_path):
    # Si no se puede crear la carpeta, ffmpeg abortaría con -progress apuntando a ella
    bloqueo = tmp_path / "archivo"
    bloqueo.write_text("")
    monkeypatch.setattr(progreso, "DIR_PROGRESO", bloqueo / "progreso")
    llamadas = []
    _ffmpeg_falso(monkeypatch, llamadas)
    process, _ = _comprimir(_proc(monkeypatch))
    assert process.returncode == 0
    assert "-progress" not in llamadas[0] and llamadas[0][-1] == SALIDA


def test_ffmpeg_que_rechaza_progress_se_reintenta_sin_el(monkeypatch):
    llamadas = []
    rechaza = lambda cmd: "-progress" in cmd
    _ffmpeg_falso(
        monkeypatch, llamadas,
        rc=lambda cmd: 254 if rechaza(cmd) else 0,
        stderr=lambda cmd: 'Failed to open progress URL "x": Permission denied' if rechaza(cmd) else "",
    )
    process, _ = _comprimir(_proc(monkeypatch))
    assert process.returncode == 0
    assert len(llamadas) == 2 and "-progress" not in llamadas[1]
    assert llamadas[1][-1] == SALIDA


def test_workers_paralelos_no_se_pisan(monkeypatch, _dir_progreso):
    # Dos workers del ProcessPool: cada uno con su pid y su propia etapa
    proc = _proc(monkeypatch)
    vistas = []

    def worker_a_corriendo(cmd):
        # Mientras A comprime, B inicia y termina su compresión
        monkeypatch.setattr(processor_module.os, "getpid", lambda: 2002)
        _ffmpeg_falso(monkeypatch, [], al_correr=lambda c: vistas.append(
            sorted(e.etapa for e in progreso.leer()["etapas"])))
        _comprimir(proc)
        vistas.append(sorted(e.etapa for e in progreso.leer()["etapas"]))

    monkeypatch.setattr(processor_module.os, "getpid", lambda: 1001)
    _ffmpeg_falso(monkeypatch, [], al_correr=worker_a_corriendo)
    _comprimir(proc)

    # Con B en marcha se ven las dos etapas; al terminar B, la de A sigue ahí
    assert vistas == [["compresion-1001", "compresion-2002"], ["compresion-1001"]]
    assert progreso.leer()["etapas"] == []


def test_ffprobe_una_sola_vez_por_archivo(monkeypatch):
    sondeos = []
    _ffmpeg_falso(monkeypatch, [])
    run = processor_module.subprocess.run

    def contar(cmd, *a, **k):
        if cmd[0] == "ffprobe":
            sondeos.append(cmd[-1])
        return run(cmd, *a, **k)

    monkeypatch.setattr(processor_module.subprocess, "run", contar)
    proc = _proc(monkeypatch)
    _comprimir(proc, use_gpu=True)  # intento GPU
    _comprimir(proc)                # reintento CPU del mismo archivo
    assert len(sondeos) == 1
    proc._execute_compression_attempt(Path("/media/Otra.mkv"), Path(SALIDA), False, [1], [])
    assert len(sondeos) == 2


def test_sin_carpeta_de_progreso_no_se_corre_ffprobe(monkeypatch, tmp_path):
    bloqueo = tmp_path / "archivo"
    bloqueo.write_text("")
    monkeypatch.setattr(progreso, "DIR_PROGRESO", bloqueo / "progreso")
    llamadas = []

    def run(cmd, *a, **k):
        llamadas.append(cmd[0])
        return subprocess.CompletedProcess(cmd, 0, stdout="10\n", stderr="")

    monkeypatch.setattr(processor_module.subprocess, "run", run)
    _comprimir(_proc(monkeypatch))
    assert llamadas == ["ffmpeg"]


# --- Cron runner ------------------------------------------------------------

@pytest.fixture
def runner(tmp_path, monkeypatch):
    cron = MediaJellyCron.__new__(MediaJellyCron)
    cron.logger = _Logger()
    cron.tmp_path = tmp_path
    cron.lock_file = tmp_path / "cron_runner.lock"
    fases = []
    monkeypatch.setattr(runner_module.progreso, "fase", lambda nombre: fases.append(nombre))
    monkeypatch.setattr(runner_module, "migration_pending", lambda: False)
    monkeypatch.setattr(cron, "run_scanner", lambda: fases.append("<scanner>") or True)
    monkeypatch.setattr(cron, "run_language_detector", lambda: fases.append("<detector>") or True)
    monkeypatch.setattr(cron, "_start_nfo_translator", lambda: None)
    monkeypatch.setattr(cron, "_finish_nfo_translator", lambda job: None)
    monkeypatch.setattr(cron, "_start_subtitle_translator", lambda extra_args=None: object())
    monkeypatch.setattr(cron, "_finish_subtitle_translator",
                        lambda job: (fases.append("<espera subs>") if job else None) or True)
    monkeypatch.setattr(cron, "run_processor", lambda: fases.append("<processor>") or True)
    monkeypatch.setattr(cron, "_release_lock", lambda: None)
    cron.fases = fases
    return cron


def _hora(monkeypatch, hour):
    class _Fija(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 18, hour, 0, 0)

    monkeypatch.setattr(runner_module, "datetime", _Fija)


def test_cron_marca_fases(runner, monkeypatch):
    _hora(monkeypatch, 1)
    assert runner.run_cycle() is True
    assert runner.fases == [
        "escaneo", "<scanner>", "idiomas", "<detector>", "compresion", "<processor>",
        "subtitulos", "<espera subs>", None,
    ]


def test_cron_marca_fases_de_dia(runner, monkeypatch):
    _hora(monkeypatch, 14)
    assert runner.run_cycle() is True
    assert [f for f in runner.fases if not (f or "").startswith("<")] == ["escaneo", "idiomas", "compresion", None]


def test_cron_limpia_la_fase_ante_un_error(runner, monkeypatch):
    _hora(monkeypatch, 14)

    def falla():
        raise RuntimeError("se cayó el procesador")

    monkeypatch.setattr(runner, "run_processor", falla)
    with pytest.raises(RuntimeError):
        runner.run_cycle()
    assert runner.fases[-1] is None


# --- Detector de idiomas ----------------------------------------------------

def test_detector_informa_archivo_actual(monkeypatch):
    eventos = []
    monkeypatch.setattr(detector_module.progreso, "iniciar",
                        lambda etapa, archivo, duracion_s=None, detalle="": eventos.append(
                            ("iniciar", etapa, archivo, duracion_s, detalle)))
    monkeypatch.setattr(detector_module.progreso, "terminar", lambda etapa: eventos.append(("terminar", etapa)))
    monkeypatch.setattr(detector_module, "load_whisper_model", lambda: object())
    monkeypatch.setattr(detector_module, "get_cached_language", lambda *a: None)
    monkeypatch.setattr(detector_module, "detect_language_with_multiple_samples",
                        lambda *a, **k: eventos.append(("detectar", a[2], k.get("contexto"))) or "spa")
    monkeypatch.setattr(detector_module, "set_cached_language", lambda *a: None)
    monkeypatch.setattr(detector_module, "retag_if_processed", lambda *a: None)

    archivos = [
        {"absolute_path": f"/media/{n}", "relative_path": n, "streams": [(1, "aac", 100.0)]}
        for n in ("uno.mkv", "dos.mkv")
    ]
    detector_module.process_files(archivos, {})

    assert eventos == [
        ("iniciar", "idiomas", "uno.mkv", None, "archivo 1 de 2"),
        ("detectar", 1, "archivo 1 de 2"),
        ("iniciar", "idiomas", "dos.mkv", None, "archivo 2 de 2"),
        ("detectar", 1, "archivo 2 de 2"),
        ("terminar", "idiomas"),
    ]


def _muestras_falsas(monkeypatch, tmp_path, idioma="en"):
    monkeypatch.setattr(detector_module, "extract_audio_sample",
                        lambda *a, **k: str(tmp_path / "muestra.wav"))
    monkeypatch.setattr(detector_module, "detect_language_with_whisper", lambda *a: (idioma, {}))


def test_detector_informa_cada_muestra(monkeypatch, tmp_path, _dir_progreso):
    """Cada archivo tarda minutos: el detalle cambia con cada muestra para no quedar "sin señal"."""
    _muestras_falsas(monkeypatch, tmp_path)
    vistos = []
    real = progreso.actualizar

    def actualizar(etapa, posicion_s=None, detalle=None):
        real(etapa, posicion_s, detalle)
        vistos.append(progreso.leer()["etapas"][0].detalle)

    monkeypatch.setattr(detector_module.progreso, "actualizar", actualizar)
    progreso.iniciar("idiomas", "uno.mkv", None, "archivo 3 de 7")

    resultado = detector_module.detect_language_with_multiple_samples(
        object(), "/media/uno.mkv", 2, 600.0, contexto="archivo 3 de 7")

    assert resultado == detector_module.LANGUAGE_CODE_MAP.get("en", "en")
    assert vistos == [f"muestra {i} de 15, pista 2 · archivo 3 de 7" for i in range(1, 16)]


def test_detector_muestra_unica_tambien_informa(monkeypatch, tmp_path):
    _muestras_falsas(monkeypatch, tmp_path)
    detalles = []
    monkeypatch.setattr(detector_module.progreso, "actualizar",
                        lambda etapa, posicion_s=None, detalle=None: detalles.append((etapa, detalle)))
    detector_module.detect_language_with_multiple_samples(
        object(), "/media/corto.mkv", 1, 40.0, contexto="archivo 1 de 1")
    assert detalles == [("idiomas", "muestra 1 de 1, pista 1 · archivo 1 de 1")]


def test_detector_sin_contexto_no_toca_el_avance(monkeypatch, tmp_path):
    """Fuera del lote (p. ej. desde el procesador) no hay etapa "idiomas" que actualizar."""
    _muestras_falsas(monkeypatch, tmp_path)
    monkeypatch.setattr(detector_module.progreso, "actualizar",
                        lambda *a, **k: pytest.fail("actualizó el avance sin lote"))
    detector_module.detect_language_with_multiple_samples(object(), "/media/x.mkv", 1, 600.0)
    detector_module.detect_language_with_multiple_samples(object(), "/media/x.mkv", 1, 40.0)


def test_detector_termina_la_etapa_ante_un_error(monkeypatch):
    terminados = []
    monkeypatch.setattr(detector_module.progreso, "terminar", lambda etapa: terminados.append(etapa))
    monkeypatch.setattr(detector_module, "load_whisper_model", lambda: object())
    monkeypatch.setattr(detector_module, "get_cached_language", lambda *a: None)

    def falla(*a, **k):
        raise RuntimeError("whisper se cayó")

    monkeypatch.setattr(detector_module, "detect_language_with_multiple_samples", falla)
    with pytest.raises(RuntimeError):
        detector_module.process_files(
            [{"absolute_path": "/media/a.mkv", "relative_path": "a.mkv", "streams": [(1, "aac", 9.0)]}], {}
        )
    assert terminados == ["idiomas"]


# --- Whisper ----------------------------------------------------------------

class _Extractor(whisper_module.WhisperAudioExtractorMixin):
    def __init__(self, tmp_path, segmentos, duracion=100.0):
        self.logger = _Logger()
        self.tmp_dir = tmp_path
        self._transcription_video = Path("/media/Serie S01E01.mkv")
        info = SimpleNamespace(language="en", duration=duracion)
        self._faster_whisper_model = SimpleNamespace(transcribe=lambda *a, **k: (segmentos, info))


def _segmentos(n):
    for i in range(1, n + 1):
        yield SimpleNamespace(start=i - 1.0, end=float(i), text=f"frase {i}", words=None)


def _registrar_whisper(monkeypatch):
    eventos = []
    monkeypatch.setattr(whisper_module.progreso, "iniciar",
                        lambda etapa, archivo, duracion_s=None, detalle="": eventos.append(
                            ("iniciar", etapa, archivo, duracion_s, detalle)))
    monkeypatch.setattr(whisper_module.progreso, "actualizar",
                        lambda etapa, posicion_s=None, detalle=None: eventos.append(("actualizar", etapa, posicion_s)))
    monkeypatch.setattr(whisper_module.progreso, "terminar", lambda etapa: eventos.append(("terminar", etapa)))
    return eventos


def test_whisper_actualiza_como_mucho_cada_5_segundos(monkeypatch, tmp_path):
    eventos = _registrar_whisper(monkeypatch)
    reloj = iter(range(0, 1000))  # cada consulta avanza un segundo
    monkeypatch.setattr(whisper_module.time, "monotonic", lambda: float(next(reloj)))

    resultado = _Extractor(tmp_path, _segmentos(20))._transcribe_with_faster_whisper(tmp_path / "a.wav")

    assert len(resultado["segments"]) == 20
    assert eventos[0] == ("iniciar", "subtitulos", "Serie S01E01.mkv", 100.0, "Serie S01E01.mkv")
    assert eventos[-1] == ("terminar", "subtitulos")
    actualizaciones = [e for e in eventos if e[0] == "actualizar"]
    assert 2 <= len(actualizaciones) <= 4
    posiciones = [e[2] for e in actualizaciones]
    assert all(b - a >= 5 for a, b in zip(posiciones, posiciones[1:]))


def test_whisper_termina_la_etapa_ante_un_error(monkeypatch, tmp_path):
    eventos = _registrar_whisper(monkeypatch)

    def roto():
        yield SimpleNamespace(start=0.0, end=1.0, text="hola", words=None)
        raise RuntimeError("modelo caído")

    with pytest.raises(RuntimeError):
        _Extractor(tmp_path, roto())._transcribe_with_faster_whisper(tmp_path / "a.wav")
    assert eventos[-1] == ("terminar", "subtitulos")


def test_whisper_sin_video_conocido_usa_el_audio(monkeypatch, tmp_path):
    eventos = _registrar_whisper(monkeypatch)
    extractor = _Extractor(tmp_path, _segmentos(1))
    del extractor._transcription_video
    extractor._transcribe_with_faster_whisper(tmp_path / "a.wav")
    assert eventos[0][2] == "a.wav"


def test_whisper_late_entre_segmentos_lentos(monkeypatch, tmp_path):
    """Si un segmento tarda más que el latido, el avance se toca igual (sin cambiar la posición)."""
    import threading
    eventos = []
    latio = threading.Event()

    def actualizar(etapa, posicion_s=None, detalle=None):
        eventos.append(("actualizar", etapa, posicion_s))
        if posicion_s is None:
            latio.set()

    monkeypatch.setattr(whisper_module.progreso, "iniciar", lambda *a, **k: eventos.append(("iniciar",)))
    monkeypatch.setattr(whisper_module.progreso, "actualizar", actualizar)
    monkeypatch.setattr(whisper_module.progreso, "terminar", lambda etapa: eventos.append(("terminar", etapa)))
    monkeypatch.setattr(whisper_module, "PROGRESO_LATIDO_SEGUNDOS", 0.02, raising=False)

    def lentos():
        yield SimpleNamespace(start=0.0, end=1.0, text="hola", words=None)
        assert latio.wait(5), "no hubo latido mientras el segmento tardaba"
        yield SimpleNamespace(start=1.0, end=2.0, text="chau", words=None)

    resultado = _Extractor(tmp_path, lentos())._transcribe_with_faster_whisper(tmp_path / "a.wav")
    assert len(resultado["segments"]) == 2
    assert ("actualizar", "subtitulos", None) in eventos
    # el latido se detiene antes de cerrar la etapa: nada la revive después
    fin = eventos.index(("terminar", "subtitulos"))
    import time as _t
    _t.sleep(0.1)
    assert len(eventos) == fin + 1
