"""Detalle de video del panel: datos técnicos, decisión por pista y acciones."""
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import mediajelly_db  # noqa: E402
import mediajelly_politica  # noqa: E402
from panel.app import crear_app  # noqa: E402
from panel.servicios import detalle  # noqa: E402

requiere_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="sin ffmpeg/ffprobe")


@pytest.fixture
def cliente():
    c = TestClient(crear_app(password="clave", secret="s"), follow_redirects=False)
    assert c.post("/login", data={"password": "clave"}).status_code == 303
    return c


def _video(ruta: Path, idiomas_audio, idiomas_subs=()) -> Path:
    """Video corto con ffmpeg lavfi: una pista de video, las de audio y subtítulos srt."""
    ruta.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=1"]
    for i, _ in enumerate(idiomas_audio):
        cmd += ["-f", "lavfi", "-i", f"sine=frequency={440 + 110 * i}:duration=1"]
    for i, _ in enumerate(idiomas_subs):
        srt = ruta.parent / f"sub{i}.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:00,900\nHola\n", encoding="utf-8")
        cmd += ["-i", str(srt)]
    total = 1 + len(idiomas_audio) + len(idiomas_subs)
    for i in range(total):
        cmd += ["-map", str(i)]
    cmd += ["-c:v", "mpeg4", "-c:a", "aac", "-c:s", "srt"]
    for i, lang in enumerate(idiomas_audio):
        cmd += [f"-metadata:s:a:{i}", f"language={lang}"]
    for i, lang in enumerate(idiomas_subs):
        cmd += [f"-metadata:s:s:{i}", f"language={lang}"]
    cmd.append(str(ruta))
    subprocess.run(cmd, check=True, capture_output=True, timeout=60)
    return ruta


def _registrar(ruta: Path) -> str:
    registro, _ = mediajelly_db.register(ruta, require_settled=False)
    return registro.fingerprint


def _fila(fp, ruta, estado="pending"):
    mediajelly_db.init_db()
    with sqlite3.connect(mediajelly_db._get_db_path()) as conn:
        conn.execute(
            "INSERT INTO media_files (fingerprint, path, status, attempts, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)", (fp, ruta, estado, 2, "2026-01-01 10:00:00", "2026-01-01 10:00:00"))


def _tareas():
    with sqlite3.connect(mediajelly_db._get_db_path()) as conn:
        return [(t, a) for t, a in conn.execute("SELECT tipo, argumentos FROM panel_tareas ORDER BY id")]


@requiere_ffmpeg
def test_detalle_marca_que_se_conserva(tmp_path):
    ruta = _video(tmp_path / "Series" / "Show S01E01.mkv", ["spa", "eng"], ["spa"])
    fp = _registrar(ruta)

    d = detalle.obtener_detalle(fp)

    assert d["archivo"]["nombre"] == "Show S01E01.mkv"
    assert d["archivo"]["estado"] == "pending"
    assert [a["se_conserva"] for a in d["audio"]] == [True, False]
    assert [a["etiqueta"] for a in d["audio"]] == ["spa", "eng"]
    assert [a["indice_absoluto"] for a in d["audio"]] == [1, 2]
    assert [a["indice_audio"] for a in d["audio"]] == [0, 1]
    assert [s["se_conserva"] for s in d["subtitulos"]] == [True]
    assert d["video"]["ancho"] == 160 and d["video"]["alto"] == 120
    assert d["video"]["codec"] == "mpeg4"
    assert "error" not in d
    assert "pista 1 (spa)" in d["plan"]
    assert "1 pista de audio" in d["plan"]
    assert "1 en español" in d["plan"]


@requiere_ffmpeg
def test_detalle_anime_sin_espanol_conserva_la_japonesa(tmp_path):
    # Sin español, el anime se queda con la pista japonesa (si no, la inglesa).
    ruta = _video(tmp_path / "Anime" / "Show S01E01.mkv", ["eng", "jpn"])
    fp = _registrar(ruta)

    d = detalle.obtener_detalle(fp)

    assert [a["se_conserva"] for a in d["audio"]] == [False, True]
    # El plan nombra el idioma de la pista, no "sin idioma".
    assert d["plan"].startswith("Se conserva la pista 2 (jpn)")


@requiere_ffmpeg
def test_detalle_serie_conserva_todas_las_pistas_en_espanol(tmp_path):
    ruta = _video(tmp_path / "Series" / "Show S01E04.mkv", ["spa", "spa", "eng"])
    fp = _registrar(ruta)

    d = detalle.obtener_detalle(fp)

    assert [a["se_conserva"] for a in d["audio"]] == [True, True, False]
    assert d["plan"].startswith("Se conservan las pistas 1 y 2 (spa)")
    assert "se quita 1 pista de audio" in d["plan"]


def _sondeo(*pistas):
    streams = [{"index": 0, "codec_type": "video", "codec_name": "h264", "width": 640, "height": 360}]
    for i, (tipo, codec, tags) in enumerate(pistas, start=1):
        streams.append({"index": i, "codec_type": tipo, "codec_name": codec, "tags": tags})
    return {"streams": streams, "format": {"size": "123"}}


def test_detalle_varias_pistas_con_idiomas_distintos():
    sondeo = _sondeo(("audio", "aac", {"language": "eng"}), ("audio", "aac", {"language": "spa"}),
                     ("audio", "aac", {"language": "es-419"}))
    d = detalle.armar_detalle({"ruta": "/m/Series/x.mkv", "nombre": "x.mkv", "tamano": 123}, sondeo, {})

    assert [a["se_conserva"] for a in d["audio"]] == [False, True, True]
    # El plan muestra el idioma que se escribirá (ISO 639-2) y, si la etiqueta
    # traía región, el nombre que llevará la pista.
    assert d["plan"].startswith("Se conservan las pistas 2 (spa) y 3 (spa, «Español (Latinoamérica)»)")


def test_detalle_idiomas_distintos_y_desconocido():
    sondeo = _sondeo(("audio", "aac", {"language": "spa"}), ("audio", "aac", {"language": "Castellano"}))
    d = detalle.armar_detalle({"ruta": "/m/Series/x.mkv", "nombre": "x.mkv", "tamano": 123}, sondeo, {})
    assert d["plan"].startswith("Se conservan las pistas 1 y 2 (spa)")

    sondeo = _sondeo(("audio", "aac", {"language": "xx-yy"}), ("audio", "aac", {"language": "fre"}))
    d = detalle.armar_detalle({"ruta": "/m/Series/x.mkv", "nombre": "x.mkv", "tamano": 123}, sondeo, {})
    assert d["plan"].startswith("Se conserva la pista 1 (sin idioma)")


def test_detalle_checo_no_es_espanol():
    sondeo = _sondeo(("audio", "aac", {"language": "ces"}), ("audio", "aac", {"language": "eng"}),
                     ("subtitle", "subrip", {"language": "est"}))
    d = detalle.armar_detalle({"ruta": "/m/Series/x.mkv", "nombre": "x.mkv", "tamano": 123}, sondeo, {})

    assert [a["se_conserva"] for a in d["audio"]] == [False, True]
    # Sin subtítulos en español cae al primero de texto, como el procesador.
    assert "no hay en español" in d["plan"]


def test_detalle_metadatos_de_cada_pista_por_su_indice():
    # Stream 0 = video; 1 "English" y 2 "Japanese", sin etiqueta de idioma.
    sondeo = _sondeo(("audio", "aac", {"title": "English"}), ("audio", "aac", {"title": "Japanese"}))
    d = detalle.armar_detalle({"ruta": "/m/Anime/x.mkv", "nombre": "x.mkv", "tamano": 123}, sondeo, {})

    assert [a["idioma"] for a in d["audio"]] == ["eng", "jpn"]
    assert [a["se_conserva"] for a in d["audio"]] == [False, True]


def test_detalle_grande_o_uhd_no_conserva_subtitulos(monkeypatch):
    monkeypatch.setattr(mediajelly_politica, "omitir_subtitulos", lambda t, a, h: True)
    sondeo = _sondeo(("audio", "aac", {"language": "spa"}), ("subtitle", "subrip", {"language": "eng"}),
                     ("subtitle", "subrip", {"language": "spa"}))
    d = detalle.armar_detalle({"ruta": "/m/Series/x.mkv", "nombre": "x.mkv", "tamano": 123}, sondeo, {})

    assert [s["se_conserva"] for s in d["subtitulos"]] == [False, False]
    assert "subtítulos: ninguno (archivo grande o UHD)" in d["plan"]


def test_detalle_reutiliza_las_reglas_de_idioma_de_la_politica():
    # Sin copias de los patrones: el panel usa las mismas funciones que el procesador.
    assert not hasattr(detalle, "_PATRONES_ESPANOL")
    assert not hasattr(detalle, "_METADATOS")


@requiere_ffmpeg
def test_detalle_usa_idioma_detectado_por_huella(tmp_path):
    ruta = _video(tmp_path / "Series" / "Show S01E02.mkv", ["eng", "und"])
    fp = _registrar(ruta)
    mediajelly_db.set_stream_language(fp, 2, "spa", "whisper")  # índice absoluto

    d = detalle.obtener_detalle(fp)

    assert [a["detectado"] for a in d["audio"]] == [None, "spa"]
    assert [a["se_conserva"] for a in d["audio"]] == [False, True]


@requiere_ffmpeg
def test_detalle_usa_politica(tmp_path, monkeypatch):
    ruta = _video(tmp_path / "Series" / "Show S01E03.mkv", ["spa", "eng"])
    fp = _registrar(ruta)
    vistos = {}

    def plan_falso(audio_languages, audio_espanol, subs_espanol, es_anime, grande, titulos_audio=None):
        vistos.update(langs=audio_languages, esp=audio_espanol, anime=es_anime, grande=grande)
        return mediajelly_politica.PlanPistas([1], {1: "eng"}, [], False)

    monkeypatch.setattr(mediajelly_politica, "planificar_pistas", plan_falso)

    d = detalle.obtener_detalle(fp)

    assert [a["se_conserva"] for a in d["audio"]] == [False, True]
    assert vistos == {"langs": {0: "spa", 1: "eng"}, "esp": [0], "anime": False, "grande": False}


def test_detalle_omitir_subtitulos_viene_de_politica(monkeypatch):
    assert detalle.politica is mediajelly_politica
    llamadas = []
    monkeypatch.setattr(mediajelly_politica, "omitir_subtitulos",
                        lambda t, a, h: llamadas.append((t, a, h)) or True)
    sondeo = {"streams": [{"index": 0, "codec_type": "video", "codec_name": "h264", "width": 640, "height": 360}],
              "format": {"size": "123"}}
    d = detalle.armar_detalle({"ruta": "/m/x.mkv", "nombre": "x.mkv", "tamano": 123}, sondeo, {})
    assert llamadas == [(123, 640, 360)]
    assert "subtítulos" in d["plan"]


def test_detalle_ffprobe_falla(tmp_path):
    ruta = tmp_path / "roto.mkv"
    ruta.write_bytes(b"esto no es un video" * 100)
    _fila("fp-roto", str(ruta))

    d = detalle.obtener_detalle("fp-roto")

    assert d["audio"] == [] and d["subtitulos"] == [] and d["video"] == {}
    assert "ffprobe" in d["error"]


def test_detalle_huella_inexistente_404(cliente):
    assert detalle.obtener_detalle("no-existe") is None
    r = cliente.get("/video/no-existe")
    assert r.status_code == 404
    assert "No se encontró" in r.text


@requiere_ffmpeg
def test_pagina_de_detalle(cliente, tmp_path):
    ruta = _video(tmp_path / "Series" / "Show S01E04.mkv", ["spa", "eng"], ["spa"])
    fp = _registrar(ruta)

    r = cliente.get(f"/video/{fp}")

    assert r.status_code == 200
    assert "Show S01E04.mkv" in r.text
    assert '<span class="insignia">Pendiente</span>' in r.text  # estado en español
    assert "✔" in r.text and "✂" in r.text
    assert f'hx-post="/video/{fp}/reprocesar"' in r.text
    assert f'hx-post="/video/{fp}/detectar"' in r.text
    # el <select> de cada pista envía el índice absoluto del stream
    assert 'name="pista" value="1"' in r.text and 'name="pista" value="2"' in r.text
    for idioma in detalle.IDIOMAS_PERMITIDOS:
        assert f'<option value="{idioma}"' in r.text


def test_idioma_no_permitido_400(cliente):
    _fila("fp1", "/m/a.mkv")
    for idioma in ("xxx", "", "SPA", "spa; rm -rf", "es"):
        r = cliente.post("/video/fp1/idioma", data={"pista": "1", "idioma": idioma})
        assert r.status_code == 400, idioma
        assert r.headers["HX-Retarget"] == "#aviso-video"
    r = cliente.post("/video/fp1/idioma", data={"pista": "uno", "idioma": "spa"})
    assert r.status_code == 400
    r = cliente.post("/video/fp1/idioma", data={"pista": "-1", "idioma": "spa"})
    assert r.status_code == 400
    assert _tareas() == []


def test_corregir_idioma_crea_tarea(cliente):
    _fila("fp1", "/m/a.mkv")
    r = cliente.post("/video/fp1/idioma", data={"pista": "2", "idioma": "jpn"})
    assert r.status_code == 200
    assert "en cola" in r.text
    r = cliente.post("/video/fp1/idioma", data={"pista": "2", "idioma": "jpn"})
    assert r.status_code == 200 and "Ya hay" in r.text
    assert _tareas() == [("retag", '{"fp": "fp1", "idioma": "jpn", "pista": 2}')]


def test_detectar_crea_tarea(cliente):
    _fila("fp1", "/m/a.mkv")
    r = cliente.post("/video/fp1/detectar")
    assert r.status_code == 200 and "en cola" in r.text
    assert _tareas() == [("detectar", '{"fp": "fp1"}')]


def test_acciones_con_huella_inexistente_409(cliente):
    for accion, datos in (("reprocesar", None), ("detectar", None),
                          ("idioma", {"pista": "1", "idioma": "spa"})):
        r = cliente.post(f"/video/no-existe/{accion}", data=datos)
        assert r.status_code == 409, accion
        assert "El archivo cambió; recarga la página" in r.text
        assert r.headers["HX-Retarget"] == "#aviso-video"
    assert _tareas() == []


def test_reprocesar_desde_detalle(cliente):
    _fila("fp1", "/m/a.mkv", "failed")
    _fila("fp2", "/m/b.mkv", "processing")
    r = cliente.post("/video/fp1/reprocesar")
    assert r.status_code == 200
    assert mediajelly_db.get_by_fingerprint("fp1").status == "pending"
    assert mediajelly_db.get_by_fingerprint("fp1").attempts == 0
    r = cliente.post("/video/fp2/reprocesar")
    assert r.status_code == 200 and "se está procesando" in r.text
    assert r.text.startswith('<p class="aviso">')
    assert mediajelly_db.get_by_fingerprint("fp2").status == "processing"


def test_detalle_exige_sesion_y_post(cliente):
    anonimo = TestClient(crear_app(password="clave", secret="s"), follow_redirects=False)
    assert anonimo.get("/video/fp1").status_code == 303
    for accion in ("reprocesar", "detectar", "idioma"):
        assert anonimo.post(f"/video/fp1/{accion}").status_code == 303
        assert cliente.get(f"/video/fp1/{accion}").status_code == 405


def test_cola_enlaza_al_detalle(cliente):
    _fila("fp1", "/m/a.mkv")
    r = cliente.get("/cola")
    assert 'href="/video/fp1"' in r.text


@pytest.mark.parametrize("pista", ["²", "١", "-1", "abc", "1.5", " 1", "1000",
                                   pytest.param("9" * 5000, id="enorme")])
def test_pista_no_valida_400(cliente, pista):
    _fila("fp1", "/m/a.mkv")
    r = cliente.post("/video/fp1/idioma", data={"pista": pista, "idioma": "spa"})
    assert r.status_code == 400
    assert "Pista no válida" in r.text
    assert r.headers["HX-Retarget"] == "#aviso-video"
    assert _tareas() == []


def test_detalle_archivo_ausente_en_disco(cliente, tmp_path):
    _fila("fp-ausente", str(tmp_path / "ya no está.mkv"))

    d = detalle.obtener_detalle("fp-ausente")

    assert d["video"] == {} and d["audio"] == [] and d["subtitulos"] == []
    assert d["archivo"]["nombre"] == "ya no está.mkv"
    assert "no está en el disco" in d["error"]
    r = cliente.get("/video/fp-ausente")
    assert r.status_code == 200
    assert "no está en el disco" in r.text


def test_avisos_contenedor_neutro_y_fragmento_con_clase(cliente):
    _fila("fp1", "/m/a.mkv", "failed")
    r = cliente.get("/video/fp1")
    assert '<div id="aviso-video" class="avisos" role="status" aria-live="polite">' in r.text
    r = cliente.get("/cola")
    assert '<div id="aviso-cola" class="avisos" role="status" aria-live="polite">' in r.text
    # éxito: clase ok, no aviso (rojo)
    for accion in ("reprocesar", "detectar"):
        r = cliente.post(f"/video/fp1/{accion}")
        assert r.status_code == 200 and 'class="ok"' in r.text and "aviso" not in r.text
    r = cliente.post("/video/fp1/idioma", data={"pista": "1", "idioma": "spa"})
    assert r.status_code == 200 and 'class="ok"' in r.text
    # error: un solo bloque con clase aviso
    r = cliente.post("/video/fp1/idioma", data={"pista": "1", "idioma": "xxx"})
    assert r.text.startswith('<p class="aviso">')


def test_css_tiene_estilo_ok_claro_y_oscuro():
    css = (Path(__file__).resolve().parent.parent / "scripts/panel/estaticos/panel.css").read_text()
    assert css.count("--ok:") == 2 and ".ok {" in css


def test_huella_vieja_tras_retag_redirige_a_la_nueva(cliente, tmp_path):
    """Tras re-etiquetar, la huella cambia: el enlace viejo lleva al detalle nuevo."""
    ruta = tmp_path / "Serie.mkv"
    ruta.write_bytes(b"x" * 4096)
    vieja = _registrar(ruta)
    ruta.write_bytes(b"y" * 5000)  # el remux reescribe el archivo
    nueva = mediajelly_db.record_rewrite(vieja, ruta).fingerprint
    assert nueva != vieja
    r = cliente.get(f"/video/{vieja}")
    assert r.status_code == 303
    assert r.headers["location"] == f"/video/{nueva}"
    assert detalle.huella_derivada(vieja) == nueva
    assert detalle.huella_derivada("no-existe") is None
    assert cliente.get("/video/no-existe").status_code == 404


def test_detalle_muestra_la_variante_regional_que_se_escribira():
    sondeo = {"streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080},
        {"index": 1, "codec_type": "audio", "codec_name": "aac", "tags": {"language": "spa", "title": "Castellano"}},
        {"index": 2, "codec_type": "audio", "codec_name": "aac", "tags": {"language": "es-419"}},
        {"index": 3, "codec_type": "audio", "codec_name": "aac", "tags": {"language": "eng", "title": "English"}},
    ], "format": {"size": "1000"}}
    archivo = {"ruta": "/mediajelly/media/series/Show/Show S01E01.mkv", "tamano": 1000}

    d = detalle.armar_detalle(archivo, sondeo, {})

    assert [a["nombre"] for a in d["audio"]] == ["Español (España)", "Español (Latinoamérica)", None]
    assert [a["titulo"] for a in d["audio"]] == ["Castellano", None, "English"]
    assert d["plan"].startswith(
        "Se conservan las pistas 1 (spa, «Español (España)») y 2 (spa, «Español (Latinoamérica)»)")
