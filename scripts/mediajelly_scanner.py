#!/usr/bin/env python3
"""
MediaJelly Scanner - Media File Discovery and Management

Este módulo implementa el escáner de archivos multimedia para MediaJelly.
Detecta archivos de video nuevos, los valida y gestiona las colas de procesamiento.

Funcionalidades principales:
    - Escaneo recursivo de directorios multimedia
    - Validación de integridad de archivos con ffmpeg
    - Gestión de archivos corruptos (movimiento a .delete)
    - Detección de archivos que necesitan subtítulos
    - Exclusión automática de carpetas temporales

Example:
    Uso básico desde línea de comandos::

        $ python3 mediajelly_scanner.py /media/anime /media/series
        Archivos encontrados: 481
        Nuevas rutas detectadas: 15

    Uso programático::

        from mediajelly_scanner import MediaScanner

        scanner = MediaScanner()
        total, new = scanner.run(['/media/anime', '/media/series'])
        print(f"Nuevos archivos: {new}")

Attributes:
    EXTENSIONS (set): Extensiones de video soportadas (.mkv, .mp4, etc.)
    EXCLUDED_FOLDERS (set): Carpetas excluidas del escaneo (.delete, .tmp, etc.)

Note:
    El escáner automáticamente detecta si está corriendo en un contenedor
    Docker y ajusta las rutas base en consecuencia.

See Also:
    - mediajelly_processor.py: Procesamiento de archivos detectados
    - mediajelly_language_detector.py: Detección de idiomas con Whisper
"""

import json
import sys
import time
import logging
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional

from mediajelly_utils import MediaJellyPaths, create_compressed_rotating_file_handler
from mediajelly_db import register, mark_missing_except, purge_missing, replace_subtitle_queue

# Subtítulos de texto que el traductor puede exportar a .es.srt externo.
EXPORTABLE_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text"}

# Comprobaciones simultáneas de subtítulos (un ffprobe por video)
SUBTITLE_CHECK_WORKERS = 4


class MediaScanner:
    """
    Scanner optimizado de archivos multimedia con validación de integridad.

    Esta clase implementa el sistema de detección y validación de archivos
    multimedia para MediaJelly. Escanea directorios en busca de archivos de
    video, valida su integridad y gestiona las colas de procesamiento.

    Attributes:
        EXTENSIONS (set): Extensiones de archivos de video soportadas
        EXCLUDED_FOLDERS (set): Carpetas que deben ser excluidas del escaneo
        is_container (bool): True si está corriendo en contenedor Docker
        base_dir (Path): Directorio base de MediaJelly
        scripts_dir (Path): Directorio de scripts
        tmp_dir (Path): Directorio temporal para archivos de estado
        logs_dir (Path): Directorio de logs
        log_file (Path): Archivo de log del scanner
        logger (logging.Logger): Logger configurado
        start_time (float): Timestamp de inicio del escaneo

    Example:
        >>> scanner = MediaScanner()
        >>> total, new = scanner.run(['/media/anime'])
        >>> print(f"Total: {total}, Nuevos: {new}")
        Total: 212, Nuevos: 5
    """

    EXTENSIONS = MediaJellyPaths.EXTENSIONS
    EXCLUDED_FOLDERS = MediaJellyPaths.EXCLUDED_FOLDERS

    def __init__(self):
        # Detecta el entorno
        self.is_container = MediaJellyPaths.is_container()
        self.base_dir = MediaJellyPaths.get_base_path()
        self.scripts_dir = self.base_dir / "scripts"
        self.tmp_dir = self.scripts_dir / "tmp"
        self.logs_dir = self.scripts_dir / "tmp" / "logs"

        # Archivos
        self.log_file = self.logs_dir / "scan.log"

        # Configura logging
        self.setup_logging()

        # Crea directorios
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        self.start_time = time.time()

    def setup_logging(self):
        """Configurar logging con rotación de archivos"""
        # Crear handler con rotación (máx 10MB, mantener 5 backups)
        file_handler = create_compressed_rotating_file_handler(self.log_file, max_bytes=10 * 1024 * 1024, backup_count=5)
        file_handler.setLevel(logging.INFO)

        # Handler para consola
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)

        # Formato
        formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)

        # Configurar logger
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.INFO)
        self.logger.handlers.clear()
        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)

        # Evitar propagación a loggers padres
        self.logger.propagate = False

    def _is_file_in_excluded_folder(self, file_path: Path) -> bool:
        """Verifica si un archivo está en una carpeta excluida"""
        return any(part.lower() in self.EXCLUDED_FOLDERS for part in file_path.parts)

    def scan_folder_optimized(self, folder: Path) -> List[Path]:
        """Escaneo optimizado de carpeta usando pathlib"""
        if not folder.exists():
            self.logger.warning(f"Carpeta no encontrada: {folder}")
            return []

        self.logger.info(f"Escaneando: {folder}")
        found_files = []

        # Usa rglob para búsqueda recursiva eficiente
        for file_path in folder.rglob("*"):
            # Excluir archivos en carpetas temporales o de eliminación
            if self._is_file_in_excluded_folder(file_path):
                continue

            if (
                file_path.is_file()
                and file_path.suffix.lower() in self.EXTENSIONS
                and not file_path.name.startswith(".")
                and not file_path.name.endswith(".compressed.mp4")
            ):
                found_files.append(file_path)

        return found_files

    def scan_media_directories(self, media_dirs: List[Path]) -> tuple[int, int]:
        """Escanea los directorios de medios y registra los archivos por huella"""
        # Validar directorios
        valid_dirs = [d for d in media_dirs if d.exists()]
        if not valid_dirs:
            self.logger.warning("No hay directorios válidos para escanear")
            return 0, 0

        # Escanear todos los directorios
        all_found_files = self._scan_all_directories(valid_dirs)

        # Registrar por huella (nuevos, renombrados, etc.)
        return self._process_new_files(all_found_files, valid_dirs)

    def _scan_all_directories(self, media_dirs: List[Path]) -> List[Path]:
        """Escanea todos los directorios y retorna archivos encontrados"""
        all_found_files = []
        for media_dir in media_dirs:
            found_files = self.scan_folder_optimized(media_dir)
            all_found_files.extend(found_files)
            self.logger.info(f"Directorio {media_dir.name}: {len(found_files)} encontrados")

        return all_found_files

    def _process_new_files(self, all_found_files: List[Path], roots: Optional[List[Path]] = None) -> tuple[int, int]:
        """Registra cada archivo por huella y devuelve (encontrados, nuevos)"""
        counts = {"new": 0, "renamed": 0, "unchanged": 0, "unsettled": 0, "duplicate": 0}
        new_files: List[str] = []

        for file_path in all_found_files:
            try:
                record, event = register(file_path)
            except Exception as e:
                self.logger.error(f"Error registrando {file_path.name}: {e}")
                continue

            if event in ("new", "replaced"):
                counts["new"] += 1
                new_files.append(str(file_path))
            elif event == "renamed":
                counts["renamed"] += 1
            elif event in counts:
                counts[event] += 1
            else:
                self.logger.info(f"Archivo {event}: {file_path.name}")

        self.logger.info(
            f"Nuevos: {counts['new']}, renombrados: {counts['renamed']}, sin cambios: {counts['unchanged']}, "
            f"en copia: {counts['unsettled']}, duplicados: {counts['duplicate']}"
        )

        # Marcar ausentes (todas las rutas de todos los directorios) y purgar los antiguos
        try:
            mark_missing_except({str(p) for p in all_found_files}, roots=self._roots_with_files(all_found_files, roots))
            purge_missing()
        except Exception as e:
            self.logger.error(f"Error actualizando archivos ausentes: {e}")

        if not new_files:
            self.logger.info("No hay archivos nuevos para agregar a pendientes")
            return len(all_found_files), 0

        self._log_new_files_added(new_files)
        return len(all_found_files), len(new_files)

    @staticmethod
    def _roots_with_files(all_found_files: List[Path], roots: Optional[List[Path]]) -> List[str]:
        """Raíces que existen y produjeron al menos un archivo (protege montajes caídos o vacíos)"""
        if roots is None:
            return []
        return [str(r) for r in roots if any(r in f.parents for f in all_found_files)]

    def _log_new_files_added(self, new_files: List[str]):
        """Registra archivos nuevos agregados"""
        self.logger.info(f"Nuevos archivos agregados a pendientes: {len(new_files)}")
        for i, file_path in enumerate(new_files[:5]):
            self.logger.info(f"  + {Path(file_path).name}")
        if len(new_files) > 5:
            self.logger.info(f"  ... y {len(new_files) - 5} más")

    def scan_subtitle_directories(self, media_dirs: List[Path]) -> int:
        """Escanea directorios en busca de archivos que necesiten subtítulos"""
        self.logger.info("=== Inicia escaneo de subtítulos ===")

        # Buscar todos los archivos de video
        all_video_files = self._find_all_video_files(media_dirs)

        # Determinar archivos que necesitan subtítulos (sin cargar existentes)
        files_needing_subtitles = self._find_files_needing_subtitles_fresh(all_video_files)

        # Reemplazar la cola completa con la lista actualizada
        self._write_subtitle_files(files_needing_subtitles)

        if not files_needing_subtitles:
            self.logger.info("No hay archivos que necesiten subtítulos")
        else:
            # Log de archivos encontrados
            self._log_subtitle_files_added(files_needing_subtitles)

        self.logger.info("=== Escaneo de subtítulos completado ===")
        return len(files_needing_subtitles)

    def _find_all_video_files(self, media_dirs: List[Path]) -> List[Path]:
        """Encuentra todos los archivos de video en los directorios"""
        all_video_files = []
        for media_dir in media_dirs:
            if media_dir.exists():
                self.logger.info(f"Escaneando: {media_dir}")
                for file_path in media_dir.rglob("*"):
                    if (
                        file_path.is_file()
                        and file_path.suffix.lower() in self.EXTENSIONS
                        and not self._is_file_in_excluded_folder(file_path)
                    ):
                        all_video_files.append(file_path)
        return all_video_files

    def _find_files_needing_subtitles_fresh(self, video_files: List[Path]) -> List[str]:
        """Determina qué archivos necesitan subtítulos (sin filtrar por pending existente)"""
        candidates = []
        for video_file in video_files:
            # Validación: excluir archivos con ".compressed" en el nombre
            if ".compressed" in video_file.name:
                self.logger.debug(f"Excluyendo archivo comprimido de subtítulos: {video_file.name}")
                continue
            # Carpeta marcada para no generar subtítulos (p. ej. ya vienen incrustados)
            if MediaJellyPaths.has_nosubs_marker(video_file):
                continue
            candidates.append(video_file)

        # La comprobación lanza un ffprobe por video (espera de E/S): en paralelo, conservando el orden
        with ThreadPoolExecutor(max_workers=SUBTITLE_CHECK_WORKERS) as executor:
            has_spanish = list(executor.map(self._has_spanish_subtitle, candidates))

        # Solo agregar los que NO tienen subtítulos en español
        return [str(video_file) for video_file, spanish in zip(candidates, has_spanish) if not spanish]

    def _has_spanish_subtitle(self, video_file: Path) -> bool:
        """Verifica si el archivo ya tiene subtítulos en español (externos o embebidos)"""

        # 1. Verificar archivos de subtítulos externos en español
        video_stem = video_file.stem

        # Patrones de subtítulos en español (priorizados por calidad)
        spanish_subtitle_patterns = [
            f"{video_stem}.es.srt",  # Español genérico (prioritario)
            f"{video_stem}.spa.srt",  # Español (código ISO)
            f"{video_stem}.es-ES.srt",  # Español de España
            f"{video_stem}.es-MX.srt",  # Español de México
            f"{video_stem}.es-AR.srt",  # Español de Argentina
            f"{video_stem}.spanish.srt",  # Español (nombre completo)
            f"{video_stem}.español.srt",  # Español (acentuado)
        ]

        # Buscar subtítulos en español (excluyendo .hi - hearing impaired)
        for pattern in spanish_subtitle_patterns:
            subtitle_path = video_file.parent / pattern
            # Verificar que exista y no sea hearing impaired
            if subtitle_path.exists() and ".hi." not in subtitle_path.name:
                # Verificar que el archivo no esté vacío
                if subtitle_path.stat().st_size > 100:  # Al menos 100 bytes
                    return True

        # También buscar cualquier .srt existente que no necesite traducción
        # (por ejemplo, si ya existe un .srt sin idioma específico)
        generic_srt = video_file.with_suffix(".srt")
        if generic_srt.exists() and generic_srt.stat().st_size > 100:
            # Verificar si es español mediante detección simple
            try:
                with open(generic_srt, "r", encoding="utf-8", errors="ignore") as f:
                    sample = f.read(4000)  # Leer muestra
                    # Palabras comunes en español
                    spanish_words = [
                        "el",
                        "la",
                        "los",
                        "las",
                        "de",
                        "que",
                        "en",
                        "y",
                        "a",
                        "un",
                        "una",
                        "por",
                        "con",
                        "para",
                    ]
                    sample_lower = sample.lower()
                    spanish_count = sum(1 for word in spanish_words if f" {word} " in sample_lower)
                    if spanish_count >= 3:  # Si encuentra al menos 3 palabras españolas
                        return True
            except Exception:
                pass

        # Un .srt genérico que no es español NO cuenta: queda pendiente para que el
        # traductor lo traduzca (antes el video nunca volvía a la cola).

        # 2. Subtítulos embebidos en español. Uno de texto no basta: el Roku no muestra
        # mov_text incrustado, así que el traductor lo exporta a .es.srt. Uno de imagen
        # (PGS) no se puede exportar y cuenta como resuelto para no reencolarlo siempre.
        try:
            probe_cmd = [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "s",
                "-show_entries",
                "stream=codec_name:stream_tags=language:stream_disposition=forced",
                "-of",
                "json",
                str(video_file),
            ]
            result = subprocess.run(
                probe_cmd,
                capture_output=True,
                timeout=5,  # Reducido de 10 a 5 segundos
                encoding="utf-8",
                errors="replace",
            )

            if result.returncode == 0:
                # Buscar pistas completas en español (las forzadas solo traen carteles)
                spanish_codes = {"spa", "es", "esp", "spanish"}

                for stream in json.loads(result.stdout).get("streams", []):
                    lang = (stream.get("tags", {}).get("language") or "").strip().lower()
                    codec = (stream.get("codec_name") or "").lower()
                    if (
                        lang in spanish_codes
                        and not stream.get("disposition", {}).get("forced")
                        and codec not in EXPORTABLE_SUB_CODECS
                    ):
                        return True

        except Exception as e:
            # Si hay error verificando embebidos, asumir que no tiene para ser conservador
            self.logger.debug(f"Error verificando subtítulos embebidos en {video_file.name}: {e}")

        return False

    def _write_subtitle_files(self, files_needing_subtitles: List[str]):
        """Reemplaza la cola de subtítulos en la base de datos"""
        try:
            replace_subtitle_queue(files_needing_subtitles)
        except Exception as e:
            self.logger.error(f"Error escribiendo la cola de subtítulos (DB): {e}")

    def _log_subtitle_files_added(self, files_needing_subtitles: List[str]):
        """Registra archivos de subtítulos encontrados"""
        self.logger.info(f"Encontrados {len(files_needing_subtitles)} archivos que necesitan subtítulos")
        for i, file_path in enumerate(files_needing_subtitles[:5]):
            self.logger.info(f"  + {Path(file_path).name}")
        if len(files_needing_subtitles) > 5:
            self.logger.info(f"  ... y {len(files_needing_subtitles) - 5} más")

    def validate_file_integrity(self, file_path: Path) -> bool:
        """Valida la integridad de un archivo multimedia usando ffprobe

        Args:
            file_path: Ruta al archivo a validar

        Returns:
            True si el archivo es válido, False si está corrupto
        """
        try:
            # Comando ffprobe para validar archivo (más rápido que ffmpeg)
            cmd = ["ffprobe", "-v", "error", "-show_format", "-show_streams", str(file_path)]

            # Ejecutar comando con timeout de 300 segundos
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300, text=True)

            # Si hay errores en stderr, el archivo puede estar corrupto
            if result.stderr:
                self.logger.warning(f"Archivo potencialmente corrupto: {file_path.name}")
                self.logger.warning(f"Errores detectados: {result.stderr[:200]}")
                return False

            return True

        except subprocess.TimeoutExpired:
            self.logger.error(f"Timeout validando archivo: {file_path.name}")
            return False
        except Exception as e:
            self.logger.error(f"Error validando {file_path.name}: {e}")
            return False

    def move_corrupted_file(self, file_path: Path) -> bool:
        """Mueve un archivo corrupto a la carpeta .delete

        Args:
            file_path: Ruta al archivo corrupto

        Returns:
            True si se movió exitosamente, False en caso contrario
        """
        try:
            # Crear carpeta .delete en el mismo directorio del archivo
            delete_folder = file_path.parent / ".delete"
            delete_folder.mkdir(exist_ok=True)

            # Mover archivo
            destination = delete_folder / file_path.name
            file_path.rename(destination)

            self.logger.info(f"Archivo corrupto movido a: {destination}")
            return True

        except Exception as e:
            self.logger.error(f"Error moviendo archivo corrupto {file_path.name}: {e}")
            return False

    def run(self, media_paths: List[str]) -> tuple[int, int]:
        """Función principal de ejecución optimizada - SIN notificaciones"""
        self.logger.info("=== Inicia escaneo ===")

        # Convierte las rutas a objetos Path y valida
        valid_dirs = [Path(path) for path in media_paths if Path(path).exists()]

        if not valid_dirs:
            self.logger.error("No se encontraron directorios válidos")
            return 0, 0

        self.logger.info(f"=== Inicia escaneo de {len(valid_dirs)} directorios ===")

        # Escanea directorios de medios
        total_found, new_detected = self.scan_media_directories(valid_dirs)

        # Escanea subtítulos
        self.scan_subtitle_directories(valid_dirs)

        self.logger.info(f"=== Escaneo completado en {time.time() - self.start_time:.2f}s ===")
        return total_found, new_detected


def main():
    """Función principal"""
    if len(sys.argv) < 2:
        print("Uso: mediajelly_scanner.py <carpeta1> [carpeta2 ...]")
        sys.exit(1)

    scanner = MediaScanner()

    # Ejecución de escaneo
    media_paths = sys.argv[1:]
    total_found, new_detected = scanner.run(media_paths)

    # Salida compatible con script bash
    print(f"Archivos encontrados: {total_found}")
    print(f"Nuevas rutas detectadas: {new_detected}")


if __name__ == "__main__":
    main()
