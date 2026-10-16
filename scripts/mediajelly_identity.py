"""Huella de contenido de videos, estable ante renombrados."""

import hashlib
import time
from pathlib import Path
from typing import Optional

CHUNK_SIZE = 1024 * 1024


def fingerprint(path: Path) -> str:
    """Devuelve "<tamaño>:<sha1 hex>" sobre inicio, medio y final (o todo si ≤ 3 MiB)."""
    path = Path(path)
    size = path.stat().st_size
    digest = hashlib.sha1()
    with open(path, "rb") as f:
        if size <= 3 * CHUNK_SIZE:
            digest.update(f.read())
        else:
            for offset in (0, size // 2 - CHUNK_SIZE // 2, size - CHUNK_SIZE):
                f.seek(offset)
                digest.update(f.read(CHUNK_SIZE))
    return f"{size}:{digest.hexdigest()}"


def is_settled(path: Path, min_age_seconds: int = 120, now: Optional[float] = None) -> bool:
    """True si el archivo fue modificado hace al menos min_age_seconds."""
    try:
        mtime = Path(path).stat().st_mtime
    except OSError:
        return False
    if now is None:
        now = time.time()
    return (now - mtime) >= min_age_seconds
