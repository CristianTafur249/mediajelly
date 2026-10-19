"""Política de pistas: qué audio y qué subtítulos conserva el procesador.

La primera parte son tests de caracterización: se escribieron contra el
procesador ANTES de extraer la lógica a ``mediajelly_politica`` y fijan su
comportamiento. La segunda parte prueba las funciones puras con la misma tabla
de casos, para que el panel muestre exactamente lo que hará el procesador.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import mediajelly_processor as processor_module  # noqa: E402
from mediajelly_processor import MediaJellyProcessor, ProcessingStats  # noqa: E402


# ---------------------------------------------------------------------------
# Tablas de casos (entradas y salidas obtenidas del procesador actual)
# ---------------------------------------------------------------------------

# (id, audio_languages, audio_espanol, subs_espanol, es_anime, grande_o_uhd,
#  audio esperado, idiomas esperados, subtítulos esperados, omitir esperado)
#
# Regla acordada: todo el español, en su orden; si no hay, anime -> japonés,
# si no inglés; el resto -> inglés; si no, la pista 0. Sin español se conserva
# una sola pista. La pista conservada lleva su idioma si se conoce; si no
# (sin dato o "und"), no se fuerza ninguno.
CASOS_PLAN = [
    ("anime_spa_jpn", {0: "jpn", 1: "spa"}, [1], [0], True, False, [1], {1: "spa"}, [0], False),
    ("anime_sin_espanol_jpn", {0: "eng", 1: "jpn"}, [], [], True, False, [1], {1: "jpn"}, [], False),
    ("anime_sin_jpn_eng", {0: "fre", 1: "eng"}, [], [], True, False, [1], {1: "eng"}, [], False),
    ("anime_sin_jpn_ni_eng", {0: "fre", 1: "ger"}, [], [], True, False, [0], {0: "fre"}, [], False),
    ("anime_dos_jpn_gana_la_primera", {0: "eng", 1: "jpn", 2: "jpn"}, [], [], True, False, [1], {1: "jpn"}, [], False),
    ("serie_jpn_eng", {0: "jpn", 1: "eng"}, [], [], False, False, [1], {1: "eng"}, [], False),
    ("serie_sin_eng_pista_0", {0: "fre", 1: "jpn"}, [], [], False, False, [0], {0: "fre"}, [], False),
    ("serie_dos_spa", {0: "spa", 1: "spa", 2: "eng"}, [0, 1], [], False, False, [0, 1], {0: "spa", 1: "spa"}, [], False),
    ("anime_spa_y_lat", {0: "jpn", 1: "spa", 2: "es-419"}, [1, 2], [], True, False, [1, 2], {1: "spa", 2: "spa"}, [], False),
    ("espanol_en_su_orden", {0: "eng", 1: "lat", 2: "spa"}, [1, 2], [], False, False, [1, 2], {1: "spa", 2: "spa"}, [], False),
    ("serie_sin_espanol", {0: "eng", 1: "fre"}, [], [], False, False, [0], {0: "eng"}, [], False),
    ("sin_idiomas", {}, [], [2], False, False, [0], {}, [2], False),
    ("pista_0_und", {0: "und", 1: "fre"}, [], [], False, False, [0], {}, [], False),
    ("archivo_grande", {0: "spa"}, [0], [0, 1], False, True, [0], {0: "spa"}, [], True),
    ("grande_sin_subs", {0: "spa"}, [0], [], False, True, [0], {0: "spa"}, [], True),
    ("mayusculas", {0: "fre", 1: "English"}, [], [], False, False, [1], {1: "eng"}, [], False),
    ("dos_letras_anime", {0: "en", 1: "ja"}, [], [], True, False, [1], {1: "jpn"}, [], False),
    ("dos_letras_serie", {0: "fre", 1: "en-us"}, [], [], False, False, [1], {1: "eng"}, [], False),
    ("est_no_es_espanol_ni_ingles", {0: "est", 1: "ces"}, [], [], False, False, [0], {0: "est"}, [], False),
    ("idioma_ausente", {1: "fre"}, [], [], False, False, [0], {}, [], False),
    ("castellano", {0: "Castellano", 1: "eng"}, [0], [], False, False, [0], {0: "spa"}, [], False),
    ("japanese_completo", {0: "english", 1: "japanese"}, [], [], True, False, [1], {1: "jpn"}, [], False),
    ("desconocido_no_se_fuerza", {0: "xx-yy", 1: "fre"}, [], [], False, False, [0], {}, [], False),
    ("dos_letras_otro_idioma", {0: "fr-ca"}, [], [], False, False, [0], {0: "fra"}, [], False),
    ("ninguno_conocido", {0: "kor", 1: "chi"}, [], [1], True, False, [0], {0: "kor"}, [1], False),
]

# (id, subs_espanol, códecs de subtítulos que devuelve ffprobe, pistas esperadas)
CASOS_SUBS = [
    ("pgs_mezclado_con_texto", [0, 1], ["subrip", "hdmv_pgs_subtitle"], [0]),
    ("espanol_ass_tras_pgs", [1], ["hdmv_pgs_subtitle", "ass"], [1]),
    ("espanol_solo_pgs_no_cae_al_texto", [0], ["hdmv_pgs_subtitle", "subrip"], []),
    ("sin_espanol_primer_texto", [], ["hdmv_pgs_subtitle", "webvtt", "subrip"], [1]),
    ("sin_espanol_solo_pgs", [], ["hdmv_pgs_subtitle", "dvd_subtitle"], []),
    ("sin_subtitulos", [], [], []),
    ("indice_fuera_de_rango", [3], ["subrip"], []),
    ("codec_en_mayusculas", [0], ["SubRip"], [0]),
    ("orden_preservado", [1, 0], ["ass", "srt"], [1, 0]),
    ("mov_text_y_ssa", [0, 2], ["mov_text", "hdmv_pgs_subtitle", "ssa"], [0, 2]),
]


class _Logger:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _processor(tmp_path) -> MediaJellyProcessor:
    proc = MediaJellyProcessor.__new__(MediaJellyProcessor)
    proc.logger = _Logger()
    proc.no_spanish_logger = _Logger()
    proc.tmp_dir = tmp_path
    proc.completed_file = tmp_path / "completed.txt"
    proc.failed_file = tmp_path / "failed-compression.txt"
    proc.stats = ProcessingStats()
    proc.processed_files = {}
    proc.config = None
    return proc


def _ids(casos):
    return [c[0] for c in casos]


# ---------------------------------------------------------------------------
# Caracterización: _prepare_compression
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("modo_grande", ["tamano", "uhd"])
@pytest.mark.parametrize("caso", CASOS_PLAN, ids=[f"p{i}" for i in range(len(CASOS_PLAN))])
def test_prepare_compression_conserva_la_seleccion(tmp_path, caso, modo_grande):
    _, langs, audio_esp, subs_esp, anime, grande, exp_audio, exp_langs, exp_subs, _ = caso
    # Los ids son neutros para que la ruta temporal no contenga "anime".
    assert "anime" not in str(tmp_path).lower()

    carpeta = tmp_path / ("Anime" if anime else "Series")
    carpeta.mkdir()
    video = carpeta / "Episodio S01E01.mkv"
    resolution = None
    if grande and modo_grande == "tamano":
        with open(video, "wb") as f:
            f.truncate(5 * 1024**3 + 1)  # archivo disperso: no ocupa disco
    else:
        video.write_bytes(b"x" * 1024)
        if grande:
            resolution = {"width": 3840, "height": 2160}

    proc = _processor(tmp_path)
    proc._validate_file_for_compression = lambda p: None
    proc.detect_language_streams = lambda p, allow_whisper_fallback=True: (
        bool(audio_esp),
        list(audio_esp),
        list(subs_esp),
        dict(langs),
    )
    proc._get_video_codec = lambda p: "av1"
    proc._should_use_gpu_compression = lambda p, resolution=None: False

    ok, _info, _temp, audio, subs, idiomas = proc._prepare_compression(video, resolution=resolution)

    assert ok is True
    assert audio == exp_audio
    assert idiomas == exp_langs
    assert subs == exp_subs


# ---------------------------------------------------------------------------
# Caracterización: subtítulos en los constructores de ffmpeg
# ---------------------------------------------------------------------------


def _ffprobe_falso(codecs):
    def run(cmd, *args, **kwargs):
        if "-select_streams" in cmd and cmd[cmd.index("-select_streams") + 1] == "s":
            salida = "".join(f"{c}\n" for c in codecs)
        else:
            salida = "aac\n2\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=salida, stderr="")

    return run


def _subs_mapeados(cmd):
    mapeados = []
    for i, arg in enumerate(cmd[:-1]):
        if arg == "-map":
            m = re.fullmatch(r"0:s:(\d+)\?", cmd[i + 1])
            if m:
                mapeados.append(int(m.group(1)))
    return mapeados


def _tiene_mov_text(cmd):
    """La salida lleva subtítulos mov_text: convertidos (-c:s mov_text) o copiados
    porque ya lo eran (-c:s:K copy)."""
    return any(re.fullmatch(r"-c:s(:\d+)?", cmd[i]) and cmd[i + 1] in ("mov_text", "copy")
               for i in range(len(cmd) - 1))


CONSTRUCTORES = ["_build_remux_command", "_build_ffmpeg_command", "_build_fallback_ffmpeg_command"]


@pytest.mark.parametrize("constructor", CONSTRUCTORES)
@pytest.mark.parametrize("caso", CASOS_SUBS, ids=_ids(CASOS_SUBS))
def test_constructores_mapean_los_mismos_subtitulos(tmp_path, monkeypatch, caso, constructor):
    _, subs_esp, codecs, esperado = caso
    monkeypatch.setattr(processor_module.subprocess, "run", _ffprobe_falso(codecs))
    proc = _processor(tmp_path)

    cmd = getattr(proc, constructor)(
        tmp_path / "entrada.mkv", tmp_path / "salida.mp4", [0], list(subs_esp), {0: "spa"}
    )

    assert _subs_mapeados(cmd) == esperado
    assert _tiene_mov_text(cmd) is bool(esperado)


@pytest.mark.parametrize("constructor", CONSTRUCTORES)
def test_constructores_sin_subtitulos_si_ffprobe_falla(tmp_path, monkeypatch, constructor):
    def run(cmd, *args, **kwargs):
        if "-select_streams" in cmd and cmd[cmd.index("-select_streams") + 1] == "s":
            raise subprocess.TimeoutExpired(cmd, 1)
        return subprocess.CompletedProcess(cmd, 0, stdout="aac\n2\n", stderr="")

    monkeypatch.setattr(processor_module.subprocess, "run", run)
    proc = _processor(tmp_path)

    cmd = getattr(proc, constructor)(tmp_path / "e.mkv", tmp_path / "s.mp4", [0], [0], {0: "spa"})

    assert _subs_mapeados(cmd) == []
    assert not _tiene_mov_text(cmd)


# ---------------------------------------------------------------------------
# Funciones puras de mediajelly_politica (misma tabla de casos)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("caso", CASOS_PLAN, ids=_ids(CASOS_PLAN))
def test_planificar_pistas(caso):
    from mediajelly_politica import PlanPistas, planificar_pistas

    _, langs, audio_esp, subs_esp, anime, grande, exp_audio, exp_langs, exp_subs, exp_omitir = caso

    plan = planificar_pistas(dict(langs), list(audio_esp), list(subs_esp), anime, grande)

    # nombres_audio (variante regional) se prueba aparte: test_nombre_pista
    plan.nombres_audio = {}
    assert plan == PlanPistas(
        audio=exp_audio,
        idiomas_audio=exp_langs,
        subtitulos_espanol=exp_subs,
        omitir_subtitulos=exp_omitir,
    )


def test_planificar_pistas_no_modifica_las_entradas():
    from mediajelly_politica import planificar_pistas

    langs, audio, subs = {0: "eng", 1: "spa"}, [0, 1], [0]
    planificar_pistas(langs, audio, subs, False, True)

    assert (langs, audio, subs) == ({0: "eng", 1: "spa"}, [0, 1], [0])


@pytest.mark.parametrize("caso", CASOS_SUBS, ids=_ids(CASOS_SUBS))
def test_elegir_subtitulos(caso):
    from mediajelly_politica import elegir_subtitulos

    _, subs_esp, codecs, esperado = caso

    assert elegir_subtitulos(list(subs_esp), list(codecs)) == esperado


def test_elegir_subtitulos_ignora_codecs_desconocidos():
    from mediajelly_politica import elegir_subtitulos

    assert elegir_subtitulos([], [None, "", "ass"]) == [2]
    assert elegir_subtitulos([0], [None]) == []


def test_codecs_de_texto():
    from mediajelly_politica import TEXT_SUB_CODECS

    assert TEXT_SUB_CODECS == {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text"}


@pytest.mark.parametrize(
    "ruta, esperado",
    [
        ("/media/Anime/Show/S01E01.mkv", True),
        ("/media/ANIME/x.mkv", True),
        ("/media/series_anime_jp/x.mkv", True),
        ("/media/Series/Show/S01E01.mkv", False),
        ("/media/Películas/Anim.mkv", False),
    ],
)
def test_es_anime(ruta, esperado):
    from mediajelly_politica import es_anime

    assert es_anime(Path(ruta)) is esperado


# ---------------------------------------------------------------------------
# omitir_subtitulos: archivo de más de 5 GiB o UHD (mismos umbrales que el procesador)
# ---------------------------------------------------------------------------

CINCO_GIB = 5 * 1024**3


@pytest.mark.parametrize(
    "tamano, ancho, alto, esperado",
    [
        (CINCO_GIB, 1920, 1080, False),      # justo en el umbral: no es "más de 5 GiB"
        (CINCO_GIB + 1, 1920, 1080, True),   # un byte más
        (CINCO_GIB - 1, 0, 0, False),
        (1024, 3800, 1080, True),            # borde UHD por ancho (>=)
        (1024, 3799, 1080, False),
        (1024, 1920, 2000, True),            # borde UHD por alto (>=)
        (1024, 1920, 1999, False),
        (1024, 3840, 2160, True),
        (1024, 0, 0, False),                 # sin resolución (ffprobe falló)
        (1024, None, None, False),
    ],
)
def test_omitir_subtitulos(tamano, ancho, alto, esperado):
    from mediajelly_politica import omitir_subtitulos

    assert omitir_subtitulos(tamano, ancho, alto) is esperado


def test_umbrales_uhd_los_comparte_el_procesador():
    import mediajelly_politica as politica

    assert politica.UHD_WIDTH_THRESHOLD == 3800
    assert politica.UHD_HEIGHT_THRESHOLD == 2000
    assert politica.UMBRAL_ARCHIVO_GRANDE == CINCO_GIB
    assert processor_module.UHD_WIDTH_THRESHOLD == politica.UHD_WIDTH_THRESHOLD
    assert processor_module.UHD_HEIGHT_THRESHOLD == politica.UHD_HEIGHT_THRESHOLD


@pytest.mark.parametrize(
    "tamano, resolution, omitir",
    [
        (CINCO_GIB, None, False),
        (CINCO_GIB + 1, None, True),
        (1024, {"width": 3800, "height": 1600}, True),
        (1024, {"width": 3799, "height": 1999}, False),
        (1024, {"width": 1920, "height": 2000}, True),
    ],
    ids=["5gib_justo", "5gib_mas_1", "uhd_ancho", "casi_uhd", "uhd_alto"],
)
def test_prepare_compression_borde_grande_o_uhd(tmp_path, tamano, resolution, omitir):
    """El procesador decide con omitir_subtitulos: se ve en si conserva los subtítulos."""
    video = tmp_path / "Series" / "Episodio S01E01.mkv"
    video.parent.mkdir()
    with open(video, "wb") as f:
        f.truncate(tamano)  # archivo disperso: no ocupa disco

    proc = _processor(tmp_path)
    proc._validate_file_for_compression = lambda p: None
    proc.detect_language_streams = lambda p, allow_whisper_fallback=True: (True, [0], [0], {0: "spa"})
    proc._get_video_codec = lambda p: "av1"
    proc._should_use_gpu_compression = lambda p, resolution=None: False

    ok, _info, _temp, _audio, subs, _idiomas = proc._prepare_compression(video, resolution=resolution)

    assert ok is True
    assert subs == ([] if omitir else [0])


# ---------------------------------------------------------------------------
# Fallo 3: "español" por código exacto o palabra completa, no por subcadena
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "idioma, esperado",
    [
        ("spa", True), ("es", True), ("esp", True), ("lat", True),
        ("es-419", True), ("es-es", True), ("es-mx", True), ("es_ES", True), ("spa-lat", True),
        ("SPA", True), ("Spanish", True), ("español", True), ("espanol", True),
        ("castellano", True), ("latino", True), ("Español Latino", True),
        ("ces", False), ("est", False), ("rest", False), ("test", False), ("best", False),
        ("eng", False), ("jpn", False), ("fre", False), ("und", False), ("", False),
        ("chinese", False), ("estonian", False), ("lavender", False),
    ],
)
def test_es_espanol(idioma, esperado):
    from mediajelly_politica import es_espanol

    assert es_espanol(idioma) is esperado


@pytest.mark.parametrize(
    "texto, esperado",
    [
        ("aac,Spanish 5.1,SoundHandler", "spa"),
        ("ac3,Español Latino", "spa"),
        ("aac,Castellano", "spa"),
        ("aac,spa", "spa"),
        ("aac,English,SoundHandler", "eng"),
        ("aac,Inglés", "eng"),
        ("aac,Japanese", "jpn"),
        ("aac,japonés", "jpn"),
        ("aac,JAP", "jpn"),
        ("aac,Rest of world,SoundHandler", None),
        ("aac,Best quality", None),
        ("aac,test", None),
        ("aac,Platinum edition", None),
        ("aac,Strength", None),
        ("aac,", None),
        ("", None),
    ],
)
def test_idioma_por_metadatos(texto, esperado):
    from mediajelly_politica import idioma_por_metadatos

    assert idioma_por_metadatos(texto) == esperado


def _procesador_con_ffprobe(tmp_path, monkeypatch, salida):
    import mediajelly_language_detection as deteccion

    def run(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=salida, stderr="")

    monkeypatch.setattr(deteccion.subprocess, "run", run)
    return _processor(tmp_path)


def test_detect_language_streams_sin_falsos_positivos(tmp_path, monkeypatch):
    salida = (
        "0,video,\n"
        "1,audio,ces\n"
        "2,audio,est\n"
        "3,audio,spa\n"
        "4,audio,es-419\n"
        "5,subtitle,est\n"
        "6,subtitle,ces\n"
        "7,subtitle,es\n"
    )
    proc = _procesador_con_ffprobe(tmp_path, monkeypatch, salida)

    hay, audio, subs, idiomas = proc.detect_language_streams(tmp_path / "x.mkv", allow_whisper_fallback=False)

    assert hay is True
    assert audio == [2, 3]
    assert subs == [2]
    assert idiomas == {0: "ces", 1: "est", 2: "spa", 3: "es-419"}


def test_detect_language_streams_checo_solo_no_es_espanol(tmp_path, monkeypatch):
    proc = _procesador_con_ffprobe(tmp_path, monkeypatch, "0,video,\n1,audio,ces\n2,subtitle,est\n")

    hay, audio, subs, _ = proc.detect_language_streams(tmp_path / "x.mkv", allow_whisper_fallback=False)

    assert (hay, audio, subs) == (False, [], [])


# ---------------------------------------------------------------------------
# Fallo 2: archivo grande/UHD no conserva ningún subtítulo incrustado
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("caso", CASOS_SUBS, ids=_ids(CASOS_SUBS))
def test_elegir_subtitulos_omitir(caso):
    from mediajelly_politica import elegir_subtitulos

    _, subs_esp, codecs, _ = caso

    assert elegir_subtitulos(list(subs_esp), list(codecs), omitir=True) == []


@pytest.mark.parametrize("constructor", CONSTRUCTORES)
def test_constructores_no_mapean_subtitulos_si_se_omiten(tmp_path, monkeypatch, constructor):
    monkeypatch.setattr(processor_module.subprocess, "run", _ffprobe_falso(["subrip", "ass"]))
    proc = _processor(tmp_path)

    cmd = getattr(proc, constructor)(
        tmp_path / "e.mkv", tmp_path / "s.mp4", [0], [], {0: "spa"}, omitir_subtitulos=True
    )

    assert _subs_mapeados(cmd) == []
    assert not _tiene_mov_text(cmd)


def test_prepare_compression_informa_omitir_subtitulos(tmp_path):
    video = tmp_path / "Series" / "Episodio S01E01.mkv"
    video.parent.mkdir()
    video.write_bytes(b"x" * 1024)
    proc = _processor(tmp_path)
    proc._validate_file_for_compression = lambda p: None
    proc.detect_language_streams = lambda p, allow_whisper_fallback=True: (True, [0], [0], {0: "spa"})
    proc._get_video_codec = lambda p: "av1"

    _ok, info_uhd, *_ = proc._prepare_compression(video, resolution={"width": 3840, "height": 2160})
    _ok, info_hd, *_ = proc._prepare_compression(video, resolution={"width": 1920, "height": 1080})

    assert info_uhd["omitir_subtitulos"] is True
    assert info_hd["omitir_subtitulos"] is False


@pytest.mark.parametrize(
    "use_gpu, use_remux, constructor",
    [(False, True, "_build_remux_command"), (True, False, "_build_ffmpeg_command"),
     (False, False, "_build_fallback_ffmpeg_command")],
)
def test_execute_pasa_omitir_subtitulos_al_constructor(tmp_path, monkeypatch, use_gpu, use_remux, constructor):
    proc = _processor(tmp_path)
    vistos = {}

    def falso(*args, **kwargs):
        vistos.update(kwargs)
        return ["ffmpeg", str(tmp_path / "s.mp4")]

    monkeypatch.setattr(proc, constructor, falso)
    monkeypatch.setattr(proc, "_progreso_disponible", lambda: False)
    monkeypatch.setattr(
        proc, "_run_ffmpeg_con_progreso",
        lambda cmd, etapa, usar: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )

    proc._execute_compression_attempt(
        tmp_path / "e.mkv", tmp_path / "s.mp4", use_gpu, [0], [], use_remux, {0: "spa"},
        omitir_subtitulos=True,
    )

    assert vistos.get("omitir_subtitulos") is True


def test_fallback_conserva_omitir_subtitulos(tmp_path, monkeypatch):
    proc = _processor(tmp_path)
    llamadas = []

    def intento(*args, **kwargs):
        llamadas.append(kwargs.get("omitir_subtitulos"))
        return subprocess.CompletedProcess([], 1, stdout="", stderr="falló"), 0.0

    monkeypatch.setattr(proc, "_execute_compression_attempt", intento)
    monkeypatch.setattr(proc, "_process_compression_result", lambda *a, **k: {"success": False})

    proc._handle_fallback_logic(
        {"success": False}, None, tmp_path / "e.mkv", tmp_path / "s.mp4", True, [0], [],
        {0: "spa"}, use_remux=False, omitir_subtitulos=True,
    )

    assert llamadas and all(v is True for v in llamadas)


# ---------------------------------------------------------------------------
# Fallo 1: varias pistas de audio en los constructores
# ---------------------------------------------------------------------------


def _ffprobe_audio(codecs_audio, codecs_subs=()):
    """ffprobe falso: '-select_streams a' lista index y códec de cada pista de audio
    (el stream 0 es el video). Los canales se ignoran: el remux no los usa."""
    def run(cmd, *args, **kwargs):
        sel = cmd[cmd.index("-select_streams") + 1] if "-select_streams" in cmd else None
        if sel == "s":
            salida = "".join(f"{c}\n" for c in codecs_subs)
        elif sel == "a":
            salida = "".join(f"{i},{c}\n" for i, (c, _ch) in enumerate(codecs_audio, start=1))
        else:
            salida = ""
        return subprocess.CompletedProcess(cmd, 0, stdout=salida, stderr="")

    return run


def _audios_mapeados(cmd):
    mapas, idiomas = [], {}
    for i, arg in enumerate(cmd[:-1]):
        if arg == "-map":
            m = re.fullmatch(r"0:a:(\d+)", cmd[i + 1])
            if m:
                mapas.append(int(m.group(1)))
        m = re.fullmatch(r"-metadata:s:a:(\d+)", arg)
        if m and cmd[i + 1].startswith("language="):
            idiomas[int(m.group(1))] = cmd[i + 1]
    return mapas, idiomas


def _nombres_mapeados(cmd):
    """Salida -> handler_name que escribe el comando (el nombre de pista que guarda el MP4)."""
    nombres = {}
    for i, arg in enumerate(cmd[:-1]):
        m = re.fullmatch(r"-metadata:s:a:(\d+)", arg)
        if m and cmd[i + 1].startswith("handler_name="):
            nombres[int(m.group(1))] = cmd[i + 1][len("handler_name="):]
    return nombres


def _opcion(cmd, nombre):
    return cmd[cmd.index(nombre) + 1] if nombre in cmd else None


@pytest.mark.parametrize("constructor", CONSTRUCTORES)
def test_constructores_mapean_varias_pistas_con_su_idioma(tmp_path, monkeypatch, constructor):
    monkeypatch.setattr(processor_module.subprocess, "run", _ffprobe_audio([("ac3", 6), ("ac3", 6), ("aac", 2)]))
    proc = _processor(tmp_path)

    cmd = getattr(proc, constructor)(tmp_path / "e.mkv", tmp_path / "s.mp4", [0, 1], [], {0: "spa", 1: "spa"})

    assert _audios_mapeados(cmd) == ([0, 1], {0: "language=spa", 1: "language=spa"})
    assert _opcion(cmd, "-c:a") == "aac"  # un solo -c:a: se aplica a todas las pistas
    assert cmd.count("-c:a") == 1


@pytest.mark.parametrize("constructor", CONSTRUCTORES)
def test_constructores_sin_idioma_conocido_no_lo_fuerzan(tmp_path, monkeypatch, constructor):
    monkeypatch.setattr(processor_module.subprocess, "run", _ffprobe_audio([("aac", 2)]))
    proc = _processor(tmp_path)

    cmd = getattr(proc, constructor)(tmp_path / "e.mkv", tmp_path / "s.mp4", [0], [], {})

    assert _audios_mapeados(cmd) == ([0], {})


def test_remux_copia_audio_solo_si_todas_las_pistas_son_aac(tmp_path, monkeypatch):
    proc = _processor(tmp_path)

    monkeypatch.setattr(processor_module.subprocess, "run", _ffprobe_audio([("aac", 2), ("ac3", 6)]))
    mezcla = proc._build_remux_command(tmp_path / "e.mkv", tmp_path / "s.mp4", [0, 1], [], {0: "spa", 1: "spa"})
    monkeypatch.setattr(processor_module.subprocess, "run", _ffprobe_audio([("aac", 2), ("ac3", 6), ("aac", 2)]))
    todas_aac = proc._build_remux_command(tmp_path / "e.mkv", tmp_path / "s.mp4", [0, 2], [], {0: "spa", 2: "spa"})

    assert _opcion(mezcla, "-c:a") == "aac"
    assert _opcion(todas_aac, "-c:a") == "copy"


# ---------------------------------------------------------------------------
# Punta a punta con ffmpeg real: detección -> plan -> comando -> archivo final
# ---------------------------------------------------------------------------

requiere_ffmpeg = pytest.mark.skipif(
    not (__import__("shutil").which("ffmpeg") and __import__("shutil").which("ffprobe")),
    reason="sin ffmpeg/ffprobe",
)


def _video_lavfi(ruta: Path, audios):
    """Video corto: stream 0 de video y una pista de audio por cada (idioma, título)."""
    ruta.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=1"]
    for i, _ in enumerate(audios):
        cmd += ["-f", "lavfi", "-i", f"sine=frequency={440 + 110 * i}:duration=1"]
    for i in range(1 + len(audios)):
        cmd += ["-map", str(i)]
    cmd += ["-c:v", "mpeg4", "-c:a", "aac"]
    for i, (lang, titulo) in enumerate(audios):
        if lang:
            cmd += [f"-metadata:s:a:{i}", f"language={lang}"]
        if titulo:
            cmd += [f"-metadata:s:a:{i}", f"title={titulo}"]
    cmd.append(str(ruta))
    subprocess.run(cmd, check=True, capture_output=True, timeout=60)
    return ruta


def _idiomas_audio_de(ruta: Path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream_tags=language",
         "-of", "csv=p=0", str(ruta)],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return [l.strip() for l in r.stdout.splitlines()]


def _comprimir_de_verdad(tmp_path, ruta: Path):
    proc = _processor(tmp_path)
    proc._validate_file_for_compression = lambda p: None
    proc._should_use_gpu_compression = lambda p, resolution=None: False

    ok, info, temp, audio, subs, idiomas = proc._prepare_compression(ruta, resolution={"width": 160, "height": 120})
    assert ok is True and info["use_remux"] is False and info["use_gpu"] is False
    salida = tmp_path / "salida.mp4"
    cmd = proc._build_fallback_ffmpeg_command(
        ruta, salida, audio, subs, idiomas, omitir_subtitulos=info["omitir_subtitulos"]
    )
    subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    return cmd, salida


@requiere_ffmpeg
def test_punta_a_punta_anime_eng_jpn_conserva_solo_jpn(tmp_path):
    ruta = _video_lavfi(tmp_path / "Anime" / "Show S01E01.mkv", [("eng", None), ("jpn", None)])

    cmd, salida = _comprimir_de_verdad(tmp_path, ruta)

    assert _audios_mapeados(cmd) == ([1], {0: "language=jpn"})
    assert _idiomas_audio_de(salida) == ["jpn"]


@requiere_ffmpeg
def test_punta_a_punta_serie_spa_spa_eng_conserva_las_dos_spa(tmp_path):
    ruta = _video_lavfi(tmp_path / "Series" / "Show S01E01.mkv", [("spa", None), ("spa", None), ("eng", None)])

    cmd, salida = _comprimir_de_verdad(tmp_path, ruta)

    assert _audios_mapeados(cmd) == ([0, 1], {0: "language=spa", 1: "language=spa"})
    assert _idiomas_audio_de(salida) == ["spa", "spa"]


# ---------------------------------------------------------------------------
# Fallo 4: los metadatos de una pista se leen con su índice absoluto
# ---------------------------------------------------------------------------


@requiere_ffmpeg
def test_detect_audio_language_usa_el_indice_absoluto(tmp_path):
    # Stream 0 = video, 1 = audio "English", 2 = audio "Japanese"; sin etiqueta de idioma.
    ruta = _video_lavfi(tmp_path / "Series" / "Show S01E01.mkv", [(None, "English"), (None, "Japanese")])
    proc = _processor(tmp_path)

    assert proc.detect_audio_language(ruta, 1, allow_whisper_fallback=False) == "eng"
    assert proc.detect_audio_language(ruta, 2, allow_whisper_fallback=False) == "jpn"


# ---------------------------------------------------------------------------
# Ronda 1: idioma ISO 639-2, ffprobe de audio alineado, AAC estéreo, NFC
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "idioma, esperado",
    [
        ("spa", "spa"), ("es-419", "spa"), ("es-mx", "spa"), ("Castellano", "spa"), ("lat", "spa"),
        ("en-us", "eng"), ("English", "eng"), ("japanese", "jpn"), ("jap", "jpn"), ("ja", "jpn"),
        ("fre", "fre"), ("kor", "kor"), ("fr", "fra"), ("de-at", "deu"),
        ("xx-yy", None), ("xx", None), ("und", None), ("", None), (None, None), ("francais", None),
    ],
)
def test_idioma_iso(idioma, esperado):
    from mediajelly_politica import idioma_iso

    assert idioma_iso(idioma) == esperado


@pytest.mark.parametrize("constructor", CONSTRUCTORES)
def test_constructores_escriben_idioma_iso(tmp_path, monkeypatch, constructor):
    from mediajelly_politica import planificar_pistas

    monkeypatch.setattr(processor_module.subprocess, "run", _ffprobe_audio([("aac", 2), ("aac", 2)]))
    proc = _processor(tmp_path)
    plan = planificar_pistas({0: "spa", 1: "es-419"}, [0, 1], [], False, False)

    cmd = getattr(proc, constructor)(tmp_path / "e.mkv", tmp_path / "s.mp4", plan.audio, [], plan.idiomas_audio)

    assert _audios_mapeados(cmd) == ([0, 1], {0: "language=spa", 1: "language=spa"})


@pytest.mark.parametrize("constructor", CONSTRUCTORES)
def test_constructores_idioma_desconocido_sin_metadata(tmp_path, monkeypatch, constructor):
    from mediajelly_politica import planificar_pistas

    monkeypatch.setattr(processor_module.subprocess, "run", _ffprobe_audio([("aac", 2)]))
    proc = _processor(tmp_path)
    plan = planificar_pistas({0: "xx-yy"}, [], [], False, False)

    cmd = getattr(proc, constructor)(tmp_path / "e.mkv", tmp_path / "s.mp4", plan.audio, [], plan.idiomas_audio)

    assert _audios_mapeados(cmd) == ([0], {})


@requiere_ffmpeg
def test_punta_a_punta_es_419_sale_como_spa(tmp_path):
    ruta = _video_lavfi(tmp_path / "Series" / "Show S01E01.mkv", [("eng", None), ("es-419", None)])

    cmd, salida = _comprimir_de_verdad(tmp_path, ruta)

    assert _audios_mapeados(cmd) == ([1], {0: "language=spa"})
    assert _idiomas_audio_de(salida) == ["spa"]


def test_remux_ffprobe_conserva_la_posicion_de_pistas_sin_codec(tmp_path, monkeypatch):
    # La pista 0 no informa códec: la 1 sigue siendo la 1 (AAC) y se copia.
    monkeypatch.setattr(processor_module.subprocess, "run", _ffprobe_audio([("", 2), ("aac", 2)]))
    proc = _processor(tmp_path)

    cmd = proc._build_remux_command(tmp_path / "e.mkv", tmp_path / "s.mp4", [1], [], {1: "spa"})

    assert _opcion(cmd, "-c:a") == "copy"


def test_remux_recodificado_es_aac_estereo_como_gpu_y_cpu(tmp_path, monkeypatch):
    monkeypatch.setattr(processor_module.subprocess, "run", _ffprobe_audio([("ac3", 6), ("aac", 2)]))
    proc = _processor(tmp_path)

    remux = proc._build_remux_command(tmp_path / "e.mkv", tmp_path / "s.mp4", [0, 1], [], {0: "spa", 1: "spa"})
    cpu = proc._build_fallback_ffmpeg_command(tmp_path / "e.mkv", tmp_path / "s.mp4", [0, 1], [], {0: "spa", 1: "spa"})

    def audio(cmd):
        return {k: _opcion(cmd, k) for k in ("-c:a", "-b:a", "-ac", "-ar")}

    assert audio(remux) == {"-c:a": "aac", "-b:a": "256k", "-ac": "2", "-ar": "48000"}
    assert audio(remux) == audio(cpu)


@pytest.mark.parametrize(
    "texto, esperado",
    [("Espan\u0303ol", "spa"), ("Ingle\u0301s", "eng"), ("Japone\u0301s", "jpn")],
)
def test_palabras_con_tildes_combinadas_nfd(texto, esperado):
    from mediajelly_politica import idioma_base, idioma_por_metadatos

    assert idioma_base(texto) == esperado
    assert idioma_por_metadatos("aac," + texto) == esperado


# ---------------------------------------------------------------------------
# Formatos regionales: el MP4 solo guarda el idioma en ISO 639-2 ("spa") y
# descarta el título de la pista; la variante regional va en handler_name.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "idioma, titulo, esperado",
    [
        ("es-419", None, "Español (Latinoamérica)"),
        ("es-MX", None, "Español (Latinoamérica)"),
        ("es_AR", None, "Español (Latinoamérica)"),
        ("lat", None, "Español (Latinoamérica)"),
        ("spa-lat", None, "Español (Latinoamérica)"),
        ("spa", "Latino 5.1", "Español (Latinoamérica)"),
        ("spa", "Spanish (Latin America)", "Español (Latinoamérica)"),
        ("es-ES", None, "Español (España)"),
        ("spa", "Castellano", "Español (España)"),
        ("spa", "Castilian", "Español (España)"),
        ("en-US", None, "English (US)"),
        ("en-GB", None, "English (UK)"),
        ("pt-BR", None, "Português (Brasil)"),
        ("por", "Brazilian", "Português (Brasil)"),
        ("pt-PT", None, "Português (Portugal)"),
        ("fr-CA", None, "Français (Canada)"),
        ("spa", "Español", "Español"),          # sin región: se respeta el título
        ("jpn", "Japanese 2.0", "Japanese 2.0"),
        ("spa", None, None),                     # nada que nombrar
        ("es", "", None),
        ("eng", "SoundHandler", None),           # handler por defecto: no es un nombre
        ("und", None, None),
        ("eng", "Spain commentary", "Spain commentary"),  # otro idioma: el título no da región
        ("jpn", "Latino", "Latino"),
        (None, "Latino", "Español (Latinoamérica)"),        # idioma desconocido: vale el título
        ("spa", "Jason.bourne.lat_fixed50.aac", "Español (Latinoamérica)"),  # nombre de archivo con "lat"
        ("eng", "Jason.bourne.2016.1080p.bluray.x264-sparks-1_track2_eng.aac", None),  # archivo: no es nombre
        ("eng", "Lat Long", "Lat Long"),                    # "lat" solo vale en pistas en español
    ],
)
def test_nombre_pista(idioma, titulo, esperado):
    from mediajelly_politica import nombre_pista

    assert nombre_pista(idioma, titulo) == esperado


def test_plan_lleva_el_nombre_regional_de_las_pistas_conservadas():
    from mediajelly_politica import planificar_pistas

    plan = planificar_pistas(
        {0: "spa", 1: "es-419", 2: "eng"}, [0, 1], [], False, False,
        titulos_audio={0: "Castellano", 2: "English"},
    )

    assert plan.audio == [0, 1]
    assert plan.idiomas_audio == {0: "spa", 1: "spa"}
    assert plan.nombres_audio == {0: "Español (España)", 1: "Español (Latinoamérica)"}


def test_plan_sin_titulos_sigue_funcionando():
    from mediajelly_politica import planificar_pistas

    plan = planificar_pistas({0: "spa"}, [0], [], False, False)

    assert plan.nombres_audio == {}


@pytest.mark.parametrize("constructor", CONSTRUCTORES + ["_build_ffmpeg_command_no_subtitles"])
def test_constructores_escriben_el_nombre_regional(tmp_path, monkeypatch, constructor):
    from mediajelly_politica import planificar_pistas

    monkeypatch.setattr(processor_module.subprocess, "run", _ffprobe_audio([("aac", 2), ("aac", 2)]))
    proc = _processor(tmp_path)
    plan = planificar_pistas({0: "spa", 1: "es-419"}, [0, 1], [], False, False, titulos_audio={0: "Castellano"})
    entrada, salida = tmp_path / "e.mkv", tmp_path / "s.mp4"

    if constructor == "_build_ffmpeg_command_no_subtitles":
        cmd = proc._build_ffmpeg_command_no_subtitles(
            entrada, salida, plan.audio, plan.idiomas_audio, nombres_audio=plan.nombres_audio)
    else:
        cmd = getattr(proc, constructor)(
            entrada, salida, plan.audio, [], plan.idiomas_audio, nombres_audio=plan.nombres_audio)

    assert _audios_mapeados(cmd) == ([0, 1], {0: "language=spa", 1: "language=spa"})
    assert _nombres_mapeados(cmd) == {0: "Español (España)", 1: "Español (Latinoamérica)"}


@requiere_ffmpeg
def test_titulos_audio_lee_titulo_por_pista(tmp_path):
    ruta = _video_lavfi(tmp_path / "Series" / "Show S01E01.mkv", [("spa", "Castellano"), ("es-419", None)])
    proc = _processor(tmp_path)

    assert proc.titulos_audio(ruta) == {0: "Castellano"}


@requiere_ffmpeg
def test_punta_a_punta_variantes_regionales_quedan_en_el_mp4(tmp_path):
    ruta = _video_lavfi(
        tmp_path / "Series" / "Show S01E01.mkv",
        [("spa", "Castellano"), ("es-419", None), ("eng", None)],
    )
    proc = _processor(tmp_path)
    proc._validate_file_for_compression = lambda p: None
    proc._should_use_gpu_compression = lambda p, resolution=None: False

    ok, info, temp, audio, subs, idiomas = proc._prepare_compression(ruta, resolution={"width": 160, "height": 120})
    salida = tmp_path / "salida.mp4"
    cmd = proc._build_fallback_ffmpeg_command(
        ruta, salida, audio, subs, idiomas, omitir_subtitulos=info["omitir_subtitulos"],
        nombres_audio=info["nombres_audio"],
    )
    subprocess.run(cmd, check=True, capture_output=True, timeout=120)

    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream_tags=language,handler_name", "-of", "csv=p=0", str(salida)],
        capture_output=True, text=True, timeout=30, check=True,
    )
    filas = sorted(l.strip() for l in r.stdout.splitlines())
    assert filas == sorted(["spa,Español (España)", "spa,Español (Latinoamérica)"]) or filas == sorted(
        ["Español (España),spa", "Español (Latinoamérica),spa"])


# ---------------------------------------------------------------------------
# Subtítulos que ya son mov_text se copian: al recodificarlos ffmpeg los pasa a
# microsegundos y uno de más de ~35 min (p. ej. el hueco entre subtítulos
# forzados) supera el máximo del MP4 ("Application provided duration ... is
# invalid") y la compresión falla al final (Jason Bourne).
# ---------------------------------------------------------------------------


def _codecs_subs(cmd):
    """-c:s:K de cada subtítulo de salida (o el -c:s general)."""
    codecs = {}
    for i, arg in enumerate(cmd[:-1]):
        m = re.fullmatch(r"-c:s(?::(\d+))?", arg)
        if m:
            codecs[int(m.group(1)) if m.group(1) else "todos"] = cmd[i + 1]
    return codecs


@pytest.mark.parametrize("constructor", CONSTRUCTORES)
def test_subtitulos_mov_text_se_copian(tmp_path, monkeypatch, constructor):
    monkeypatch.setattr(processor_module.subprocess, "run",
                        _ffprobe_audio([("aac", 2)], codecs_subs=["mov_text", "subrip", "mov_text"]))
    proc = _processor(tmp_path)

    cmd = getattr(proc, constructor)(tmp_path / "e.mp4", tmp_path / "s.mp4", [0], [0, 1, 2], {0: "spa"})

    assert _subs_mapeados(cmd) == [0, 1, 2]
    assert _codecs_subs(cmd) == {0: "copy", 1: "mov_text", 2: "copy"}


def test_error_de_duracion_de_subtitulo_reintenta_sin_subtitulos(tmp_path):
    proc = _processor(tmp_path)

    assert proc._es_error_de_subtitulos("[mp4 @ 0x1] Application provided duration: 2436959000 is invalid")
    assert proc._es_error_de_subtitulos("Could not find codec parameters for stream 3")
    assert not proc._es_error_de_subtitulos("Conversion failed!")
    assert not proc._es_error_de_subtitulos(None)


@requiere_ffmpeg
def test_punta_a_punta_subtitulo_mov_text_se_copia(tmp_path):
    # Un MP4 con subtítulo mov_text sale con el subtítulo copiado (sin recodificar).
    # El caso real (un subtítulo de >35 min) no se puede fabricar: ffmpeg no
    # sabe escribirlo; se comprobó a mano con el archivo de Jason Bourne.
    srt = tmp_path / "corto.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:00,900\nHola\n", encoding="utf-8")
    entrada = tmp_path / "Peliculas" / "x.mp4"
    entrada.parent.mkdir(parents=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=1",
         "-f", "lavfi", "-i", "sine=duration=1", "-i", str(srt), "-map", "0", "-map", "1", "-map", "2",
         "-c:v", "mpeg4", "-c:a", "aac", "-c:s", "mov_text", "-metadata:s:s:0", "language=spa", str(entrada)],
        check=True, capture_output=True, timeout=60)
    proc = _processor(tmp_path)
    salida = tmp_path / "salida.mp4"

    cmd = proc._build_fallback_ffmpeg_command(entrada, salida, [0], [0], {0: "spa"})
    subprocess.run(cmd, check=True, capture_output=True, timeout=120)

    assert _codecs_subs(cmd) == {0: "copy"}
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "s", "-show_entries", "stream=codec_name",
                        "-of", "csv=p=0", str(salida)], capture_output=True, text=True, timeout=30, check=True)
    assert r.stdout.split() == ["mov_text"]
