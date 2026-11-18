"""Prioridad de la cola y tabla de tareas del panel (mediajelly_db)."""
import multiprocessing as mp
import sqlite3
import sys
from pathlib import Path

import pytest

import mediajelly_db as db


def _registrar(tmp_path, nombre, fp):
    """Inserta una fila pendiente directamente, con created_at controlado."""
    db.init_db()
    conn = db._get_conn()
    with conn:
        conn.execute(
            "INSERT INTO media_files(fingerprint, path, status, created_at, updated_at) "
            "VALUES (?, ?, 'pending', ?, ?)",
            (fp, str(tmp_path / nombre), "2026-01-01 00:00:0" + fp[-1], "2026-01-01"),
        )
    conn.close()


def test_prioridad_ordena_pendientes(tmp_path):
    _registrar(tmp_path, "a.mkv", "fp1")
    _registrar(tmp_path, "b.mkv", "fp2")
    _registrar(tmp_path, "c.mkv", "fp3")
    assert [Path(p).name for p in db.get_pending_files()] == ["a.mkv", "b.mkv", "c.mkv"]
    db.set_prioridad("fp3", 5)
    db.set_prioridad("fp2", 1)
    assert [Path(p).name for p in db.get_pending_files()] == ["c.mkv", "b.mkv", "a.mkv"]
    assert [r.fingerprint for r in db.list_by_status("pending")] == ["fp3", "fp2", "fp1"]


def test_migracion_agrega_prioridad_a_bd_existente(tmp_path, monkeypatch):
    ruta = tmp_path / "vieja.db"
    monkeypatch.setattr(db, "_get_db_path", lambda: ruta)
    conn = sqlite3.connect(str(ruta))
    conn.execute(
        """CREATE TABLE media_files (
            fingerprint TEXT PRIMARY KEY, path TEXT UNIQUE NOT NULL, status TEXT NOT NULL,
            attempts INTEGER DEFAULT 0, first_failed_at TEXT, last_error TEXT,
            failure_type TEXT, derived_from TEXT, missing_since TEXT,
            status_before_missing TEXT, created_at TEXT, updated_at TEXT)"""
    )
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO schema_version VALUES (2)")
    conn.execute(
        "INSERT INTO media_files(fingerprint, path, status, created_at) VALUES ('x', '/m/x.mkv', 'pending', '2026-01-01')"
    )
    conn.commit()
    conn.close()

    db.init_db()
    db.init_db()  # idempotente

    conn = sqlite3.connect(str(ruta))
    columnas = [r[1] for r in conn.execute("PRAGMA table_info(media_files)")]
    filas = conn.execute("SELECT fingerprint, prioridad FROM media_files").fetchall()
    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    conn.close()
    assert "prioridad" in columnas
    assert filas == [("x", 0)]
    assert version == 4


def test_crear_tarea_no_duplica():
    id1, creada1 = db.crear_tarea("ciclo", {})
    id2, creada2 = db.crear_tarea("ciclo", {})
    assert (creada1, creada2) == (True, False)
    assert id1 == id2
    # El orden de las claves no cambia la identidad
    a, _ = db.crear_tarea("retag", {"fp": "f", "idioma": "spa"})
    b, creada = db.crear_tarea("retag", {"idioma": "spa", "fp": "f"})
    assert a == b and creada is False
    # Argumentos distintos son otra tarea
    c, creada = db.crear_tarea("retag", {"fp": "g", "idioma": "spa"})
    assert creada is True and c != a
    # Una tarea terminada ya no bloquea una nueva
    tomada = db.tomar_siguiente_tarea()
    db.terminar_tarea(tomada["id"], True, "listo")
    nueva, creada = db.crear_tarea(tomada["tipo"], tomada["argumentos"])
    assert creada is True and nueva != tomada["id"]


def _tomar_muchas(ruta, salida):
    db._get_db_path = lambda: Path(ruta)
    ids = []
    while True:
        t = db.tomar_siguiente_tarea()
        if t is None:
            break
        ids.append(t["id"])
    salida.put(ids)


def test_tomar_siguiente_es_atomica(tmp_path, monkeypatch):
    ruta = tmp_path / "carrera.db"
    monkeypatch.setattr(db, "_get_db_path", lambda: ruta)
    for i in range(30):
        db.crear_tarea("detectar", {"n": i})
    ctx = mp.get_context("spawn")
    cola = ctx.Queue()
    procs = [ctx.Process(target=_tomar_muchas, args=(str(ruta), cola)) for _ in range(2)]
    for p in procs:
        p.start()
    resultados = [cola.get(timeout=60) for _ in procs]
    for p in procs:
        p.join(timeout=60)
    todos = resultados[0] + resultados[1]
    assert len(todos) == 30
    assert len(set(todos)) == 30


def test_tomar_devuelve_la_mas_antigua_y_none_al_final():
    a, _ = db.crear_tarea("ciclo", {})
    b, _ = db.crear_tarea("detectar", {})
    t = db.tomar_siguiente_tarea()
    assert t["id"] == a and t["estado"] == "corriendo" and t["iniciada"]
    assert t["argumentos"] == {}
    assert db.tomar_siguiente_tarea()["id"] == b
    assert db.tomar_siguiente_tarea() is None


def test_terminar_tarea():
    i, _ = db.crear_tarea("ciclo", {})
    j, _ = db.crear_tarea("detectar", {})
    db.tomar_siguiente_tarea()
    db.tomar_siguiente_tarea()
    db.terminar_tarea(i, True, "bien")
    db.terminar_tarea(j, False, "mal")
    por_id = {t["id"]: t for t in db.listar_tareas()}
    assert por_id[i]["estado"] == "ok" and por_id[i]["salida"] == "bien" and por_id[i]["terminada"]
    assert por_id[j]["estado"] == "error" and por_id[j]["salida"] == "mal"


def test_liberar_colgadas():
    viva, _ = db.crear_tarea("ciclo", {})
    vieja, _ = db.crear_tarea("detectar", {})
    db.tomar_siguiente_tarea()
    db.tomar_siguiente_tarea()
    conn = db._get_conn()
    with conn:
        conn.execute(
            "UPDATE panel_tareas SET iniciada = datetime('now', '-3 hours') WHERE id = ?", (vieja,)
        )
    conn.close()
    assert db.liberar_tareas_colgadas(120) == 1
    por_id = {t["id"]: t for t in db.listar_tareas()}
    assert por_id[vieja]["estado"] == "error"
    assert por_id[vieja]["salida"] == "Interrumpida (proceso detenido)"
    assert por_id[viva]["estado"] == "corriendo"


def test_listar_tareas_mas_nueva_primero_y_limite():
    ids = [db.crear_tarea("detectar", {"n": n})[0] for n in range(5)]
    assert [t["id"] for t in db.listar_tareas()] == ids[::-1]
    assert [t["id"] for t in db.listar_tareas(limite=2)] == ids[::-1][:2]


def test_tipo_invalido():
    with pytest.raises(ValueError):
        db.crear_tarea("borrar_todo", {})
    assert db.TIPOS_TAREA == {"ciclo", "retag", "detectar"}
    assert db.listar_tareas() == []


def test_reiniciar_fallos_deja_contadores_en_cero(tmp_path):
    _registrar(tmp_path, "a.mkv", "fp1")
    for _ in range(2):
        db.record_failure("fp1", "ffmpeg falló", resource=False)
    db.reiniciar_fallos("fp1")
    conn = db._get_conn()
    fila = conn.execute(
        "SELECT attempts, first_failed_at, failure_type, status FROM media_files WHERE fingerprint = 'fp1'"
    ).fetchone()
    conn.close()
    assert (fila[0], fila[1], fila[2], fila[3]) == (0, None, None, "pending")
    db.reiniciar_fallos("no-existe")  # no lanza


def test_reasignar_tareas_cambia_solo_las_pendientes_de_esa_huella():
    pendiente, _ = db.crear_tarea("retag", {"fp": "vieja", "pista": 2, "idioma": "jpn"})
    detectar, _ = db.crear_tarea("detectar", {"fp": "vieja"})
    otra, _ = db.crear_tarea("detectar", {"fp": "otra"})
    ciclo, _ = db.crear_tarea("ciclo", {})
    corriendo, _ = db.crear_tarea("retag", {"fp": "vieja", "pista": 1, "idioma": "spa"})
    conn = db._get_conn()
    with conn:
        conn.execute("UPDATE panel_tareas SET estado = 'corriendo' WHERE id = ?", (corriendo,))
    conn.close()

    assert db.reasignar_tareas("vieja", "nueva") == 2

    por_id = {t["id"]: t for t in db.listar_tareas(20)}
    assert por_id[pendiente]["argumentos"] == {"fp": "nueva", "pista": 2, "idioma": "jpn"}
    assert por_id[detectar]["argumentos"] == {"fp": "nueva"}
    assert por_id[otra]["argumentos"] == {"fp": "otra"}
    assert por_id[ciclo]["argumentos"] == {}
    assert por_id[corriendo]["argumentos"]["fp"] == "vieja"
    # el JSON queda igual que el de crear_tarea: la deduplicación sigue funcionando
    assert db.crear_tarea("detectar", {"fp": "nueva"}) == (detectar, False)
    assert db.reasignar_tareas("vieja", "nueva") == 0


def test_borrar_idiomas_detectados_conserva_los_manuales():
    db.set_stream_language("fp1", 1, "jpn", "whisper")
    db.set_stream_language("fp1", 2, "spa", "manual")
    db.set_stream_language("fp1", 3, "eng", "cache")
    db.set_stream_language("fp2", 1, "kor", "whisper")
    assert db.borrar_idiomas_detectados("fp1") == 1
    assert db.get_stream_languages("fp1") == {2: "spa", 3: "eng"}
    assert db.get_stream_languages("fp2") == {1: "kor"}


def test_hay_tareas_pendientes():
    assert db.hay_tareas_pendientes() is False
    id_, _ = db.crear_tarea("ciclo", {})
    assert db.hay_tareas_pendientes() is True
    db.tomar_siguiente_tarea()
    assert db.hay_tareas_pendientes() is False
    db.terminar_tarea(id_, True, "ok")
    assert db.hay_tareas_pendientes() is False
