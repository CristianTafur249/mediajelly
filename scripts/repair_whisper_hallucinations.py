#!/usr/bin/env python3
"""
Quita las alucinaciones de Whisper de los subtítulos ya transcritos.

Solo revisa los videos que según los logs del traductor salieron de Whisper
(ver requeue_whisper_subtitles.read_whisper_stems): su .srt, su .{idioma}.srt
y su .es.srt. Las
reglas están en mediajelly_alucinaciones.

Uso:
    python3 repair_whisper_hallucinations.py            # solo informa
    python3 repair_whisper_hallucinations.py --write    # corrige los archivos
"""

import argparse
import glob
from pathlib import Path
from typing import Dict, List

from mediajelly_alucinaciones import filtrar_srt
from mediajelly_politica import idioma_de_subtitulo
from mediajelly_utils import MediaJellyPaths
from requeue_whisper_subtitles import read_whisper_stems


def subtitulos_whisper(media_dir: Path, stems: set) -> List[Path]:
    """El .srt, los .{idioma}.srt y el .es.srt de cada video transcrito con Whisper."""
    encontrados: Dict[Path, None] = {}
    for video in media_dir.rglob("*"):
        if video.stem not in stems or not MediaJellyPaths.is_video_file(video):
            continue
        if MediaJellyPaths.is_excluded_folder(video):
            continue
        if video.with_suffix(".srt").exists():
            encontrados[video.with_suffix(".srt")] = None
        for srt in video.parent.glob(f"{glob.escape(video.stem)}.*.srt"):
            if idioma_de_subtitulo(srt.name, video.stem):
                encontrados[srt] = None
    return sorted(encontrados)


def main():
    parser = argparse.ArgumentParser(description="Quita alucinaciones de Whisper de los subtítulos")
    parser.add_argument("--write", action="store_true", help="Corregir los archivos (sin esto solo informa)")
    args = parser.parse_args()

    stems = read_whisper_stems(MediaJellyPaths.get_tmp_dir() / "logs")
    archivos = subtitulos_whisper(MediaJellyPaths.get_media_dir(), stems)

    afectados = 0
    total = 0
    for srt in archivos:
        try:
            contenido = srt.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            print(f"No se pudo leer {srt}: {e}")
            continue
        limpio, cambios = filtrar_srt(contenido)
        if not cambios:
            continue
        afectados += 1
        total += cambios
        print(f"{cambios:5d}  {srt.relative_to(MediaJellyPaths.get_media_dir())}")
        if args.write:
            try:
                tmp = srt.with_suffix(srt.suffix + ".tmp")
                tmp.write_text(limpio, encoding="utf-8")
                tmp.replace(srt)
            except OSError as e:
                print(f"No se pudo escribir {srt}: {e}")

    accion = "corregidos" if args.write else "por corregir"
    print(f"Subtítulos de Whisper revisados: {len(archivos)}")
    print(f"Con alucinaciones: {afectados} ({total} cues {accion})")
    if not args.write and afectados:
        print("Ejecuta con --write para corregirlos.")


if __name__ == "__main__":
    main()
