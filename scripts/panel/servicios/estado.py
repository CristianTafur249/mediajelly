"""Estado que muestra el Inicio del panel: solo lectura, nunca lanza."""
import json
import logging
import shutil
import sqlite3
import time
from pathlib import Path

import mediajelly_db
import mediajelly_progreso
from panel.servicios import bd

registro = logging.getLogger(__name__)

MENSAJE_BD = bd.MENSAJE_BD
ESTADOS = ("pending", "processing", "done", "skipped", "failed", "missing")
SEGUNDOS_HUERFANA = 15 * 60
HORAS_CICLO_VIEJO = 4
MINIMO_DISCO_LIBRE = 0.10

NOMBRES_ETAPA = {
    "compresion": "Comprimiendo",
    "subtitulos": "Subtítulos (Whisper)",
    "idiomas": "Detectando idiomas",
}
NOMBRES_FASE = {
    "escaneo": "Escaneando",
    "idiomas": "Idiomas",
    "compresion": "Compresión",
    "subtitulos": "Subtítulos",
}


def nombre_etapa(etapa: str) -> str:
    """'compresion-123' (un worker) se muestra como 'Comprimiendo'."""
    base = etapa.split("-", 1)[0]
    return NOMBRES_ETAPA.get(base, base.capitalize())


def nombre_fase(ciclo: dict | None) -> str:
    if not ciclo:
        return "Sin datos del ciclo"
    fase = ciclo.get("fase")
    if fase is None:
        return "En espera" if ciclo.get("fin") else "Sin datos del ciclo"
    return NOMBRES_FASE.get(fase, str(fase).capitalize())


def formato_tiempo(segundos: float | None) -> str:
    """mm:ss, o h:mm:ss si pasa de una hora."""
    if segundos is None:
        return "--:--"
    s = int(max(0, segundos))
    h, resto = divmod(s, 3600)
    m, s = divmod(resto, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def minutos_restantes(segundos: float | None) -> int | None:
    if segundos is None:
        return None
    return max(1, round(segundos / 60))


def formato_tamano(n: int | float | None) -> str:
    n = float(n or 0)
    for unidad in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.0f} {unidad}" if unidad == "B" else f"{n:.1f} {unidad}"
        n /= 1024
    return f"{n:.2f} TB"


def _antiguedad(etapa: str, ahora: float) -> float:
    """Segundos desde la última modificación de los archivos de la etapa."""
    ultima = 0.0
    for sufijo in (".json", ".ffmpeg"):
        try:
            ultima = max(ultima, (mediajelly_progreso.DIR_PROGRESO / f"{etapa}{sufijo}").stat().st_mtime)
        except OSError:
            pass
    return ahora - ultima if ultima else 0.0


def _etapas_visibles(etapas: list) -> list:
    ahora = time.time()
    return [a for a in etapas
            if not (a.sin_senal and _antiguedad(a.etapa, ahora) > SEGUNDOS_HUERFANA)]


def _alerta_disco(media_root: Path) -> str | None:
    try:
        uso = shutil.disk_usage(media_root)
        if uso.total > 0 and uso.free / uso.total < MINIMO_DISCO_LIBRE:
            return f"Disco casi lleno ({uso.free / uso.total * 100:.0f} % libre)"
    except Exception:
        pass
    return None


def _leer_bd() -> dict | None:
    """Lee la BD en modo solo lectura: no crea archivos ni esquema. None si no existe."""
    with bd.conexion_solo_lectura() as conn:
        if conn is None:
            return None
        contadores = {e: 0 for e in ESTADOS}
        for st, n in conn.execute("SELECT status, COUNT(*) FROM media_files GROUP BY status"):
            if st in contadores:
                contadores[st] = n
        proximos = [Path(r[0]).name for r in conn.execute(
            "SELECT path FROM media_files WHERE status = 'pending' "
            "ORDER BY prioridad DESC, created_at ASC, path ASC LIMIT 5")]
        fila = conn.execute("SELECT data FROM progress WHERE key = 'main' LIMIT 1").fetchone()
        try:
            procesando = (json.loads(fila[0]) if fila else {}).get("processing") or {}
        except (ValueError, AttributeError):
            procesando = {}
        try:
            migracion = (conn.execute("SELECT 1 FROM completed_files LIMIT 1").fetchone() is not None
                         and conn.execute("SELECT 1 FROM meta WHERE key = 'legacy_migrated'").fetchone() is None)
        except sqlite3.OperationalError:  # sin esas tablas no hay nada que migrar
            migracion = False
        return {"contadores": contadores, "proximos": proximos,
                "procesando": procesando, "migracion": migracion}


def obtener_estado(media_root: Path = Path("/mediajelly/media")) -> dict:
    avance = mediajelly_progreso.leer()
    ciclo = avance.get("ciclo")
    resultado = {
        "ciclo": ciclo,
        "etapas": _etapas_visibles(avance.get("etapas", [])),
        "general": {"actual": 0, "total": 0, "nombre": "", "estado": ""},
        "contadores": {},
        "proximos": [],
        "ahorro_bytes": 0,
        "alertas": [],
        "error_bd": None,
    }
    alertas = resultado["alertas"]

    try:
        datos = _leer_bd()
        if datos is not None:
            contadores = datos["contadores"]
            procesando = datos["procesando"]
            stats = procesando.get("stats") or {}
            resultado["contadores"] = contadores
            resultado["proximos"] = datos["proximos"]
            resultado["general"] = {
                "actual": procesando.get("current_file") or 0,
                "total": procesando.get("total_files") or 0,
                "nombre": procesando.get("current_file_name") or "",
                "estado": procesando.get("status") or "",
            }
            resultado["ahorro_bytes"] = (stats.get("total_original_size") or 0) - (
                stats.get("total_compressed_size") or 0)
            if contadores.get("failed", 0) > 0:
                alertas.append(f"{contadores['failed']} en cuarentena")
            if datos["migracion"]:
                alertas.append("Migración pendiente")
    except Exception as exc:
        registro.warning("Lectura de la BD fallida: %s", exc)
        resultado["error_bd"] = MENSAJE_BD
        resultado["contadores"] = {}

    alerta = _alerta_disco(media_root)
    if alerta:
        alertas.append(alerta)
    try:
        fin = (ciclo or {}).get("fin")
        if fin and time.time() - float(fin) > HORAS_CICLO_VIEJO * 3600:
            alertas.append(f"Último ciclo hace {int((time.time() - float(fin)) // 3600)} h")
        desde = (ciclo or {}).get("desde")
        if ciclo and ciclo.get("fase") and not fin and desde \
                and time.time() - float(desde) > HORAS_CICLO_VIEJO * 3600:
            horas = int((time.time() - float(desde)) // 3600)
            alertas.append(f"El ciclo lleva {horas} h en {nombre_fase(ciclo)}")
    except (TypeError, ValueError):
        pass
    return resultado
