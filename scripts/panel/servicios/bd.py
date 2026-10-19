"""Acceso de solo lectura a la BD del panel: nunca crea archivos ni esquema."""
import contextlib
import logging
import sqlite3

import mediajelly_db

registro = logging.getLogger(__name__)

MENSAJE_BD = "La base de datos no respondió; se reintenta en 2 s."


@contextlib.contextmanager
def conexion_solo_lectura():
    """Entrega una conexión `mode=ro` (o None si la BD no existe) y la cierra al salir.

    No usa init_db ni retry_on_locked: el panel nunca escribe ni espera a un ciclo.
    """
    ruta = mediajelly_db._get_db_path()
    if not ruta.exists():
        yield None
        return
    conn = sqlite3.connect(f"file:{ruta}?mode=ro", uri=True, timeout=2)
    try:
        yield conn
    finally:
        conn.close()
