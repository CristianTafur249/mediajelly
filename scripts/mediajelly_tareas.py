#!/usr/bin/env python3
"""Ejecuta las tareas que el panel encola en panel_tareas.

Corre cada minuto por cron en el contenedor mediajelly-cron. Toma el flock de
cron_runner.lock para no pisar un ciclo en marcha; si está ocupado, sale sin
tocar nada y lo reintenta el minuto siguiente.
"""
import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import mediajelly_db as db
import mediajelly_retag

DIR_SCRIPTS = Path(__file__).resolve().parent
DIR_TMP = DIR_SCRIPTS / "tmp"
RUNNER = DIR_SCRIPTS / "mediajelly_cron_runner.py"
MENSAJE_HUELLA_CAMBIADA = "El archivo cambió (se recomprimió o re-etiquetó); vuelve a abrirlo desde la Cola"


def _log(mensaje: str) -> None:
    """Una línea de tareas.log con fecha y hora (cron no las agrega)."""
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {mensaje}", flush=True)


def _tomar_lock() -> Optional[int]:
    """Toma el flock de cron_runner.lock; None si está ocupado."""
    DIR_TMP.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(DIR_TMP / "cron_runner.lock"), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as error:
        # P. ej. un lock que dejó un runner lanzado a mano como root.
        _log(f"No se pudo abrir cron_runner.lock ({error}); lanza los scripts con -u mediauser")
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n{int(time.time())}\n".encode())
    return fd


def _ruta_de(fp: str) -> str:
    """Ruta del archivo de una huella; falla si ya no está registrado o en disco."""
    registro = db.get_by_fingerprint(fp)
    if registro is None:
        raise RuntimeError(MENSAJE_HUELLA_CAMBIADA)
    if not Path(registro.path).is_file():
        raise RuntimeError("El archivo ya no está")
    return registro.path


def _argumentos(tarea: dict, *claves: str) -> list:
    faltan = [c for c in claves if c not in tarea["argumentos"]]
    if faltan:
        raise ValueError(f"Faltan argumentos: {', '.join(faltan)}")
    return [tarea["argumentos"][c] for c in claves]


def _retag(tarea: dict) -> str:
    fp, pista, idioma = _argumentos(tarea, "fp", "pista", "idioma")
    pista = int(pista)
    ruta = _ruta_de(fp)
    if not mediajelly_retag.retag_audio(ruta, {pista: idioma}):
        raise RuntimeError(mediajelly_retag.ULTIMO_ERROR or "No se pudo escribir la etiqueta")
    nuevo = db.record_rewrite(fp, ruta)
    db.set_stream_language(nuevo.fingerprint, pista, idioma, "manual")
    if nuevo.fingerprint != fp:
        # el remux cambia la huella: las tareas encoladas sobre el mismo archivo la siguen
        db.reasignar_tareas(fp, nuevo.fingerprint)
    return f"pista {pista}: {db.normalize_lang(idioma)}"


def _detectar(tarea: dict) -> str:
    (fp,) = _argumentos(tarea, "fp")
    ruta = _ruta_de(fp)
    # Import perezoso: arrastra Whisper y torch, que solo esta tarea necesita.
    import mediajelly_language_detector as detector

    pistas = mediajelly_retag._audio_indices(Path(ruta))
    if not pistas:
        raise RuntimeError("El archivo no tiene pistas de audio")
    # El detector devuelve primero lo guardado: sin borrar lo que detectó antes no
    # volvería a escuchar. Los idiomas corregidos a mano se conservan.
    db.borrar_idiomas_detectados(fp)
    # El detector guarda cada idioma en la BD por su cuenta (fuente "whisper").
    resumen = []
    for indice in pistas:
        idioma = detector.detect_language_with_whisper_for_processor(ruta, indice)
        resumen.append(f"pista {indice}: {idioma or 'sin resultado'}")
    if all(r.endswith("sin resultado") for r in resumen):
        raise RuntimeError("No se detectó el idioma de ninguna pista (" + "; ".join(resumen) + ")")
    return "; ".join(resumen)


def _lanzar_ciclo() -> None:
    """Lanza el cron runner sin esperarlo; su salida va a cron.log (append)."""
    dir_logs = DIR_TMP / "logs"
    dir_logs.mkdir(parents=True, exist_ok=True)
    with open(dir_logs / "cron.log", "ab") as log:
        subprocess.Popen(
            [sys.executable, str(RUNNER)],
            cwd=str(DIR_SCRIPTS),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def _ejecutar(tarea: dict) -> str:
    tipo = tarea["tipo"]
    if tipo == "retag":
        return _retag(tarea)
    if tipo == "detectar":
        return _detectar(tarea)
    raise ValueError(f"Tipo de tarea desconocido: {tipo!r}")


def ejecutar_pendientes(max_tareas: int = 5) -> int:
    """Procesa hasta `max_tareas` tareas pendientes. Devuelve cuántas procesó."""
    db.liberar_tareas_colgadas()
    # Sin pendientes se sale sin tomar el lock ni escribir: corre cada minuto.
    if not db.hay_tareas_pendientes():
        return 0
    fd = _tomar_lock()
    if fd is None:
        _log("Ciclo en marcha (cron_runner.lock tomado); las tareas esperan")
        return 0
    procesadas = 0
    ciclos = []  # se cierran cuando el lanzamiento funcionó (o falló)
    try:
        while procesadas < max_tareas:
            tarea = db.tomar_siguiente_tarea()
            if tarea is None:
                break
            procesadas += 1
            if tarea["tipo"] == "ciclo":
                ciclos.append(tarea["id"])
                continue
            try:
                salida = _ejecutar(tarea)
                db.terminar_tarea(tarea["id"], True, salida)
                _log(f"Tarea {tarea['id']} ({tarea['tipo']}): {salida}")
            except Exception as e:
                db.terminar_tarea(tarea["id"], False, str(e) or type(e).__name__)
                _log(f"Tarea {tarea['id']} ({tarea['tipo']}) falló: {e}")
    finally:
        os.close(fd)  # el runner toma el lock por su cuenta
    if ciclos:
        try:
            _lanzar_ciclo()
            ok, salida = True, "Ciclo lanzado"
        except Exception as e:
            ok, salida = False, f"No se pudo lanzar el ciclo: {str(e) or type(e).__name__}"
        for id_ in ciclos:
            db.terminar_tarea(id_, ok, salida)
            _log(f"Tarea {id_} (ciclo): {salida}")
    return procesadas


def main() -> int:
    ejecutar_pendientes()
    return 0


if __name__ == "__main__":
    sys.exit(main())
