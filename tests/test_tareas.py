"""Ejecutor de tareas del panel en el contenedor del cron (mediajelly_tareas)."""
import fcntl
import os
import re
import sys
import types
from pathlib import Path

import pytest

import mediajelly_db as db
import mediajelly_retag
import mediajelly_tareas as tareas


@pytest.fixture(autouse=True)
def _tmp_aislado(tmp_path, monkeypatch):
    """El lock y los logs van a un directorio temporal, nunca a scripts/tmp."""
    monkeypatch.setattr(tareas, "DIR_TMP", tmp_path / "tmp")
    (tmp_path / "tmp").mkdir()


def _registrar(ruta: Path, contenido: bytes = b"x" * 4096):
    ruta.write_bytes(contenido)
    registro, _ = db.register(ruta, require_settled=False)
    assert registro is not None
    return registro


def _estado(id_):
    return next(t for t in db.listar_tareas(50) if t["id"] == id_)


def test_get_by_fingerprint(tmp_path):
    reg = _registrar(tmp_path / "a.mkv")
    assert db.get_by_fingerprint(reg.fingerprint).path == str(tmp_path / "a.mkv")
    assert db.get_by_fingerprint("no-existe") is None


def test_lock_ocupado_no_toca_tareas(tmp_path):
    id_, _ = db.crear_tarea("ciclo", {})
    fd = os.open(str(tareas.DIR_TMP / "cron_runner.lock"), os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert tareas.ejecutar_pendientes() == 0
        assert tareas.main() == 0
    finally:
        os.close(fd)
    assert _estado(id_)["estado"] == "pendiente"


def test_retag_ok_actualiza_huella_e_idioma(tmp_path, monkeypatch):
    reg = _registrar(tmp_path / "a.mkv")
    llamadas = []

    def falso_retag(path, mapa):
        llamadas.append((path, mapa))
        Path(path).write_bytes(b"y" * 5000)  # el remux cambia el contenido
        return True

    monkeypatch.setattr(mediajelly_retag, "retag_audio", falso_retag)
    id_, _ = db.crear_tarea("retag", {"fp": reg.fingerprint, "pista": 2, "idioma": "jpn"})
    assert tareas.ejecutar_pendientes() == 1
    assert llamadas == [(str(tmp_path / "a.mkv"), {2: "jpn"})]
    assert _estado(id_)["estado"] == "ok"
    nuevo = db.get_by_path(tmp_path / "a.mkv")
    assert nuevo.fingerprint != reg.fingerprint
    assert db.get_stream_languages(nuevo.fingerprint)[2] == "jpn"


def test_retag_fallido_guarda_el_motivo(tmp_path, monkeypatch):
    reg = _registrar(tmp_path / "a.mkv")
    monkeypatch.setattr(mediajelly_retag, "retag_audio", lambda p, m: False)
    monkeypatch.setattr(mediajelly_retag, "ULTIMO_ERROR", "ffmpeg falló")
    id_, _ = db.crear_tarea("retag", {"fp": reg.fingerprint, "pista": 1, "idioma": "spa"})
    tareas.ejecutar_pendientes()
    t = _estado(id_)
    assert t["estado"] == "error" and "ffmpeg falló" in t["salida"]


def test_retag_archivo_inexistente_error(tmp_path, monkeypatch):
    monkeypatch.setattr(mediajelly_retag, "retag_audio", lambda p, m: pytest.fail("no debe llamarse"))
    id_, _ = db.crear_tarea("retag", {"fp": "huella-que-no-existe", "pista": 1, "idioma": "spa"})
    tareas.ejecutar_pendientes()
    t = _estado(id_)
    assert t["estado"] == "error"
    assert t["salida"] == "El archivo cambió (se recomprimió o re-etiquetó); vuelve a abrirlo desde la Cola"


def test_retag_archivo_borrado_del_disco_error(tmp_path, monkeypatch):
    reg = _registrar(tmp_path / "a.mkv")
    (tmp_path / "a.mkv").unlink()
    monkeypatch.setattr(mediajelly_retag, "retag_audio", lambda p, m: pytest.fail("no debe llamarse"))
    id_, _ = db.crear_tarea("retag", {"fp": reg.fingerprint, "pista": 1, "idioma": "spa"})
    tareas.ejecutar_pendientes()
    assert "El archivo ya no está" in _estado(id_)["salida"]


def test_excepcion_deja_error_sin_reintento(tmp_path, monkeypatch):
    reg = _registrar(tmp_path / "a.mkv")

    def explota(path, mapa):
        raise RuntimeError("boom")

    monkeypatch.setattr(mediajelly_retag, "retag_audio", explota)
    id_, _ = db.crear_tarea("retag", {"fp": reg.fingerprint, "pista": 1, "idioma": "spa"})
    assert tareas.ejecutar_pendientes() == 1
    t = _estado(id_)
    assert t["estado"] == "error" and "boom" in t["salida"]
    assert tareas.ejecutar_pendientes() == 0  # no se reintenta sola


def test_argumentos_incompletos_error(tmp_path):
    id_, _ = db.crear_tarea("retag", {"fp": "x"})
    tareas.ejecutar_pendientes()
    assert _estado(id_)["estado"] == "error"


def test_detectar_recorre_pistas_de_audio(tmp_path, monkeypatch):
    reg = _registrar(tmp_path / "a.mkv")
    monkeypatch.setattr(mediajelly_retag, "_audio_indices", lambda p: [1, 3])
    vistas = []

    def falso_detector(path, indice):
        vistas.append((path, indice))
        return {1: "jpn", 3: None}[indice]

    modulo = types.ModuleType("mediajelly_language_detector")
    modulo.detect_language_with_whisper_for_processor = falso_detector
    monkeypatch.setitem(sys.modules, "mediajelly_language_detector", modulo)
    id_, _ = db.crear_tarea("detectar", {"fp": reg.fingerprint})
    tareas.ejecutar_pendientes()
    t = _estado(id_)
    assert vistas == [(str(tmp_path / "a.mkv"), 1), (str(tmp_path / "a.mkv"), 3)]
    assert t["estado"] == "ok" and "pista 1: jpn" in t["salida"] and "pista 3" in t["salida"]


def test_ciclo_suelta_lock_antes_de_lanzar(tmp_path, monkeypatch):
    id_, _ = db.crear_tarea("ciclo", {})
    estado_lock = {}
    lanzado = {}

    def falso_popen(cmd, **kw):
        fd = os.open(str(tareas.DIR_TMP / "cron_runner.lock"), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            estado_lock["libre"] = True
        except OSError:
            estado_lock["libre"] = False
        finally:
            os.close(fd)
        lanzado["cmd"], lanzado["kw"] = cmd, kw
        return types.SimpleNamespace(pid=1)

    monkeypatch.setattr(tareas.subprocess, "Popen", falso_popen)
    assert tareas.ejecutar_pendientes() == 1
    assert estado_lock == {"libre": True}
    assert lanzado["cmd"][0] == sys.executable
    assert lanzado["cmd"][1].endswith("mediajelly_cron_runner.py")
    assert lanzado["kw"]["start_new_session"] is True
    assert (tareas.DIR_TMP / "logs" / "cron.log").exists()
    t = _estado(id_)
    assert t["estado"] == "ok" and t["salida"] == "Ciclo lanzado"


def test_dos_ciclos_lanzan_una_sola_vez(tmp_path, monkeypatch):
    db.crear_tarea("ciclo", {})
    lanzados = []
    monkeypatch.setattr(tareas.subprocess, "Popen", lambda cmd, **kw: lanzados.append(cmd))
    # segunda tarea ciclo con otros argumentos para evitar la deduplicación
    db.crear_tarea("ciclo", {"otra": 1})
    assert tareas.ejecutar_pendientes() == 2
    assert len(lanzados) == 1


def test_libera_colgadas_al_arrancar(monkeypatch):
    llamado = []
    monkeypatch.setattr(db, "liberar_tareas_colgadas", lambda *a, **k: llamado.append(1) or 0)
    tareas.ejecutar_pendientes()
    assert llamado


def test_respeta_max_tareas(tmp_path, monkeypatch):
    monkeypatch.setattr(mediajelly_retag, "_audio_indices", lambda p: [])
    for i in range(4):
        db.crear_tarea("retag", {"fp": f"nada{i}", "pista": 1, "idioma": "spa"})
    assert tareas.ejecutar_pendientes(max_tareas=3) == 3
    assert [t["estado"] for t in db.listar_tareas(10)].count("pendiente") == 1


def _falso_retag_que_reescribe(monkeypatch):
    """Cada retag cambia el contenido del archivo (como el remux real) y anota lo pedido."""
    llamadas = []

    def falso_retag(path, mapa):
        llamadas.append(mapa)
        Path(path).write_bytes(f"remux {len(llamadas)}".encode() * 2000)
        return True

    monkeypatch.setattr(mediajelly_retag, "retag_audio", falso_retag)
    return llamadas


def test_dos_retag_seguidos_sobre_el_mismo_archivo(tmp_path, monkeypatch):
    """El primer retag cambia la huella; el segundo, encolado con la vieja, no debe fallar."""
    reg = _registrar(tmp_path / "a.mkv")
    llamadas = _falso_retag_que_reescribe(monkeypatch)
    primero, _ = db.crear_tarea("retag", {"fp": reg.fingerprint, "pista": 1, "idioma": "spa"})
    segundo, _ = db.crear_tarea("retag", {"fp": reg.fingerprint, "pista": 2, "idioma": "jpn"})
    assert tareas.ejecutar_pendientes() == 2
    assert llamadas == [{1: "spa"}, {2: "jpn"}]
    assert _estado(primero)["estado"] == "ok"
    assert _estado(segundo)["estado"] == "ok", _estado(segundo)["salida"]
    final = db.get_by_path(tmp_path / "a.mkv")
    # el segundo corrió sobre la huella que dejó el primero (la intermedia)
    assert _estado(segundo)["argumentos"]["fp"] not in (reg.fingerprint, final.fingerprint)
    assert db.get_stream_languages(final.fingerprint) == {1: "spa", 2: "jpn"}


def test_retag_y_luego_detectar(tmp_path, monkeypatch):
    reg = _registrar(tmp_path / "a.mkv")
    _falso_retag_que_reescribe(monkeypatch)
    monkeypatch.setattr(mediajelly_retag, "_audio_indices", lambda p: [1])
    modulo = types.ModuleType("mediajelly_language_detector")
    modulo.detect_language_with_whisper_for_processor = lambda path, indice: "spa"
    monkeypatch.setitem(sys.modules, "mediajelly_language_detector", modulo)
    db.crear_tarea("retag", {"fp": reg.fingerprint, "pista": 1, "idioma": "spa"})
    detectar, _ = db.crear_tarea("detectar", {"fp": reg.fingerprint})
    assert tareas.ejecutar_pendientes() == 2
    t = _estado(detectar)
    assert t["estado"] == "ok", t["salida"]
    assert t["argumentos"]["fp"] == db.get_by_path(tmp_path / "a.mkv").fingerprint


def test_detectar_borra_antes_lo_detectado_y_conserva_lo_manual(tmp_path, monkeypatch):
    """La caché del detector devolvería lo ya detectado: se borra para que vuelva a escuchar."""
    reg = _registrar(tmp_path / "a.mkv")
    db.set_stream_language(reg.fingerprint, 1, "eng", "whisper")
    db.set_stream_language(reg.fingerprint, 2, "spa", "manual")
    monkeypatch.setattr(mediajelly_retag, "_audio_indices", lambda p: [1, 2])
    vistos = []

    def falso_detector(path, indice):
        vistos.append(dict(db.get_stream_languages(reg.fingerprint)))
        return "jpn"

    modulo = types.ModuleType("mediajelly_language_detector")
    modulo.detect_language_with_whisper_for_processor = falso_detector
    monkeypatch.setitem(sys.modules, "mediajelly_language_detector", modulo)
    id_, _ = db.crear_tarea("detectar", {"fp": reg.fingerprint})
    tareas.ejecutar_pendientes()
    assert _estado(id_)["estado"] == "ok"
    assert vistos[0] == {2: "spa"}


_HORA = r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} "


def test_sin_pendientes_no_escribe_ni_toma_el_lock(capsys):
    assert tareas.ejecutar_pendientes() == 0
    assert capsys.readouterr().out == ""
    assert not (tareas.DIR_TMP / "cron_runner.lock").exists()


def test_lineas_del_log_con_fecha_y_hora(tmp_path, capsys):
    db.crear_tarea("retag", {"fp": "no-existe", "pista": 1, "idioma": "spa"})
    tareas.ejecutar_pendientes()
    lineas = capsys.readouterr().out.splitlines()
    assert lineas and all(re.match(_HORA, linea) for linea in lineas), lineas

    db.crear_tarea("ciclo", {"x": 1})
    fd = os.open(str(tareas.DIR_TMP / "cron_runner.lock"), os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        tareas.ejecutar_pendientes()
    finally:
        os.close(fd)
    (linea,) = capsys.readouterr().out.splitlines()
    assert re.match(_HORA + "Ciclo en marcha", linea), linea


def test_ciclo_que_no_se_puede_lanzar_queda_en_error(tmp_path, monkeypatch):
    id_, _ = db.crear_tarea("ciclo", {})

    def popen_roto(cmd, **kw):
        raise OSError("sin memoria para lanzar")

    monkeypatch.setattr(tareas.subprocess, "Popen", popen_roto)
    assert tareas.ejecutar_pendientes() == 1
    t = _estado(id_)
    assert t["estado"] == "error" and "sin memoria para lanzar" in t["salida"]


def test_lock_sin_permiso_no_revienta(tmp_path, monkeypatch, capsys):
    # Un cron_runner.lock de root deja a mediauser sin poder abrirlo.
    tid, _ = db.crear_tarea("ciclo", {})

    def sin_permiso(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(tareas.os, "open", sin_permiso)
    assert tareas.ejecutar_pendientes() == 0
    assert "No se pudo abrir cron_runner.lock" in capsys.readouterr().out
    assert db.listar_tareas()[0]["estado"] == "pendiente"
