import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import mediajelly_db  # noqa: E402
from panel.app import crear_app  # noqa: E402
from panel.servicios import acciones  # noqa: E402


@pytest.fixture
def cliente():
    c = TestClient(crear_app(password="clave", secret="s"), follow_redirects=False)
    assert c.post("/login", data={"password": "clave"}).status_code == 303
    return c


def _fila(fp, ruta, estado, prioridad=0, intentos=2):
    mediajelly_db.init_db()
    with sqlite3.connect(mediajelly_db._get_db_path()) as conn:
        conn.execute(
            "INSERT INTO media_files (fingerprint, path, status, attempts, created_at, updated_at, prioridad) "
            "VALUES (?,?,?,?,?,?,?)", (fp, ruta, estado, intentos, "2026-01-01", "2026-01-01", prioridad))


def _estado(fp):
    return mediajelly_db.get_by_fingerprint(fp).status


def _prioridad(fp):
    with sqlite3.connect(mediajelly_db._get_db_path()) as conn:
        return conn.execute("SELECT prioridad FROM media_files WHERE fingerprint = ?", (fp,)).fetchone()[0]


def test_reprocesar_y_omitir_cambian_estado(cliente):
    _fila("a", "/m/a.mkv", "failed")
    _fila("b", "/m/b.mkv", "done")
    _fila("c", "/m/c.mkv", "pending")
    r = cliente.post("/acciones/reprocesar", data={"fp": ["a", "b"]})
    assert r.status_code == 200
    assert _estado("a") == "pending" and _estado("b") == "pending"
    r = cliente.post("/acciones/omitir", data={"fp": ["c"]})
    assert r.status_code == 200
    assert _estado("c") == "skipped"
    r = cliente.post("/acciones/sacar-de-cuarentena", data={"fp": ["a"]})
    assert r.status_code == 200 and _estado("a") == "pending"


def test_servicio_devuelve_resultado():
    _fila("a", "/m/a.mkv", "failed")
    res = acciones.reprocesar(["a", "zzz"])
    assert res.ok == 1 and res.cambiados == ["a"] and res.no_encontrados == ["zzz"]


def test_procesar_primero_sube_al_frente(cliente):
    _fila("a", "/m/a.mkv", "failed")
    _fila("b", "/m/b.mkv", "pending")
    r = cliente.post("/acciones/procesar-primero", data={"fp": ["a", "b"]})
    assert r.status_code == 200
    assert _prioridad("a") == 10 and _prioridad("b") == 10
    assert _estado("a") == "pending" and _estado("b") == "pending"


def _intentos(fp):
    with sqlite3.connect(mediajelly_db._get_db_path()) as conn:
        return conn.execute(
            "SELECT attempts, first_failed_at, failure_type FROM media_files WHERE fingerprint = ?",
            (fp,)).fetchone()


def test_procesar_primero_desde_cuarentena_reinicia_intentos():
    """Sin reiniciar, el archivo volvería a cuarentena al primer fallo."""
    _fila("a", "/m/a.mkv", "failed", intentos=3)
    with sqlite3.connect(mediajelly_db._get_db_path()) as conn:
        conn.execute("UPDATE media_files SET first_failed_at = '2026-01-01', failure_type = 'ffmpeg' "
                     "WHERE fingerprint = 'a'")
    _fila("b", "/m/b.mkv", "pending", intentos=2)
    acciones.procesar_primero(["a", "b"])
    assert _estado("a") == "pending" and _intentos("a") == (0, None, None)
    assert _intentos("b")[0] == 2  # un pendiente conserva sus intentos


def test_huella_inexistente_409(cliente):
    _fila("a", "/m/a.mkv", "failed")
    r = cliente.post("/acciones/reprocesar", data={"fp": ["a", "no-existe"]})
    assert r.status_code == 409
    assert "El archivo cambió; recarga la página" in r.text
    r = cliente.post("/acciones/accion-rara", data={"fp": ["a"]})
    assert r.status_code == 404


def test_acciones_exigen_post_y_sesion(cliente):
    assert cliente.get("/acciones/reprocesar").status_code == 405
    anonimo = TestClient(crear_app(password="clave", secret="s"), follow_redirects=False)
    assert anonimo.post("/acciones/reprocesar", data={"fp": ["a"]}).status_code == 303
    assert anonimo.post("/ciclo").status_code == 303


def test_lanzar_ciclo_dos_veces_una_tarea(cliente):
    r1 = cliente.post("/ciclo")
    r2 = cliente.post("/ciclo")
    assert "Ciclo en cola (empieza en menos de 1 min)" in r1.text
    assert "Ya hay un ciclo en cola" in r2.text
    assert len(mediajelly_db.listar_tareas()) == 1
    assert acciones.lanzar_ciclo()[1] is False


def test_cola_muestra_checkbox_con_huella(cliente):
    _fila("huella-1", "/m/a.mkv", "failed")
    _fila("huella-2", "/m/b.mkv", "pending")
    html = cliente.get("/cola").text
    assert 'name="fp" value="huella-1"' in html
    assert 'name="fp" value="huella-2"' in html
    assert 'hx-post="/acciones/omitir"' in html
    assert "¿Omitir los archivos seleccionados? No se procesarán." in html


def test_inicio_muestra_boton_y_tareas(cliente):
    html = cliente.get("/").text
    assert 'hx-post="/ciclo"' in html and "Ejecutar ciclo ahora" in html
    assert "Tareas" in html
    mediajelly_db.crear_tarea("ciclo", {})
    html = cliente.get("/").text
    assert "ciclo" in html and "pendiente" in html


def test_sacar_de_cuarentena_reinicia_intentos(cliente):
    _fila("a", "/m/a.mkv", "failed", intentos=3)
    assert cliente.post("/acciones/sacar-de-cuarentena", data={"fp": ["a"]}).status_code == 200
    assert mediajelly_db.record_failure("a", "falla", resource=False) == "pending"
    assert mediajelly_db.get_by_fingerprint("a").attempts == 1


def test_reprocesar_reinicia_intentos(cliente):
    _fila("a", "/m/a.mkv", "failed", intentos=3)
    cliente.post("/acciones/reprocesar", data={"fp": ["a"]})
    assert mediajelly_db.get_by_fingerprint("a").attempts == 0


def test_no_toca_archivos_en_proceso(cliente):
    _fila("a", "/m/a.mkv", "processing", intentos=1)
    _fila("b", "/m/b.mkv", "failed")
    for accion in ("reprocesar", "omitir", "procesar-primero", "sacar-de-cuarentena"):
        r = cliente.post(f"/acciones/{accion}", data={"fp": ["a", "b"]})
        assert r.status_code == 200
        assert "1 archivo(s) se están procesando; no se cambiaron." in r.text
        assert _estado("a") == "processing" and _prioridad("a") == 0
    res = acciones.omitir(["a"])
    assert res.en_proceso == ["a"] and res.ok == 0 and res.cambiados == []


def test_lote_vacio_400_con_retarget(cliente):
    r = cliente.post("/acciones/omitir", data={})
    assert r.status_code == 400
    assert r.headers["HX-Retarget"] == "#aviso-cola"
    assert "Elige al menos un archivo" in r.text


def test_base_deja_pasar_solo_400_y_409(cliente):
    html = cliente.get("/cola").text
    assert "409" in html and "400" in html
    assert "404" not in html.split("htmx:beforeSwap")[1].split("</script>")[0]
    assert "500" not in html.split("htmx:beforeSwap")[1].split("</script>")[0]


def test_409_trae_cabeceras_y_aplica_las_validas(cliente):
    _fila("a", "/m/a.mkv", "pending")
    r = cliente.post("/acciones/omitir", data={"fp": ["a", "no-existe"]})
    assert r.status_code == 409
    assert r.headers["HX-Retarget"] == "#aviso-cola"
    assert r.headers["HX-Reswap"] == "innerHTML"
    assert _estado("a") == "skipped"


def test_boton_de_ciclo_fuera_del_fragmento_sse(cliente):
    plantillas = Path(__file__).resolve().parent.parent / "scripts" / "panel" / "plantillas"
    assert "/ciclo" not in (plantillas / "_estado.html").read_text()
    assert "/ciclo" in (plantillas / "inicio.html").read_text()
