import builtins
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import mediajelly_db  # noqa: E402
from panel.app import crear_app  # noqa: E402
from panel.servicios import bd, cola, registros  # noqa: E402


@pytest.fixture
def cliente():
    c = TestClient(crear_app(password="clave", secret="s"), follow_redirects=False)
    assert c.post("/login", data={"password": "clave"}).status_code == 303
    return c


@pytest.fixture
def dir_logs(tmp_path, monkeypatch):
    d = tmp_path / "logs"
    d.mkdir()
    monkeypatch.setattr(registros, "DIR_REGISTROS", d)
    return d


def _fila(fp, ruta, estado, intentos=0, error=None, creado="2026-01-01", actualizado="2026-01-01",
          prioridad=0):
    with sqlite3.connect(mediajelly_db._get_db_path()) as conn:
        conn.execute(
            "INSERT INTO media_files (fingerprint, path, status, attempts, last_error, created_at, updated_at, "
            "prioridad) VALUES (?,?,?,?,?,?,?,?)",
            (fp, ruta, estado, intentos, error, creado, actualizado, prioridad))


def test_cola_agrupa_por_estado(zona_bogota):
    mediajelly_db.init_db()
    _fila("1", "/m/b.mkv", "pending", creado="2026-01-02")
    _fila("2", "/m/a.mkv", "pending", creado="2026-01-02")
    _fila("3", "/m/z.mkv", "pending", creado="2026-01-01")
    _fila("4", "/m/p1.mkv", "processing", actualizado="2026-02-01")
    _fila("5", "/m/f1.mkv", "failed", 3, "ffmpeg falló", actualizado="2026-03-01 12:00:00")
    _fila("6", "/m/f2.mkv", "failed", 2, "otro", actualizado="2026-04-01 12:00:00")
    _fila("7", "/m/h.mkv", "done")
    c = cola.listar_cola()
    assert [x["nombre"] for x in c["pendientes"]] == ["z.mkv", "a.mkv", "b.mkv"]
    assert [x["nombre"] for x in c["en_proceso"]] == ["p1.mkv"]
    assert [x["nombre"] for x in c["cuarentena"]] == ["f2.mkv", "f1.mkv"]
    assert c["cuarentena"][1] == {"nombre": "f1.mkv", "ruta": "/m/f1.mkv", "intentos": 3,
                                  "error": "ffmpeg falló", "fingerprint": "5", "actualizado": "01/03/2026 07:00",
                                  "prioridad": 0}
    assert c["error_bd"] is None


def test_cola_procesar_primero_va_al_frente_con_insignia(cliente):
    """Lo marcado con "Procesar primero" encabeza Pendientes, en el mismo orden que el procesador."""
    mediajelly_db.init_db()
    _fila("1", "/m/a.mkv", "pending", creado="2026-01-01")
    _fila("2", "/m/b.mkv", "pending", creado="2026-01-02", prioridad=10)
    _fila("3", "/m/c.mkv", "pending", creado="2026-01-03")
    _fila("4", "/m/d.mkv", "pending", creado="2026-01-04", prioridad=10)
    c = cola.listar_cola()
    assert [x["nombre"] for x in c["pendientes"]] == ["b.mkv", "d.mkv", "a.mkv", "c.mkv"]
    assert [x["nombre"] for x in c["pendientes"]] == [
        Path(r.path).name for r in mediajelly_db.list_by_status("pending")]
    html = cliente.get("/cola").text
    assert html.index("b.mkv") < html.index("d.mkv") < html.index("a.mkv") < html.index("c.mkv")
    tarjetas = html.split('<li class="tarjeta archivo">')[1:]
    con_insignia = [t for t in tarjetas if '<span class="insignia primero">Primero</span>' in t]
    assert len(con_insignia) == 2
    assert "b.mkv" in con_insignia[0] and "d.mkv" in con_insignia[1]


def test_cola_sin_bd_no_crea_archivo(tmp_path, monkeypatch):
    ruta = tmp_path / "nada" / "mediajelly.db"
    monkeypatch.setattr(mediajelly_db, "_get_db_path", lambda: ruta)
    c = cola.listar_cola()
    assert c == {"pendientes": [], "en_proceso": [], "cuarentena": [], "error_bd": None}
    assert not ruta.exists()


def test_cola_no_escribe_bd():
    mediajelly_db.init_db()
    _fila("1", "/m/a.mkv", "pending")
    ruta = mediajelly_db._get_db_path()
    antes = (ruta.stat().st_mtime_ns, ruta.stat().st_size)
    cola.listar_cola()
    assert antes == (ruta.stat().st_mtime_ns, ruta.stat().st_size)


def test_cola_bd_bloqueada(monkeypatch, caplog):
    mediajelly_db.init_db()

    def bloqueada(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(bd.sqlite3, "connect", bloqueada)
    with caplog.at_level("WARNING"):
        c = cola.listar_cola()
    assert c["error_bd"] == "La base de datos no respondió; se reintenta en 2 s."
    assert "locked" not in c["error_bd"]
    assert "locked" in caplog.text
    assert c["pendientes"] == []


def test_pagina_cola(cliente):
    mediajelly_db.init_db()
    _fila("5", "/m/serie/f1.mkv", "failed", 3, "línea1\nlínea2", "2026-03-01")
    r = cliente.get("/cola")
    assert r.status_code == 200
    assert "f1.mkv" in r.text and "<details" in r.text and "línea2" in r.text


def test_pagina_cola_sin_bd(cliente, tmp_path, monkeypatch):
    monkeypatch.setattr(mediajelly_db, "_get_db_path", lambda: tmp_path / "x" / "m.db")
    assert cliente.get("/cola").status_code == 200


def test_cola_exige_sesion():
    c = TestClient(crear_app(password="clave", secret="s"), follow_redirects=False)
    assert c.get("/cola").status_code == 303
    assert c.get("/registros").status_code == 303


def test_registro_clave_invalida_404(cliente, dir_logs, monkeypatch):
    abierto = []
    real = builtins.open
    monkeypatch.setattr(builtins, "open", lambda *a, **k: (abierto.append(a), real(*a, **k))[1])
    r = cliente.get("/registros", params={"archivo": "../../etc/passwd"})
    assert r.status_code == 404
    assert abierto == []
    with pytest.raises(KeyError):
        registros.leer_registro("../../etc/passwd")


def test_registro_lee_solo_el_final(dir_logs, monkeypatch):
    ruta = dir_logs / "cron_runner.log"
    with open(ruta, "w") as f:
        for i in range(400_000):
            f.write(f"linea {i:07d} relleno relleno\n")
    assert ruta.stat().st_size > 10 * 1024 * 1024
    leidos = [0]
    real = builtins.open

    class Contador:
        def __init__(self, fh):
            self._fh = fh

        def read(self, n=-1):
            datos = self._fh.read(n)
            leidos[0] += len(datos)
            return datos

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._fh.close()

        def __getattr__(self, nombre):
            return getattr(self._fh, nombre)

    def contando(*a, **k):
        fh = real(*a, **k)
        return Contador(fh) if Path(a[0]) == ruta else fh

    monkeypatch.setattr(builtins, "open", contando)
    lineas = registros.leer_registro("ciclo")
    assert len(lineas) == 300
    assert lineas[-1] == "linea 0399999 relleno relleno"
    assert lineas[0] == "linea 0399700 relleno relleno"
    assert 0 < leidos[0] < 1024 * 1024


def test_registro_filtro(dir_logs):
    (dir_logs / "compression-errors.log").write_text("Todo bien\nERROR: fallo uno\notra\nerror: fallo dos\n")
    assert registros.leer_registro("errores", filtro="ERROR") == ["ERROR: fallo uno", "error: fallo dos"]
    assert registros.leer_registro("errores", lineas=1, filtro="error") == ["error: fallo dos"]


def test_registro_inexistente(cliente, dir_logs):
    assert registros.leer_registro("idiomas") == []
    r = cliente.get("/registros", params={"archivo": "idiomas"})
    assert r.status_code == 200
    assert "Todavía no hay registros." in r.text


def test_pagina_registros(cliente, dir_logs):
    (dir_logs / "cron_runner.log").write_text("hola <b>x</b>\nsegunda\n")
    r = cliente.get("/registros")
    assert r.status_code == 200
    assert 'hx-trigger="every 5s"' in r.text
    assert "hola &lt;b&gt;x&lt;/b&gt;" in r.text
    r = cliente.get("/registros", params={"archivo": "ciclo", "filtro": "segunda"})
    assert "segunda" in r.text and "hola" not in r.text


def test_registros_fragmento_htmx(cliente, dir_logs):
    (dir_logs / "cron_runner.log").write_text("hola\n")
    r = cliente.get("/registros", params={"archivo": "ciclo"}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and "hola" in r.text and "<html" not in r.text


def test_css_lista_cola_no_desborda():
    css = (Path(__file__).resolve().parent.parent / "scripts/panel/estaticos/panel.css").read_text()
    import re
    regla = re.search(r"\.lista-cola\s*\{([^}]*)\}", css).group(1)
    assert "grid-template-columns: minmax(0, 1fr)" in regla
    for sel in (".archivo-nombre", ".archivo-error summary"):
        regla = re.search(re.escape(sel) + r"\s*\{([^}]*)\}", css).group(1)
        assert "overflow: hidden" in regla and "text-overflow: ellipsis" in regla


def _escribir(dir_logs, lineas, final=True):
    ruta = dir_logs / "cron_runner.log"
    ruta.write_bytes(("\n".join(lineas) + ("\n" if final else "")).encode("utf-8"))
    return ruta


@pytest.mark.parametrize("tam", [65535, 65536, 65537, 131072])
def test_lineas_en_el_limite_de_bloque(dir_logs, tam):
    lineas = ["a" * 100, "b" * (tam - 1), "c" * 50, "d" * (tam - 1), "e" * 10]
    # tam = bytes de la línea incluyendo el salto
    _escribir(dir_logs, lineas)
    assert registros.leer_registro("ciclo", lineas=10) == lineas
    assert registros.leer_registro("ciclo", lineas=2) == lineas[-2:]
    # línea que termina justo en el límite de bloque
    relleno = "x" * (65536 - 1)
    lineas = [relleno, "fin"]
    _escribir(dir_logs, lineas)
    assert registros.leer_registro("ciclo", lineas=5) == lineas


def test_utf8_multibyte_cortado(dir_logs):
    for desfase in range(0, 5):
        texto = "ñ€😀" * 10
        lineas = ["z" * (65536 - 3 - desfase), texto, "ñ" * 40000, "€😀fin"]
        _escribir(dir_logs, lineas)
        assert registros.leer_registro("ciclo", lineas=10) == lineas


def test_archivo_sin_salto_final(dir_logs):
    _escribir(dir_logs, ["uno", "dos", "tres"], final=False)
    assert registros.leer_registro("ciclo") == ["uno", "dos", "tres"]
    assert registros.leer_registro("ciclo", lineas=1) == ["tres"]


def test_contra_referencia_aleatoria(dir_logs):
    import random
    azar = random.Random(1234)
    ruta = dir_logs / "cron_runner.log"
    for _ in range(25):
        lineas = ["".join(azar.choice("ab ñ€😀") for _ in range(azar.choice([0, 1, 5, 300, 70000])))
                  for _ in range(azar.randint(0, 40))]
        ruta.write_bytes(("\n".join(lineas) + azar.choice(["", "\n"])).encode("utf-8"))
        n = azar.choice([1, 3, 10, 300])
        assert registros.leer_registro("ciclo", lineas=n) == ruta.read_text(encoding="utf-8").splitlines()[-n:]


@pytest.mark.parametrize("clave", ["CICLO", "ciclo ", ""])
def test_registro_clave_no_exacta_404(cliente, dir_logs, clave):
    assert cliente.get("/registros", params={"archivo": clave}).status_code == 404


def test_cola_escapa_html(cliente):
    mediajelly_db.init_db()
    _fila("9", "/m/<script>alert(1)</script>.mkv", "failed", 1, "<script>alert(2)</script>")
    t = cliente.get("/cola").text
    assert "<script>alert" not in t
    assert "&lt;script&gt;alert(2)&lt;/script&gt;" in t
    assert "&lt;script&gt;alert(1)&lt;/script&gt;.mkv" in t


def test_registros_lo_mas_nuevo_arriba(cliente, dir_logs):
    """El refresco cada 5 s vuelve el scroll arriba: lo más reciente debe verse sin bajar."""
    (dir_logs / "cron_runner.log").write_text("primera\nsegunda\ntercera\n")
    for cabeceras in ({}, {"HX-Request": "true"}):
        texto = cliente.get("/registros", headers=cabeceras).text
        assert texto.index("tercera") < texto.index("segunda") < texto.index("primera")
    # el servicio sigue entregando el orden del archivo
    assert registros.leer_registro("ciclo") == ["primera", "segunda", "tercera"]


def test_registro_de_subtitulos(cliente, dir_logs):
    (dir_logs / "subtitle_translator.log").write_text("traduciendo S01E01\n")
    assert registros.REGISTROS["subtitulos"] == "subtitle_translator.log"
    assert registros.leer_registro("subtitulos") == ["traduciendo S01E01"]
    r = cliente.get("/registros", params={"archivo": "subtitulos"})
    assert r.status_code == 200 and "traduciendo S01E01" in r.text
    assert '<option value="subtitulos" selected>Subtítulos</option>' in r.text


@pytest.fixture
def zona_bogota(monkeypatch):
    import time
    monkeypatch.setenv("TZ", "America/Bogota")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_cola_actualizado_en_hora_local(zona_bogota):
    mediajelly_db.init_db()
    _fila("5", "/m/f1.mkv", "failed", 1, "x", actualizado="2026-03-01 15:30:00")  # datetime('now'): UTC
    _fila("6", "/m/f2.mkv", "failed", 1, "x", actualizado="no es una fecha")
    c = cola.listar_cola()
    por_nombre = {x["nombre"]: x["actualizado"] for x in c["cuarentena"]}
    assert por_nombre["f1.mkv"] == "01/03/2026 10:30"
    assert por_nombre["f2.mkv"] == "no es una fecha"  # si no se entiende, se muestra tal cual
