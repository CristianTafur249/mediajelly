import functools
import os
import shutil
import sqlite3
import sys
import time
from collections import namedtuple
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import mediajelly_db  # noqa: E402
import mediajelly_progreso  # noqa: E402
from panel import app as modulo_app  # noqa: E402
from panel.app import crear_app  # noqa: E402
from panel.servicios import estado  # noqa: E402


@pytest.fixture
def cliente():
    c = TestClient(crear_app(password="clave", secret="s"), follow_redirects=False)
    assert c.post("/login", data={"password": "clave"}).status_code == 303
    return c


def _pendientes_y_fallido(tmp_path):
    for nombre in ("a.mkv", "b.mkv", "c.mkv"):
        (tmp_path / nombre).write_bytes(nombre.encode() * 2000)
    for nombre in ("a.mkv", "b.mkv", "c.mkv"):
        mediajelly_db.register(tmp_path / nombre, require_settled=False)
    registro = mediajelly_db.get_by_path(tmp_path / "c.mkv")
    mediajelly_db.set_status(registro.fingerprint, "failed")


def _sin_disco_lleno(monkeypatch):
    Uso = namedtuple("Uso", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda _r: Uso(100, 10, 90))


def test_estado_contadores_y_proximos(tmp_path, monkeypatch):
    _sin_disco_lleno(monkeypatch)
    _pendientes_y_fallido(tmp_path)
    e = estado.obtener_estado(tmp_path)
    assert e["contadores"]["pending"] == 2
    assert e["contadores"]["failed"] == 1
    assert e["proximos"] == ["a.mkv", "b.mkv"]
    assert "1 en cuarentena" in e["alertas"]
    assert e["error_bd"] is None


def test_proximos_respetan_procesar_primero(tmp_path, monkeypatch, cliente):
    _sin_disco_lleno(monkeypatch)
    _pendientes_y_fallido(tmp_path)
    mediajelly_db.set_prioridad(mediajelly_db.get_by_path(tmp_path / "b.mkv").fingerprint, 10)
    assert estado.obtener_estado(tmp_path)["proximos"] == ["b.mkv", "a.mkv"]
    html = cliente.get("/").text
    lista = html.split('<ol class="proximos">', 1)[1].split("</ol>", 1)[0]
    assert lista.index("b.mkv") < lista.index("a.mkv")


def test_ahorro_y_general(tmp_path, monkeypatch):
    _sin_disco_lleno(monkeypatch)
    mediajelly_db.save_progress({"processing": {
        "current_file": 3, "total_files": 10, "current_file_name": "x.mkv", "status": "processing",
        "stats": {"total_original_size": 1000, "total_compressed_size": 400}}})
    e = estado.obtener_estado(tmp_path)
    assert e["ahorro_bytes"] == 600
    assert e["general"] == {"actual": 3, "total": 10, "nombre": "x.mkv", "estado": "processing"}


def _bloquear_conexion(monkeypatch):
    mediajelly_db.init_db()  # la BD existe: lo que falla es abrirla

    def bloqueada(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(estado.sqlite3, "connect", bloqueada)


def test_estado_bd_bloqueada_no_falla(tmp_path, monkeypatch, caplog):
    _sin_disco_lleno(monkeypatch)
    _bloquear_conexion(monkeypatch)
    with caplog.at_level("WARNING"):
        e = estado.obtener_estado(tmp_path)
    assert isinstance(e, dict)
    assert e["error_bd"] == "La base de datos no respondió; se reintenta en 2 s."
    assert "locked" not in e["error_bd"]
    assert "locked" in caplog.text
    assert e["contadores"] == {}


def test_estado_sin_bd_no_crea_archivo(tmp_path, monkeypatch):
    _sin_disco_lleno(monkeypatch)
    ruta = tmp_path / "nada" / "mediajelly.db"
    monkeypatch.setattr(mediajelly_db, "_get_db_path", lambda: ruta)
    e = estado.obtener_estado(tmp_path)
    assert not ruta.exists()
    assert e["error_bd"] is None
    assert e["contadores"] == {}


def test_estado_no_escribe_bd(tmp_path, monkeypatch):
    _sin_disco_lleno(monkeypatch)
    _pendientes_y_fallido(tmp_path)
    ruta = mediajelly_db._get_db_path()
    antes = (ruta.stat().st_mtime_ns, ruta.stat().st_size)
    estado.obtener_estado(tmp_path)
    despues = (ruta.stat().st_mtime_ns, ruta.stat().st_size)
    assert antes == despues


def test_alerta_disco(tmp_path, monkeypatch):
    Uso = namedtuple("Uso", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda _r: Uso(100, 95, 5))
    e = estado.obtener_estado(tmp_path)
    assert any(a.startswith("Disco casi lleno") and "5" in a for a in e["alertas"])


def test_alerta_ciclo_viejo(tmp_path, monkeypatch):
    _sin_disco_lleno(monkeypatch)
    mediajelly_progreso._escribir_json(
        mediajelly_progreso.DIR_PROGRESO / "ciclo.json", {"fase": None, "fin": time.time() - 5 * 3600})
    e = estado.obtener_estado(tmp_path)
    assert "Último ciclo hace 5 h" in e["alertas"]


def test_alerta_ciclo_colgado(tmp_path, monkeypatch):
    _sin_disco_lleno(monkeypatch)
    mediajelly_progreso._escribir_json(
        mediajelly_progreso.DIR_PROGRESO / "ciclo.json", {"fase": "compresion", "desde": time.time() - 6 * 3600})
    e = estado.obtener_estado(tmp_path)
    assert "El ciclo lleva 6 h en Compresión" in e["alertas"]


def test_etapa_huerfana_se_oculta(tmp_path, monkeypatch):
    _sin_disco_lleno(monkeypatch)
    mediajelly_progreso.iniciar("compresion-111", "viejo.mkv", 100)
    mediajelly_progreso.iniciar("compresion-222", "reciente.mkv", 100)
    mediajelly_progreso.iniciar("subtitulos", "medio.mkv", 100)
    d = mediajelly_progreso.DIR_PROGRESO
    antes = time.time() - 20 * 60
    os.utime(d / "compresion-111.json", (antes, antes))
    medio = time.time() - 5 * 60
    os.utime(d / "subtitulos.json", (medio, medio))
    e = estado.obtener_estado(tmp_path)
    etapas = {a.etapa: a for a in e["etapas"]}
    assert "compresion-111" not in etapas
    assert etapas["subtitulos"].sin_senal is True
    assert etapas["compresion-222"].sin_senal is False


def test_nombres_visibles():
    assert estado.nombre_etapa("compresion-123") == "Comprimiendo"
    assert estado.nombre_etapa("subtitulos") == "Subtítulos (Whisper)"
    assert estado.nombre_etapa("idiomas") == "Detectando idiomas"
    assert estado.nombre_fase({"fase": "escaneo"}) == "Escaneando"
    assert estado.nombre_fase({"fase": None, "fin": 1}) == "En espera"
    assert estado.formato_tiempo(75) == "01:15"
    assert estado.formato_tiempo(3725) == "1:02:05"


def test_eventos_emite_estado(cliente, monkeypatch):
    monkeypatch.setattr(modulo_app, "generar_eventos",
                        functools.partial(modulo_app.generar_eventos, limite=1))
    with cliente.stream("GET", "/eventos") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        primero = next(r.iter_text())
    assert primero.startswith("event: estado\ndata: ")
    assert primero.endswith("\n\n")
    assert "\n" not in primero[len("event: estado\n"):].rstrip("\n")


def test_eventos_requiere_sesion():
    c = TestClient(crear_app(password="clave", secret="s"), follow_redirects=False)
    assert c.get("/eventos").status_code == 303


def test_inicio_muestra_progreso(cliente):
    mediajelly_progreso.iniciar("compresion-42", "Dr. STONE S03E04.mp4", 100)
    mediajelly_progreso.actualizar("compresion-42", 62)
    html = cliente.get("/").text
    assert "62" in html
    assert "Dr. STONE S03E04" in html
    assert "Comprimiendo" in html
    assert 'sse-connect="/eventos"' in html
    assert 'sse-swap="estado"' in html
    assert 'hx-ext="sse"' in html


def test_inicio_sin_senal(cliente):
    mediajelly_progreso.iniciar("subtitulos", "Serie.mkv", 100)
    medio = time.time() - 5 * 60
    os.utime(mediajelly_progreso.DIR_PROGRESO / "subtitulos.json", (medio, medio))
    assert "sin señal" in cliente.get("/").text


def test_inicio_con_bd_bloqueada_responde(cliente, monkeypatch):
    _bloquear_conexion(monkeypatch)
    r = cliente.get("/")
    assert r.status_code == 200
    assert "La base de datos no respondió" in r.text
    assert "locked" not in r.text


def test_inicio_en_reposo_no_muestra_archivo_x_de_n(cliente):
    """Al terminar queda current_file=0: "Archivo 0 de 412" solo confunde."""
    mediajelly_db.save_progress({"processing": {
        "current_file": 0, "total_files": 412, "current_file_name": "Procesamiento completado",
        "status": "completed"}})
    html = cliente.get("/").text
    assert "Archivo 0 de 412" not in html and "Procesamiento completado" not in html
    mediajelly_db.save_progress({"processing": {
        "current_file": 7, "total_files": 412, "current_file_name": "x.mkv", "status": "processing"}})
    assert "Archivo 7 de 412 · x.mkv" in cliente.get("/").text
