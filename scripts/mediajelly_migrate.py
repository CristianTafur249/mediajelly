#!/usr/bin/env python3
"""
Migración de un solo uso al estado por huella de contenido.

Importa el estado anterior (tabla completed_files, failed-compression.txt y
language_cache.json) a media_files y stream_languages, identificando cada
video de --media-root por su huella.

Uso:
    python3 mediajelly_migrate.py                 # simulación: no escribe nada
    python3 mediajelly_migrate.py --apply         # respalda y aplica

Reglas de estado (en orden):
    1. done si la ruta está en el historial (completed_files + completed.txt).
    2. done si su episode_key coincide con un completado de la misma carpeta que
       ya no existe en disco y es el único candidato de esa carpeta (dudoso).
    3. failed si está en failed-compression.txt.
    4. pending en el resto.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Union

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mediajelly_db  # noqa: E402
from mediajelly_emoji import EmojiGenerator  # noqa: E402
from mediajelly_identity import fingerprint  # noqa: E402
from mediajelly_utils import MediaJellyPaths  # noqa: E402

DEFAULT_MEDIA_ROOT = "/mediajelly/media"
WRONG_ROOT_MESSAGE = "Las rutas guardadas no coinciden con --media-root; ¿se está corriendo fuera del contenedor?"
BACKUP_FILES = ("mediajelly.db", "completed.txt", "failed-compression.txt", "language_cache.json", "pending_subtitles.txt")
LANGUAGE_SOURCE = "cache"

logger = logging.getLogger("mediajelly_migrate")


class MigrationError(Exception):
    """Condición que impide migrar con seguridad; main la muestra y aborta."""

EpisodeKey = Union[Tuple[str, int, int], str]


@dataclass
class MigrationPlan:
    rows: List[Tuple[str, str, str]] = field(default_factory=list)  # (ruta, huella, estado)
    languages: Dict[str, Dict[int, str]] = field(default_factory=dict)  # ruta → {índice: idioma}
    doubtful: List[str] = field(default_factory=list)
    unmatched_prefix: int = 0
    completed_total: int = 0
    unreadable: List[str] = field(default_factory=list)
    language_conflicts: List[str] = field(default_factory=list)
    failed_rows: List[str] = field(default_factory=list)
    tmp_dir: Optional[Path] = None


def episode_key(name: str) -> EpisodeKey:
    """Serie + temporada + episodio en minúsculas; en películas, el título sin el año y lo que sigue."""
    m = re.search(r"^(.*?)\s*-?\s*S(\d+)E(\d+)", name)
    if m:
        return (m.group(1).strip(" -").lower(), int(m.group(2)), int(m.group(3)))
    return re.sub(r"\(\d{4}\).*", "", name).strip().lower()


def _find_videos(media_root: Path) -> List[Path]:
    """Videos bajo media_root con los mismos filtros que el escáner, sin carpetas ocultas."""
    found: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(media_root):
        dirnames[:] = sorted(
            d for d in dirnames if not d.startswith(".") and d.lower() not in MediaJellyPaths.EXCLUDED_FOLDERS
        )
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if (
                MediaJellyPaths.is_video_file(path)
                and not name.startswith(".")
                and not name.endswith(".compressed.mp4")
                and not MediaJellyPaths.is_excluded_folder(path)
            ):
                found.append(path)
    return found


def _read_completed(tmp_dir: Path) -> List[str]:
    """Historial de completados: completed_files (BD en solo lectura) + completed.txt.

    Lanza MigrationError si la BD o la tabla no existen o no se pueden leer:
    sin historial, todo quedaría pending y se recomprimiría la biblioteca.
    """
    db_path = mediajelly_db._get_db_path()
    if not db_path.exists():
        raise MigrationError(f"No existe la BD {db_path}: no se puede leer el historial de completados")
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
        try:
            rows = [r[0] for r in conn.execute("SELECT path FROM completed_files") if r[0]]
        finally:
            conn.close()
    except sqlite3.Error as e:
        raise MigrationError(f"No se pudo leer el historial de completados (completed_files en {db_path}): {e}") from e

    completed_txt = tmp_dir / "completed.txt"
    if completed_txt.exists():
        rows += [line.strip() for line in completed_txt.read_text(encoding="utf-8", errors="replace").splitlines()]
    return list(dict.fromkeys(p for p in rows if p))


def _read_failed(tmp_dir: Path) -> Set[str]:
    failed_file = tmp_dir / "failed-compression.txt"
    if not failed_file.exists():
        return set()
    paths = set()
    for line in failed_file.read_text(encoding="utf-8", errors="replace").splitlines():
        path = line.split(" | ", 1)[0].strip()
        if path:
            paths.add(path)
    return paths


def _read_language_cache(tmp_dir: Path) -> Dict[str, Dict[int, str]]:
    """{clave original: {índice: idioma normalizado}} sin idiomas no detectados."""
    cache_file = tmp_dir / "language_cache.json"
    if not cache_file.exists():
        return {}
    try:
        raw = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning(f"{EmojiGenerator.warning()} No se pudo leer {cache_file}: {e}")
        return {}
    result: Dict[str, Dict[int, str]] = {}
    for key, streams in (raw or {}).items():
        if not isinstance(streams, dict):
            continue
        langs: Dict[int, str] = {}
        for idx, lang in streams.items():
            if not isinstance(lang, str):
                continue
            try:
                index = int(idx)
            except (TypeError, ValueError):
                continue
            norm = mediajelly_db.normalize_lang(lang)
            if norm != "und":
                langs[index] = norm
        if langs:
            result[key] = langs
    return result


def _relative_key(key: str, media_root: Path) -> str:
    """Ruta relativa a la raíz de medios de una clave de la caché (absoluta o relativa)."""
    root = str(media_root).rstrip("/") + "/"
    if key.startswith(root):
        return key[len(root):]
    for marker in ("/mediajelly/media/", "/media/"):
        if marker in key:
            return key.split(marker, 1)[1]
    return key.lstrip("/")


def _unique_by_folder(videos: List[Path]) -> Dict[Tuple[str, EpisodeKey], Path]:
    """(carpeta, episode_key) → video, solo para las claves con un único candidato en su carpeta."""
    groups: Dict[Tuple[str, EpisodeKey], List[Path]] = {}
    for video in videos:
        groups.setdefault((str(video.parent), episode_key(video.name)), []).append(video)
    return {k: v[0] for k, v in groups.items() if len(v) == 1}


def build_plan(media_root: Path, tmp_dir: Path) -> MigrationPlan:
    """Calcula el estado nuevo de cada video sin escribir en la BD.

    Lanza MigrationError si el historial no se puede leer o si la mayoría de
    sus rutas no están bajo media_root (antes de recorrer y calcular huellas).
    """
    media_root = Path(media_root)
    tmp_dir = Path(tmp_dir)
    plan = MigrationPlan(tmp_dir=tmp_dir)

    completed = _read_completed(tmp_dir)
    root_prefix = str(media_root).rstrip("/") + "/"
    plan.completed_total = len(completed)
    plan.unmatched_prefix = sum(1 for p in completed if not p.startswith(root_prefix))
    if plan.completed_total and plan.unmatched_prefix * 2 > plan.completed_total:
        raise MigrationError(WRONG_ROOT_MESSAGE)
    completed_set = set(completed)
    failed = _read_failed(tmp_dir)

    videos = _find_videos(media_root)
    unique = _unique_by_folder(videos)
    by_rel = {str(v.relative_to(media_root)): v for v in videos}

    # Coincidencia por episodio: completado de la misma carpeta, ya ausente, con un único candidato.
    episode_done: Set[str] = set()
    for p in completed:
        old = Path(p)
        if old.exists():
            continue
        candidate = unique.get((str(old.parent), episode_key(old.name)))
        # El procesador siempre produce .mp4: otro formato es una descarga nueva, no una salida procesada.
        if candidate is not None and candidate.suffix.lower() == ".mp4":
            episode_done.add(str(candidate))

    # Idiomas: por ruta; si no, por episodio con la misma restricción (entrada huérfana de la misma carpeta).
    cache = _read_language_cache(tmp_dir)
    langs_by_path: Dict[str, Dict[int, str]] = {}
    orphans: Dict[str, List[Dict[int, str]]] = {}
    for key, langs in cache.items():
        rel = _relative_key(key, media_root)
        video = by_rel.get(rel)
        if video is not None:
            langs_by_path.setdefault(str(video), {}).update(langs)
            continue
        old = media_root / rel
        if old.exists():
            continue
        candidate = unique.get((str(old.parent), episode_key(old.name)))
        if candidate is not None:
            orphans.setdefault(str(candidate), []).append(langs)

    for video in videos:
        path_s = str(video)
        try:
            fp = fingerprint(video)
        except OSError as e:
            logger.warning(f"{EmojiGenerator.warning()} No se pudo leer {path_s}: {e}")
            plan.unreadable.append(path_s)
            continue

        if path_s in completed_set:
            status = "done"
        elif path_s in episode_done:
            status = "done"
            plan.doubtful.append(path_s)
        elif path_s in failed:
            status = "failed"
        else:
            status = "pending"
        plan.rows.append((path_s, fp, status))

        langs = langs_by_path.get(path_s)
        if langs is None and path_s in orphans:
            candidates = orphans[path_s]
            if all(c == candidates[0] for c in candidates):
                langs = candidates[0]
            else:
                plan.language_conflicts.append(path_s)
        if langs:
            plan.languages[path_s] = dict(langs)

    return plan


def _backup(tmp_dir: Path, backup_dir: Path) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    db_path = mediajelly_db._get_db_path()
    for name in BACKUP_FILES:
        src = db_path if name == "mediajelly.db" else tmp_dir / name
        dst = backup_dir / name
        if not src.exists():
            logger.warning(f"{EmojiGenerator.warning()} No existe {src}; se omite del respaldo")
            continue
        if name == "mediajelly.db":
            # Copia consistente aunque haya cambios en el WAL
            source = sqlite3.connect(str(src), timeout=30)
            target = sqlite3.connect(str(dst))
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
        else:
            shutil.copy2(src, dst)
    logger.info(f"{EmojiGenerator.folder()} Respaldo guardado en {backup_dir}")


def apply_plan(plan: MigrationPlan, backup_dir: Path) -> None:
    """Respalda el estado anterior y escribe el plan en la BD (idempotente)."""
    tmp_dir = plan.tmp_dir or mediajelly_db._get_db_path().parent
    _backup(tmp_dir, Path(backup_dir))

    plan.failed_rows = []
    for path_s, planned_fp, status in plan.rows:
        try:
            _apply_row(plan, path_s, planned_fp, status)
        except Exception as e:
            logger.error(f"{EmojiGenerator.warning()} Error migrando {path_s}: {e}; se sigue con el resto")
            plan.failed_rows.append(path_s)
    if plan.failed_rows:
        print(f"{EmojiGenerator.warning()} {len(plan.failed_rows)} fila(s) fallaron:")
        for path in plan.failed_rows:
            print(f"  ! {path}")


def _apply_row(plan: MigrationPlan, path_s: str, planned_fp: str, status: str) -> None:
    rec, event = mediajelly_db.register(path_s, require_settled=False)
    if rec is None:
        logger.warning(f"{EmojiGenerator.warning()} No se pudo registrar {path_s}; se omite")
        return
    fp = rec.fingerprint
    if fp != planned_fp:
        logger.warning(f"{EmojiGenerator.warning()} {path_s} cambió desde la simulación; se usa la huella nueva")
    if status == "pending" and rec.status != "pending" and event != "new":
        # pending es la falta de información: no se pisa un estado ya conocido
        logger.info(f"Se conserva el estado {rec.status} de {Path(path_s).name}")
    else:
        mediajelly_db.set_status(fp, status)
    known = mediajelly_db.get_stream_languages(fp)
    for index, lang in sorted(plan.languages.get(path_s, {}).items()):
        if index not in known:  # no se pisa un idioma ya guardado en la BD
            mediajelly_db.set_stream_language(fp, index, lang, LANGUAGE_SOURCE)


def _acquire_lock(tmp_dir: Path) -> Optional[int]:
    """Toma el flock de cron_runner.lock; None si está ocupado."""
    fd = os.open(str(tmp_dir / "cron_runner.lock"), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n{int(time.time())}\n".encode())
    return fd


def _default_backup_dir(tmp_dir: Path) -> Path:
    base = tmp_dir / f"migration-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    candidate, n = base, 1
    while candidate.exists():
        candidate = base.with_name(f"{base.name}-{n}")
        n += 1
    return candidate


def _print_summary(plan: MigrationPlan, backup_dir: Path, apply: bool) -> None:
    counts: Dict[str, int] = {}
    for _path, _fp, status in plan.rows:
        counts[status] = counts.get(status, 0) + 1
    print("=== Migración al estado por huella" + (" ===" if apply else " (simulación: no se escribe nada) ==="))
    print(f"Videos encontrados: {len(plan.rows) + len(plan.unreadable)}")
    for status in ("done", "failed", "pending"):
        print(f"  {status}: {counts.get(status, 0)}")
    print(f"Idiomas importados: {sum(len(v) for v in plan.languages.values())} stream(s) en {len(plan.languages)} archivo(s)")
    print(f"Completados con ruta fuera de --media-root (unmatched_prefix): {plan.unmatched_prefix} de {plan.completed_total}")
    print(f"Dudosos (done por episodio, no por ruta exacta): {len(plan.doubtful)}")
    for path in plan.doubtful:
        print(f"  ? {path}")
    if plan.language_conflicts:
        print(f"Idiomas no importados por caché contradictoria: {len(plan.language_conflicts)}")
        for path in plan.language_conflicts:
            print(f"  ! {path}")
    if plan.unreadable:
        print(f"No se pudieron leer (se omiten): {len(plan.unreadable)}")
        for path in plan.unreadable:
            print(f"  ! {path}")
    print(f"Respaldo{'' if apply else ' que se usaría'}: {backup_dir}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Importa el estado anterior al estado por huella de contenido")
    parser.add_argument("--media-root", default=DEFAULT_MEDIA_ROOT, help=f"Raíz de medios (por defecto {DEFAULT_MEDIA_ROOT})")
    parser.add_argument("--apply", action="store_true", help="Aplica los cambios (sin esto, solo simula)")
    parser.add_argument(
        "--allow-empty-history",
        action="store_true",
        help="Permite migrar sin ningún completado en el historial (todo quedaría pending)",
    )
    args = parser.parse_args(argv)

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

    media_root = Path(args.media_root)
    tmp_dir = mediajelly_db._get_db_path().parent
    if not media_root.is_dir():
        print(f"No existe la raíz de medios {media_root}")
        return 2

    lock_fd = None
    if args.apply:
        lock_fd = _acquire_lock(tmp_dir)
        if lock_fd is None:
            print("El cron runner está ocupado (cron_runner.lock tomado); reintenta cuando termine el ciclo")
            return 1
    try:
        try:
            plan = build_plan(media_root, tmp_dir)
        except MigrationError as e:
            print(str(e))
            return 2
        if plan.completed_total == 0 and (plan.rows or plan.unreadable) and not args.allow_empty_history:
            print(
                "El historial de completados está vacío: todo quedaría pending y se recomprimiría la biblioteca. "
                "Si es correcto, repite con --allow-empty-history"
            )
            return 2
        backup_dir = _default_backup_dir(tmp_dir)
        _print_summary(plan, backup_dir, args.apply)
        if args.apply:
            apply_plan(plan, backup_dir)
            if plan.failed_rows:
                print(f"Migración aplicada con errores: {len(plan.failed_rows)} de {len(plan.rows)} fila(s) fallaron")
                return 1
            mediajelly_db.mark_legacy_migrated()
            print(f"{EmojiGenerator.success()} Migración aplicada: {len(plan.rows)} archivo(s)")
        return 0
    finally:
        if lock_fd is not None:
            os.close(lock_fd)


if __name__ == "__main__":
    sys.exit(main())
