#!/usr/bin/env python3
"""Pasa los subtítulos de la biblioteca al esquema "{stem}.{idioma}.srt".

1. Borra "{stem}.srt" si es idéntico a "{stem}.es.srt" (copia duplicada).
2. Renombra cualquier otro "{stem}.srt" a "{stem}.{idioma}.srt" según el texto.
3. Renombra "{stem}.hi.srt" en inglés a "{stem}.en.sdh.srt" (Jellyfin lo mostraba como hindi).
4. No sobrescribe nunca: si el destino existe, lo omite y lo informa.

Con --auditar-desfase también lista los videos cuyo audio difiere del video en más
de DESFASE_MAX_SEGUNDOS y, con --aplicar, marca el aviso (sin Telegram).

Uso:
    python3 mediajelly_renombrar_subs.py RUTA                 # solo muestra el plan
    python3 mediajelly_renombrar_subs.py RUTA --aplicar
    python3 mediajelly_renombrar_subs.py RUTA --auditar-desfase
"""

import argparse
import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from mediajelly_calidad_subs import DESFASE_MAX_SEGUNDOS, desfase_audio
from mediajelly_politica import idioma_639_1
from mediajelly_utils import MediaJellyPaths


# Idiomas que hay en la biblioteca. Otro código detectado ("ca" en un SRT en inglés)
# suele ser un error del detector: se omite en vez de renombrar mal.
IDIOMAS_PLAUSIBLES = {"en", "es", "ja", "zh", "ko", "fr", "pt", "de", "it", "ru"}


@dataclass
class Accion:
    """Cambio planeado sobre un subtítulo.

    Attributes:
        tipo: "borrar", "renombrar" u "omitir".
        origen: Subtítulo afectado.
        destino: Nuevo nombre (solo al renombrar).
        motivo: Explicación para el reporte.
    """

    tipo: str
    origen: Path
    destino: Optional[Path]
    motivo: str


def _videos(carpeta: Path) -> List[Path]:
    return sorted(
        p
        for p in carpeta.rglob("*")
        if p.is_file() and MediaJellyPaths.is_video_file(p) and not MediaJellyPaths.is_excluded_folder(p)
    )


def _renombrar(origen: Path, destino: Path, motivo: str, reservados: set) -> Accion:
    if destino.exists() or destino in reservados:
        return Accion("omitir", origen, destino, "destino existe")
    reservados.add(destino)
    return Accion("renombrar", origen, destino, motivo)


def planear(carpeta: Path, detectar: Callable[[Path], str]) -> List[Accion]:
    """Arma la lista de cambios sin tocar nada.

    Args:
        carpeta: Carpeta de la biblioteca (se recorre completa).
        detectar: Devuelve el idioma del texto de un .srt ("en", "es", "unknown"...).

    Returns:
        Acciones en el orden en que se aplicarían.
    """
    acciones: List[Accion] = []
    reservados: set = set()
    for video in _videos(carpeta):
        generico = video.with_suffix(".srt")
        espanol = video.with_suffix(".es.srt")
        if generico.exists():
            if espanol.exists() and generico.read_bytes() == espanol.read_bytes():
                acciones.append(Accion("borrar", generico, None, "idéntico a .es.srt"))
            else:
                codigo = idioma_639_1(detectar(generico))
                if codigo is None:
                    acciones.append(Accion("omitir", generico, None, "idioma desconocido"))
                elif codigo not in IDIOMAS_PLAUSIBLES:
                    acciones.append(Accion("omitir", generico, None, f"idioma dudoso ({codigo})"))
                else:
                    destino = video.parent / f"{video.stem}.{codigo}.srt"
                    acciones.append(_renombrar(generico, destino, f"idioma {codigo}", reservados))
        hi = video.parent / f"{video.stem}.hi.srt"
        if hi.exists() and idioma_639_1(detectar(hi)) == "en":
            destino = video.parent / f"{video.stem}.en.sdh.srt"
            acciones.append(_renombrar(hi, destino, "inglés para sordos", reservados))
    return acciones


def aplicar(acciones: Sequence[Accion]) -> Dict[str, int]:
    """Ejecuta las acciones; un error en un archivo se informa y se sigue con el siguiente.

    Returns:
        Conteo por tipo, más "error".
    """
    conteo = {"borrar": 0, "renombrar": 0, "omitir": 0, "error": 0}
    for accion in acciones:
        try:
            if accion.tipo == "borrar":
                accion.origen.unlink()
            elif accion.tipo == "renombrar" and accion.destino is not None:
                if accion.destino.exists():  # apareció después de planear
                    conteo["omitir"] += 1
                    continue
                accion.origen.rename(accion.destino)
            conteo[accion.tipo] += 1
        except OSError as e:
            conteo["error"] += 1
            print(f"ERROR {accion.origen}: {e}")
    return conteo


def probar_ffprobe(video: Path) -> Optional[dict]:
    """Salida JSON de ffprobe con las duraciones del formato y de cada pista."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=codec_type,duration",
        "-of",
        "json",
        str(video),
    ]
    try:
        salida = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return json.loads(salida.stdout) if salida.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def auditar_desfase(
    carpeta: Path, probar: Callable[[Path], Optional[dict]], marcar: bool = False
) -> List[Tuple[Path, float]]:
    """Videos cuyo audio difiere del video en más de DESFASE_MAX_SEGUNDOS.

    Args:
        carpeta: Carpeta de la biblioteca.
        probar: Devuelve la salida de ffprobe de un video.
        marcar: Si True, marca el aviso "audio_desfasado" de los videos registrados.

    Returns:
        (video, desfase en segundos) de cada video desfasado.
    """
    desfasados = []
    for video in _videos(carpeta):
        desfase = desfase_audio(probar(video))
        if desfase is None or abs(desfase) <= DESFASE_MAX_SEGUNDOS:
            continue
        desfasados.append((video, desfase))
        if marcar:
            from mediajelly_db import get_by_path, marcar_aviso

            registro = get_by_path(video)
            if registro is not None:
                marcar_aviso(registro.fingerprint, "audio_desfasado", f"audio {desfase:+.0f} s respecto al video")
    return desfasados


def _detector() -> Callable[[Path], str]:
    from mediajelly_subtitle_translator import SubtitleTranslator

    traductor = SubtitleTranslator.__new__(SubtitleTranslator)
    traductor.logger = logging.getLogger("renombrar_subs")
    return traductor.detect_srt_language


def main(
    argv: Optional[Sequence[str]] = None,
    detectar: Optional[Callable[[Path], str]] = None,
    probar: Callable[[Path], Optional[dict]] = probar_ffprobe,
) -> int:
    """Punto de entrada de la línea de comandos."""
    parser = argparse.ArgumentParser(description="Renombra los subtítulos al idioma y audita el audio desfasado")
    parser.add_argument("ruta", type=Path)
    parser.add_argument("--aplicar", action="store_true", help="Ejecutar los cambios (sin esto solo muestra el plan)")
    parser.add_argument("--auditar-desfase", action="store_true", help="Listar videos con el audio desfasado")
    args = parser.parse_args(argv)

    acciones = planear(args.ruta, detectar or _detector())
    for accion in acciones:
        destino = f" -> {accion.destino.name}" if accion.destino else ""
        print(f"{accion.tipo.upper():9} {accion.origen}{destino} ({accion.motivo})")
    if args.aplicar:
        conteo = aplicar(acciones)
    else:
        conteo = {t: sum(1 for a in acciones if a.tipo == t) for t in ("borrar", "renombrar", "omitir")}
    print("Resumen:", ", ".join(f"{k}: {v}" for k, v in conteo.items()), "" if args.aplicar else "(sin cambios)")

    if args.auditar_desfase:
        desfasados = auditar_desfase(args.ruta, probar, marcar=args.aplicar)
        for video, desfase in desfasados:
            print(f"DESFASE   {video} ({desfase:+.1f} s)")
        print(f"Audio desfasado: {len(desfasados)} video(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
