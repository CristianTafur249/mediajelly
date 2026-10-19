"""Cola de archivos que muestra el panel: solo lectura, nunca lanza."""
import logging
from datetime import datetime, timezone
from pathlib import Path

from panel.servicios import bd

registro = logging.getLogger(__name__)

_CONSULTA = (
    "SELECT path, fingerprint, attempts, last_error, updated_at, prioridad FROM media_files "
    "WHERE status = ? ORDER BY {orden}"
)
_GRUPOS = (
    # mismo orden que mediajelly_db.list_by_status: lo marcado "Procesar primero" va delante
    ("pendientes", "pending", "prioridad DESC, created_at ASC, path ASC"),
    ("en_proceso", "processing", "updated_at DESC"),
    ("cuarentena", "failed", "updated_at DESC"),
)


def hora_local(texto):
    """'2026-03-01 15:30:00' de datetime('now') (UTC) en la hora del contenedor (TZ).

    Si no se entiende, se devuelve tal cual.
    """
    try:
        instante = datetime.fromisoformat(str(texto).strip())
    except ValueError:
        return texto
    if instante.tzinfo is None:
        instante = instante.replace(tzinfo=timezone.utc)
    return instante.astimezone().strftime("%d/%m/%Y %H:%M")


def listar_cola() -> dict:
    """Pendientes, en proceso y en cuarentena; `error_bd` lleva el aviso si la BD falla."""
    resultado = {"pendientes": [], "en_proceso": [], "cuarentena": [], "error_bd": None}
    try:
        with bd.conexion_solo_lectura() as conn:
            if conn is None:
                return resultado
            for clave, estado, orden in _GRUPOS:
                resultado[clave] = [
                    {"nombre": Path(ruta).name, "ruta": ruta, "fingerprint": fingerprint, "intentos": intentos or 0,
                     "error": error, "actualizado": hora_local(actualizado) if actualizado else actualizado,
                     "prioridad": prioridad or 0}
                    for ruta, fingerprint, intentos, error, actualizado, prioridad
                    in conn.execute(_CONSULTA.format(orden=orden), (estado,))
                ]
    except Exception as exc:
        registro.warning("Lectura de la cola fallida: %s", exc)
        resultado = {"pendientes": [], "en_proceso": [], "cuarentena": [],
                     "error_bd": bd.MENSAJE_BD}
    return resultado


def listar_tareas_recientes(limite: int = 5) -> list[dict]:
    """Últimas tareas del panel, solo lectura; sin tabla o con la BD ocupada queda vacía."""
    try:
        with bd.conexion_solo_lectura() as conn:
            if conn is None:
                return []
            filas = conn.execute(
                "SELECT id, tipo, estado, salida, creada FROM panel_tareas ORDER BY id DESC LIMIT ?",
                (int(limite),),
            ).fetchall()
    except Exception as exc:  # p. ej. panel_tareas aún no existe
        registro.info("Sin tareas que mostrar: %s", exc)
        return []
    return [{"id": i, "tipo": tipo, "estado": estado, "salida": salida,
             "creada": hora_local(creada) if creada else creada}
            for i, tipo, estado, salida, creada in filas]
