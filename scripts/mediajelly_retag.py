#!/usr/bin/env python3
"""Escribe la etiqueta de idioma de las pistas de audio dentro del archivo.

Remux sin recodificar (-c copy) a un temporal, validación y reemplazo atómico.
"""
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from mediajelly_db import normalize_lang

TIMEOUT_REMUX = 3600
ULTIMO_ERROR = ""

logger = logging.getLogger(__name__)


def _probe(path: Path) -> dict:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "stream=index,codec_type:stream_tags=language:format=duration",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, check=True, timeout=60,
    ).stdout
    return json.loads(out)


def _audio_indices(path: Path) -> List[int]:
    """Índices absolutos de las pistas de audio, en orden."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "json", str(path)],
        capture_output=True, text=True, check=True, timeout=60,
    ).stdout
    return [int(s["index"]) for s in json.loads(out).get("streams", [])]


def _audio_tags_of(data: dict) -> List[str]:
    return [
        (s.get("tags") or {}).get("language", "")
        for s in data["streams"]
        if s["codec_type"] == "audio"
    ]


def _validate(before: dict, after: dict, expected: Dict[int, str]) -> bool:
    """Mismos tipos de pista, duración casi igual y etiquetas escritas (por posición de audio)."""
    same_streams = [s["codec_type"] for s in before["streams"]] == [s["codec_type"] for s in after["streams"]]
    same_duration = abs(float(before["format"]["duration"]) - float(after["format"]["duration"])) < 1.0
    tags = _audio_tags_of(after)
    tagged = all(pos < len(tags) and tags[pos] == lang for pos, lang in expected.items())
    if not (same_streams and same_duration and tagged):
        logger.warning(f"⚠️ Validación del re-etiquetado falló (pistas={same_streams} duración={same_duration} etiqueta={tagged})")
    return same_streams and same_duration and tagged


def _format_for(path: Path) -> str:
    return "matroska" if path.suffix.lower() in (".mkv", ".mka", ".webm") else "mp4"


def _clean_orphans(path: Path) -> None:
    for orphan in path.parent.glob(".*.retag.tmp"):
        try:
            orphan.unlink()
            logger.info(f"🧹 Temporal huérfano eliminado: {orphan.name}")
        except OSError:
            pass


def _retag(path: Path, languages: Dict[int, str]) -> Tuple[bool, str]:
    wanted = {}
    for i, l in languages.items():
        lang = normalize_lang(l)
        if lang != "und":
            wanted[int(i)] = lang
    if not wanted:
        return False, "sin idiomas que escribir"

    tmp: Optional[Path] = None
    try:
        _clean_orphans(path)
        absolute = _audio_indices(path)
        expected: Dict[int, str] = {}
        for idx, lang in wanted.items():
            if idx not in absolute:
                logger.warning(f"⚠️ Stream {idx} no es de audio en {path.name}; se omite")
                continue
            expected[absolute.index(idx)] = lang
        if not expected:
            return False, "ningún stream de audio coincide"

        tmp = path.with_name(f".{path.name}.retag.tmp")
        cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-y", "-i", str(path),
               # Sin -map 0:d: la pista de datos es la de capítulos y ffmpeg la regenera
               "-map", "0:v", "-map", "0:a", "-map", "0:s?", "-c", "copy"]
        for pos, lang in sorted(expected.items()):
            cmd += [f"-metadata:s:a:{pos}", f"language={lang}"]
        cmd += ["-movflags", "+faststart", "-f", _format_for(path), str(tmp)]

        res = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_REMUX)
        if res.returncode != 0:
            msg = f"ffmpeg falló: {res.stderr.strip()[-300:]}"
            logger.warning(f"⚠️ {msg} ({path.name})")
            tmp.unlink(missing_ok=True)
            return False, msg

        if not _validate(_probe(path), _probe(tmp), expected):
            tmp.unlink(missing_ok=True)
            return False, "validación fallida"

        st = path.stat()
        os.chown(tmp, st.st_uid, st.st_gid)
        os.chmod(tmp, st.st_mode)
        os.replace(tmp, path)
        logger.info(f"🏷️ Idioma escrito en {path.name}: {expected}")
        return True, ""
    except subprocess.TimeoutExpired:
        logger.warning(f"⚠️ Tiempo agotado re-etiquetando {path.name}")
        if tmp is not None:
            tmp.unlink(missing_ok=True)
        return False, "tiempo agotado"
    except Exception as e:
        logger.warning(f"⚠️ No se pudo re-etiquetar {path}: {e}")
        if tmp is not None:
            tmp.unlink(missing_ok=True)
        return False, str(e)


def retag_audio(path: Path, languages: Dict[int, str]) -> bool:
    """Escribe el idioma en las pistas de audio del archivo.

    Args:
        path: archivo a modificar.
        languages: {índice absoluto de stream: código de idioma}. Se normalizan; los 'und' se ignoran.

    Returns:
        True si el archivo fue reemplazado; False si no había nada que hacer o algo falló
        (el original queda intacto). El motivo del último fallo queda en ULTIMO_ERROR.
    """
    global ULTIMO_ERROR
    ok, err = _retag(Path(path), languages)
    ULTIMO_ERROR = err
    return ok
