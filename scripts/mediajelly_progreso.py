"""Avance de los procesos largos (compresión, transcripción, detección de idiomas).

Los procesos escriben aquí y el panel lee. Todo en archivos pequeños dentro de
DIR_PROGRESO, escritos de forma atómica. Solo biblioteca estándar.
"""
import fcntl
import json
import math
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

DIR_PROGRESO = Path(__file__).parent / "tmp" / "progreso"

SEGUNDOS_SIN_SENAL = 60
# El -progress de ffmpeg crece ~1,5 MB por hora de video y el panel lo lee cada 2 s
# por cliente: solo se lee el final (cada bloque ocupa unos 250 bytes).
BYTES_FINAL_FFMPEG = 8 * 1024


@dataclass
class Avance:
    etapa: str
    archivo: str
    detalle: str
    porcentaje: float | None
    posicion_s: float | None
    duracion_s: float | None
    velocidad: float | None
    restante_s: float | None
    sin_senal: bool


def _escribir_json(ruta: Path, datos: dict) -> None:
    """Escritura atómica: .tmp con nombre único y después os.replace."""
    ruta.parent.mkdir(parents=True, exist_ok=True)
    fd, nombre_tmp = tempfile.mkstemp(dir=ruta.parent, prefix=f".{ruta.stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(datos))
        os.replace(nombre_tmp, ruta)
    except BaseException:
        try:
            os.unlink(nombre_tmp)
        except OSError:
            pass
        raise


@contextmanager
def _candado(etapa: str):
    """Exclusión entre procesos para iniciar/actualizar/terminar de una etapa."""
    DIR_PROGRESO.mkdir(parents=True, exist_ok=True)
    with open(DIR_PROGRESO / f".{etapa}.lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _borrar(ruta: Path) -> None:
    try:
        ruta.unlink()
    except FileNotFoundError:
        pass


def iniciar(etapa: str, archivo: str, duracion_s: float | None = None, detalle: str = "") -> None:
    try:
        with _candado(etapa):
            _borrar(DIR_PROGRESO / f"{etapa}.ffmpeg")
            _escribir_json(DIR_PROGRESO / f"{etapa}.json", {
                "etapa": etapa,
                "archivo": archivo,
                "duracion_s": duracion_s,
                "detalle": detalle,
                "inicio": time.time(),
                "posicion_s": 0,
            })
    except OSError:
        pass  # el avance es accesorio: nunca debe tumbar al proceso


def actualizar(etapa: str, posicion_s: float | None = None, detalle: str | None = None) -> None:
    ruta = DIR_PROGRESO / f"{etapa}.json"
    try:
        with _candado(etapa):
            # se vuelve a leer dentro del candado: si terminar() ya borró la etapa, no se revive
            try:
                datos = json.loads(ruta.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return
            if not isinstance(datos, dict):
                return
            if posicion_s is not None:
                datos["posicion_s"] = posicion_s
            if detalle is not None:
                datos["detalle"] = detalle
            _escribir_json(ruta, datos)
    except OSError:
        pass


def terminar(etapa: str) -> None:
    try:
        with _candado(etapa):
            _borrar(DIR_PROGRESO / f"{etapa}.json")
            _borrar(DIR_PROGRESO / f"{etapa}.ffmpeg")
    except OSError:
        pass


def ffmpeg_args(etapa: str) -> list[str]:
    return ["-progress", str(DIR_PROGRESO / f"{etapa}.ffmpeg"), "-nostats"]


def fase(nombre: str | None) -> None:
    ruta = DIR_PROGRESO / "ciclo.json"
    try:
        if nombre is None:
            _escribir_json(ruta, {"fase": None, "fin": time.time()})
        else:
            _escribir_json(ruta, {"fase": nombre, "desde": time.time()})
    except OSError:
        pass


def _ultimo_bloque_ffmpeg(texto: str) -> dict[str, str]:
    """Último bloque clave=valor completo (cerrado por progress=...).

    Si el último quedó cortado a medias se usa el anterior.
    """
    bloque: dict[str, str] = {}
    ultimo: dict[str, str] = {}
    for linea in texto.splitlines():
        if "=" not in linea:
            continue
        clave, valor = linea.split("=", 1)
        bloque[clave.strip()] = valor.strip()
        if clave.strip() == "progress":
            ultimo = bloque
            bloque = {}
    return ultimo


def _final_ffmpeg(ruta: Path) -> str:
    """Últimos BYTES_FINAL_FFMPEG bytes del archivo, sin la primera línea si quedó cortada."""
    with open(ruta, "rb") as f:
        tamano = f.seek(0, os.SEEK_END)
        inicio = max(0, tamano - BYTES_FINAL_FFMPEG)
        f.seek(inicio)
        datos = f.read(BYTES_FINAL_FFMPEG)
    if inicio > 0:
        corte = datos.find(b"\n")
        datos = datos[corte + 1:] if corte >= 0 else b""
    return datos.decode("utf-8", errors="replace")


def _numero(valor) -> float | None:
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None
    return numero if math.isfinite(numero) else None


def _leer_etapa(ruta_json: Path, ahora: float) -> Avance | None:
    try:
        datos = json.loads(ruta_json.read_text(encoding="utf-8"))
        if not isinstance(datos, dict):
            return None
        etapa = str(datos.get("etapa") or ruta_json.stem)
        mtime = ruta_json.stat().st_mtime
        posicion = _numero(datos.get("posicion_s"))
        duracion = _numero(datos.get("duracion_s"))
        velocidad = None

        ruta_ffmpeg = ruta_json.with_suffix(".ffmpeg")
        try:
            texto = _final_ffmpeg(ruta_ffmpeg)
            mtime = max(mtime, ruta_ffmpeg.stat().st_mtime)
        except OSError:
            texto = ""
        bloque = _ultimo_bloque_ffmpeg(texto)
        if bloque:
            us = _numero(bloque.get("out_time_us"))
            if us is None:
                ms = _numero(bloque.get("out_time_ms"))
                us = ms  # en ffmpeg out_time_ms también viene en microsegundos
            if us is not None:
                posicion = max(us, 0) / 1_000_000
            if bloque.get("progress") == "end" and duracion and duracion > 0:
                posicion = max(posicion or 0, duracion)  # ffmpeg terminó: 100 %
            velocidad = _numero(bloque.get("speed", "").rstrip("x"))
            if velocidad is not None and velocidad <= 0:
                velocidad = None

        porcentaje = None
        if duracion and duracion > 0 and posicion is not None:
            porcentaje = min(100.0, posicion / duracion * 100)
        restante = None
        if velocidad and duracion is not None and posicion is not None:
            restante = max(0.0, (duracion - posicion) / velocidad)

        return Avance(
            etapa=etapa,
            archivo=str(datos.get("archivo") or ""),
            detalle=str(datos.get("detalle") or ""),
            porcentaje=porcentaje,
            posicion_s=posicion,
            duracion_s=duracion,
            velocidad=velocidad,
            restante_s=restante,
            sin_senal=ahora - mtime > SEGUNDOS_SIN_SENAL,
        )
    except Exception:
        return None


def leer() -> dict:
    """Estado actual: {"ciclo": dict | None, "etapas": [Avance, ...]}. Nunca lanza."""
    resultado: dict = {"ciclo": None, "etapas": []}
    try:
        try:
            ciclo = json.loads((DIR_PROGRESO / "ciclo.json").read_text(encoding="utf-8"))
            if isinstance(ciclo, dict):
                resultado["ciclo"] = ciclo
        except (OSError, ValueError):
            pass
        ahora = time.time()
        for ruta in sorted(DIR_PROGRESO.glob("*.json")):
            if ruta.name == "ciclo.json" or ruta.name.startswith("."):
                continue
            avance = _leer_etapa(ruta, ahora)
            if avance is not None:
                resultado["etapas"].append(avance)
    except Exception:
        pass
    return resultado
