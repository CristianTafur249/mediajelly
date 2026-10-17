#!/usr/bin/env python3
"""Vuelve a encolar videos para que el procesador los procese en el próximo ciclo.

Reemplaza a borrar líneas de completed.txt, que desde la migración ya no se usa.

Uso (dentro del contenedor):
    python3 mediajelly_requeue.py <archivo o carpeta> [...]
Una carpeta encola todos sus videos, incluidas las subcarpetas.
"""
import sys
from pathlib import Path
from typing import Iterable, List

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mediajelly_db as db  # noqa: E402
from mediajelly_utils import MediaJellyPaths  # noqa: E402

EXTENSIONES_VIDEO = {".mp4", ".mkv", ".avi", ".webm", ".m4v", ".mov", ".ts", ".wmv", ".flv"}


def _videos(rutas: Iterable[Path]) -> List[Path]:
    encontrados: List[Path] = []
    for ruta in rutas:
        ruta = Path(ruta)
        if ruta.is_dir():
            encontrados += sorted(
                p for p in ruta.rglob("*")
                if p.is_file() and p.suffix.lower() in EXTENSIONES_VIDEO
                and not any(parte.startswith(".") for parte in p.relative_to(ruta).parts)
                and not any(parte in MediaJellyPaths.EXCLUDED_FOLDERS for parte in p.parts)
            )
        elif ruta.is_file():
            encontrados.append(ruta)
        else:
            print(f"No existe: {ruta}")
    return encontrados


def requeue(rutas: Iterable[Path]) -> int:
    """Pasa a pending los videos indicados (archivos o carpetas). Devuelve cuántos encoló."""
    encolados = 0
    for video in _videos(rutas):
        record, _ = db.register(video, require_settled=False)
        if record is None:
            print(f"No se pudo leer: {video}")
            continue
        db.set_status(record.fingerprint, "pending")
        encolados += 1
        print(f"Encolado: {video}")
    return encolados


def main(argv: List[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    total = requeue(Path(a) for a in argv)
    print(f"Videos encolados: {total}. Se procesan en el próximo ciclo del cron.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
