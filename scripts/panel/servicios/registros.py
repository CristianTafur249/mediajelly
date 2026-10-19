"""Final de los logs del sistema para la página de Registros."""
from pathlib import Path

DIR_REGISTROS = Path(__file__).resolve().parents[2] / "tmp" / "logs"
REGISTROS = {
    "ciclo": "cron_runner.log",
    "compresion": "compression-success.log",
    "errores": "compression-errors.log",
    "idiomas": "language-detection.log",
    "subtitulos": "subtitle_translator.log",
    "tareas": "tareas.log",
}
# Nombre visible de cada registro en la página (por defecto, la clave con mayúscula)
ETIQUETAS = {"compresion": "Compresión", "subtitulos": "Subtítulos"}
BLOQUE = 64 * 1024
MAX_BYTES = 8 * 1024 * 1024  # tope de lectura hacia atrás cuando el filtro casi no coincide


def leer_registro(clave: str, lineas: int = 300, filtro: str = "") -> list[str]:
    """Últimas `lineas` líneas del log (las que coinciden con `filtro`, sin distinguir mayúsculas).

    Lee hacia atrás en bloques de 64 KiB, sin cargar el archivo completo.
    Una clave desconocida lanza KeyError antes de tocar el sistema de archivos.
    """
    nombre = REGISTROS[clave]
    ruta = DIR_REGISTROS / nombre
    if lineas <= 0 or not ruta.is_file():
        return []
    filtro = filtro.lower()
    objetivo = lineas + 1  # una de más: la última puede ser el salto de línea final
    encontradas: list[str] = []
    resto = b""
    leido = 0
    with open(ruta, "rb") as f:
        posicion = f.seek(0, 2)
        while posicion > 0 and len(encontradas) < objetivo and leido < MAX_BYTES:
            paso = min(BLOQUE, posicion)
            posicion -= paso
            f.seek(posicion)
            datos = f.read(paso)
            leido += len(datos)
            partes = (datos + resto).split(b"\n")
            # la primera parte puede estar cortada: se completa con el bloque anterior
            resto = partes[0] if posicion > 0 else b""
            completas = partes[1:] if posicion > 0 else partes
            for linea in reversed(completas):
                texto = linea.decode("utf-8", errors="replace").rstrip("\r")
                if filtro and filtro not in texto.lower():
                    continue
                encontradas.append(texto)
                if len(encontradas) >= objetivo:
                    break
    if encontradas and encontradas[0] == "" and not filtro:
        encontradas.pop(0)  # el salto de línea final no es una línea
    return encontradas[:lineas][::-1]
