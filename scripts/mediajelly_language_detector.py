#!/usr/bin/env python3
"""
MediaJelly Language Detector
Pre-análisis de idiomas de pistas de audio sin etiquetas.
Ejecuta ANTES del procesador para evitar problemas de memoria.
"""

import os
import sys
import json
import subprocess

import tempfile
import random
import traceback
import hashlib
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional, Any, Union, Tuple

try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

# Importar módulo de emojis
from mediajelly_emoji import EmojiGenerator
from mediajelly_utils import MediaJellyPaths
from mediajelly_db import (
    get_pending_files,
    register,
    get_by_path,
    set_stream_language,
    get_stream_languages,
    normalize_lang,
    record_rewrite,
    record_retag_failure,
    has_retag_failure,
    list_language_candidates,
    record_language_attempt,
    migration_pending,
    MIGRATION_PENDING_MESSAGE,
)
import mediajelly_progreso as progreso
import mediajelly_retag
from mediajelly_retag import retag_audio

# Importar configuración centralizada
from mediajelly_config import MediaJellyConfig

# ============================================================================
# CONFIGURACIÓN
# ============================================================================

# Cargar configuración centralizada
try:
    config = MediaJellyConfig()
    print(f"{EmojiGenerator.success()} Configuración YAML cargada exitosamente")
except Exception as e:
    print(f"Error cargando configuración: {e}")
    sys.exit(1)


def _normalize_and_rename_if_needed(file_path: Union[str, Path]) -> Optional[Path]:
    """Normaliza nombre y renombra si cambia; retorna nueva ruta o None"""
    try:
        validated = validate_file_path(file_path)
    except Exception:
        return None

    is_movie = any(part.lower() in ["peliculas", "movies"] for part in validated.parts)
    orig = validated.stem
    ext = validated.suffix

    new_name = orig
    new_name = _remove_fansub_prefixes(new_name)
    if not is_movie:
        new_name = _normalize_season_episode_format(new_name)
    new_name = _clean_filename_formatting(new_name)

    if new_name != orig:
        return _rename_file_safely(validated, new_name, ext)
    return None

# Extraer configuración específica del módulo
MEDIA_PATHS = config.paths.media_paths
CACHE_FILE_PATH = config.paths.cache_file_path
# Mejorar robustez: calcular la ruta efectiva del cache y directorio tmp
try:
    _cache_path_candidate = Path(CACHE_FILE_PATH)
    _cache_dir_candidate = _cache_path_candidate.parent
    try:
        _cache_dir_candidate.mkdir(parents=True, exist_ok=True)
        _cache_path_effective = _cache_path_candidate
    except Exception:
        # No se puede crear en la ruta de config (perm/ausencia). Usar tmp local
        _cache_path_effective = Path(__file__).parent / "tmp" / "language_cache.json"
        _cache_path_effective.parent.mkdir(parents=True, exist_ok=True)
except Exception:
    _cache_path_effective = Path(__file__).parent / "tmp" / "language_cache.json"

CACHE_FILE_PATH = str(_cache_path_effective)
TMP_DIR = Path(CACHE_FILE_PATH).parent
try:
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    # Intentar configurar permisos razonables
    os.chmod(str(TMP_DIR), 0o775)
except Exception as _e:
    # No fallar si no puede crear o cambiar permisos en el tmp
    pass
LOG_FILE_PATH = config.logging.language_detection_log

# Configuración Redis para cache
REDIS_HOST = config.language_detection.redis_host
REDIS_PORT = config.language_detection.redis_port
REDIS_DB = config.language_detection.redis_db
REDIS_PASSWORD = config.language_detection.redis_password
CACHE_TTL = config.language_detection.cache_ttl_seconds

LANGUAGE_CODE_MAP = config.language_detection.language_code_map

# ============================================================================
# UTILIDADES
# ============================================================================


def validate_file_path(file_path: Union[str, Path]) -> Path:
    """
    Valida que la ruta de archivo sea segura y exista.

    Args:
        file_path: Ruta del archivo a validar.

    Returns:
        Path: Objeto Path resuelto y validado.

    Raises:
        ValueError: Si la ruta es inválida o es un directorio.
        FileNotFoundError: Si el archivo no existe.
    """
    if not file_path or not isinstance(file_path, (str, Path)):
        raise ValueError("Ruta de archivo inválida")

    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Archivo no encontrado: {file_path}")

    # Validar que no sea un directorio
    if path.is_dir():
        raise ValueError(f"La ruta es un directorio, no un archivo: {file_path}")

    return path.resolve()


def safe_subprocess_run(cmd: List[str], **kwargs) -> subprocess.CompletedProcess:
    """
    Ejecuta subprocess.run con validación adicional de seguridad.

    Args:
        cmd: Lista de argumentos del comando.
        **kwargs: Argumentos adicionales para subprocess.run.

    Returns:
        subprocess.CompletedProcess: Resultado de la ejecución.

    Raises:
        ValueError: Si el comando es inválido o no permitido.
    """
    if not cmd or not isinstance(cmd, list):
        raise ValueError("Comando inválido")

    # Validar que el comando ejecutable existe
    executable = cmd[0]
    if not executable or not isinstance(executable, str):
        raise ValueError("Ejecutable inválido")

    # Solo permitir comandos conocidos y seguros
    allowed_commands = ["ffprobe", "ffmpeg"]
    if executable not in allowed_commands:
        raise ValueError(f"Comando no permitido: {executable}")

    return subprocess.run(cmd, **kwargs)


def log(message: str, level: str = "INFO") -> None:
    """Registra mensaje en el log con timestamp y emoji."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    emoji_map = {
        "INFO": EmojiGenerator.info(),
        "SUCCESS": EmojiGenerator.success(),
        "ERROR": EmojiGenerator.error(),
        "WARNING": EmojiGenerator.warning_msg(),
        "DETECT": EmojiGenerator.audio(),
        "SKIP": EmojiGenerator.next_track(),
    }
    emoji = emoji_map.get(level, "•")
    log_message = f"[{timestamp}] {emoji} {message}"

    print(log_message)

    try:
        os.makedirs(os.path.dirname(LOG_FILE_PATH), exist_ok=True)
        with open(LOG_FILE_PATH, "a", encoding="utf-8") as f:
            f.write(log_message + "\n")
    except Exception as e:
        print(f"{EmojiGenerator.error()} Error escribiendo log: {e}")


def load_cache() -> Dict[str, Any]:
    """
    Carga el caché de idiomas detectados desde el archivo JSON.

    Ya no se usa en la detección (los idiomas viven en la BD por huella); se
    conserva solo como referencia de la migración del estado anterior.

    Returns:
        Dict[str, Any]: Diccionario con el caché cargado o vacío si hay error.
    """
    if os.path.exists(CACHE_FILE_PATH):
        try:
            with open(CACHE_FILE_PATH, "r", encoding="utf-8") as f:
                try:
                    return json.load(f)
                except json.JSONDecodeError as e:
                    # Corrupt cache: mover a backup
                    backup_path = f"{CACHE_FILE_PATH}.corrupt"
                    try:
                        os.replace(CACHE_FILE_PATH, backup_path)
                        log(f"Caché corrupto movido a {backup_path}", "WARNING")
                    except Exception as _e:
                        log(f"Error moviendo caché corrupto: {_e}", "WARNING")
                    return {}
        except Exception as e:
            log(f"Error cargando caché: {e}", "ERROR")
    return {}


# ---------------------------------------------------------------------------
# Normalización / renaming helpers (usados por el detector)
# ---------------------------------------------------------------------------

def _clean_filename_formatting(filename: str) -> str:
    """Limpia espacios múltiples, guiones redundantes y formato del nombre"""
    import re

    # Limpiar espacios y guiones
    filename = re.sub(r"\s*-\s*(S\d+E\d+)\s*-\s*", r" \1 ", filename)
    filename = re.sub(r"\s+", " ", filename)
    filename = re.sub(r"\s*-\s*-\s*", " - ", filename)
    filename = re.sub(r"\s*-\s*$", "", filename)
    filename = re.sub(r"^\s*-\s*", "", filename)
    return filename.strip()


def _remove_fansub_prefixes(filename: str) -> str:
    """Elimina prefijos de grupos fansub del nombre del archivo"""
    import re

    fansub_patterns = [
        r"^\[([^\]]+)\]\s*",  # [Grupo]
        r"^\(([^\)]+)\)\s*",  # (Grupo)
        r"^\{([^\}]+)\}\s*",  # {Grupo}
    ]

    for pattern in fansub_patterns:
        match = re.match(pattern, filename)
        if match:
            group_name = match.group(1)
            rest_of_name = filename[match.end() :]
            if rest_of_name and not rest_of_name.lower().startswith(group_name.lower()):
                return rest_of_name.strip()

    return filename


def _normalize_season_episode_format(filename: str) -> str:
    """Normaliza el formato de temporada/episodio en nombres de series"""
    import re

    # Intentar diferentes patrones de normalización
    match = re.search(r"S(\d{1,2})E(\d{1,2})", filename, re.IGNORECASE)
    if match:
        season = match.group(1).zfill(2)
        episode = match.group(2).zfill(2)
        before = filename[: match.start()].strip()
        after = filename[match.end() :].strip()
        if before and after:
            return f"{before} S{season}E{episode} {after}"
        elif before:
            return f"{before} S{season}E{episode}"
        else:
            return f"S{season}E{episode} {after}" if after else f"S{season}E{episode}"

    match = re.search(r"(\d{1,2})x(\d{1,2})", filename, re.IGNORECASE)
    if match:
        season = match.group(1).zfill(2)
        episode = match.group(2).zfill(2)
        before = filename[: match.start()].strip()
        after = filename[match.end() :].strip()
        if before and after:
            return f"{before} S{season}E{episode} {after}"
        elif before:
            return f"{before} S{season}E{episode}"
        else:
            return f"S{season}E{episode} {after}" if after else f"S{season}E{episode}"

    match = re.search(r"\b(\d{1})(\d{2})\b", filename)
    if match:
        season = match.group(1).zfill(2)
        episode = match.group(2).zfill(2)
        before = filename[: match.start()].strip()
        after = filename[match.end() :].strip()
        if before and after:
            return f"{before} S{season}E{episode} {after}"
        elif before:
            return f"{before} S{season}E{episode}"
        else:
            return f"S{season}E{episode} {after}" if after else f"S{season}E{episode}"

    return filename


def _rename_file_safely(file_path: Path, new_name: str, extension: str) -> Optional[Path]:
    """Renombra un archivo de forma segura verificando conflictos (versión para el detector).

    Retorna la nueva ruta si la operación fue exitosa o None si falló.
    """
    new_path = file_path.parent / f"{new_name}{extension}"

    if new_path.exists():
        log(f"No se puede renombrar a '{new_path.name}': el archivo ya existe", "WARNING")
        return None

    try:
        file_path.rename(new_path)
        log(f"✓ Archivo renombrado: '{file_path.name}' -> '{new_path.name}'", "SUCCESS")
        return new_path
    except Exception as e:
        log(f"Error renombrando archivo: {e}", "ERROR")
        return None



def _update_pending_and_completed(old_path: Path, new_path: Path) -> None:
    """Registra la ruta nueva tras un renombrado.

    El estado va por huella de contenido: register detecta el renombrado y
    conserva el estado (pending, done, ...) de la ruta anterior.
    """
    try:
        register(str(new_path), require_settled=False)
    except Exception as e:
        log(f"Error registrando el renombrado {old_path} → {new_path}: {e}", "ERROR")


def adjust_pending_names() -> None:
    """Escanea pendientes en DB y normaliza nombres de archivos si es necesario."""

    try:
        # get_pending_files() retorna objetos Path, convertir a strings
        lines = [str(l).strip() for l in get_pending_files() if l and str(l).strip()]
        for line in lines:
            try:
                original_path = Path(line)
                if not original_path.exists():
                    continue
                new_path = _normalize_and_rename_if_needed(original_path)
                if new_path:
                    _update_pending_and_completed(original_path, new_path)
            except Exception as e:
                log(f"Error procesando pending line {line}: {e}", "WARNING")

    except Exception as e:
        log(f"Error ajustando nombres en pending: {e}", "ERROR")





def get_redis_client():
    """
    Obtiene cliente Redis si está disponible y configurado.

    Returns:
        redis.Redis or None: Cliente Redis conectado o None si no está disponible.
    """
    if not REDIS_AVAILABLE:
        return None
    try:
        client = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            db=REDIS_DB,
            password=REDIS_PASSWORD,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2
        )
        client.ping()  # Verificar conexión
        return client
    except Exception as e:
        log(f"Redis no disponible: {e}", "WARNING")
        return None


def get_file_hash(file_path: Union[str, Path]) -> Optional[str]:
    """
    Genera hash SHA256 del archivo para uso como clave de caché.

    Args:
        file_path: Ruta del archivo.

    Returns:
        str: Hash SHA256 en formato hexadecimal, o None si hay error.
    """
    try:
        hash_sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            # Leer en chunks para archivos grandes
            for chunk in iter(lambda: f.read(4096), b""):
                hash_sha256.update(chunk)
        return hash_sha256.hexdigest()
    except Exception as e:
        log(f"Error generando hash para {file_path}: {e}", "WARNING")
        return None


def get_cached_language(file_path: Union[str, Path], stream_index: int) -> Optional[str]:
    """
    Obtiene el idioma guardado para un archivo y stream específico.

    El idioma se guarda por huella de contenido, así que sobrevive a renombrados.

    Args:
        file_path: Ruta del archivo.
        stream_index: Índice absoluto del stream (el `index` de ffprobe).

    Returns:
        str: Código ISO 639-2 de 3 letras si se encuentra, None en caso contrario.
    """
    try:
        record, _ = register(file_path, require_settled=False)
        if record is None:
            return None
        lang = get_stream_languages(record.fingerprint).get(int(stream_index))
        if lang and lang != "und":
            log(f"Idioma guardado para {Path(file_path).name} stream {stream_index}: {lang}", "SUCCESS")
            return lang
    except Exception as e:
        log(f"Error leyendo idioma guardado: {e}", "WARNING")
    return None


def set_cached_language(file_path: Union[str, Path], stream_index: int, language: str) -> None:
    """
    Guarda el idioma detectado asociado a la huella del archivo.

    Args:
        file_path: Ruta del archivo.
        stream_index: Índice absoluto del stream (el `index` de ffprobe).
        language: Código de idioma detectado.
    """
    try:
        record, _ = register(file_path, require_settled=False)
        if record is None:
            return
        set_stream_language(record.fingerprint, int(stream_index), normalize_lang(language), "whisper")
        log(f"Idioma guardado para {Path(file_path).name} (stream {stream_index}): {normalize_lang(language)}", "SUCCESS")
    except Exception as e:
        log(f"Error guardando idioma: {e}", "WARNING")


def _audio_tags(file_path: Union[str, Path]) -> Dict[int, str]:
    """Etiquetas de idioma de las pistas de audio del archivo: {índice absoluto: etiqueta}."""
    result = safe_subprocess_run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index:stream_tags=language", "-of", "json", str(file_path)],
        capture_output=True, text=True, timeout=30,
    )
    tags: Dict[int, str] = {}
    for s in json.loads(result.stdout).get("streams", []):
        tags[int(s["index"])] = ((s.get("tags") or {}).get("language") or "und").lower()
    return tags


def retag_if_processed(file_path: Union[str, Path], detected: Dict[int, str]) -> bool:
    """Si el archivo ya está procesado (done/skipped), escribe en él los idiomas detectados.

    Solo toca los streams que están como 'und' en el archivo y cuyo idioma detectado no es 'und'.
    Tras reescribir, traslada el estado a la huella nueva.
    """
    try:
        record = get_by_path(file_path)
        if record is None or record.status not in ("done", "skipped"):
            return False
        in_file = _audio_tags(file_path)
        to_write = {
            idx: lang for idx, lang in detected.items()
            if lang and lang != "und" and in_file.get(idx, "und") in ("und", "unknown", "zxx", "")
        }
        if not to_write:
            return False
        if has_retag_failure(record.fingerprint):
            log(f"Re-etiquetado ya falló antes para {Path(file_path).name}; no se reintenta", "SKIP")
            return False
        if not retag_audio(Path(file_path), to_write):
            record_retag_failure(record.fingerprint, mediajelly_retag.ULTIMO_ERROR or "re-etiquetado fallido")
            return False
        record_rewrite(record.fingerprint, file_path)
        log(f"Etiqueta de idioma escrita en {Path(file_path).name}: {to_write}", "SUCCESS")
        return True
    except Exception as e:
        log(f"Error re-etiquetando {file_path}: {e}", "WARNING")
        return False


# ============================================================================
# DETECCIÓN DE STREAMS
# ============================================================================


def get_streams_needing_detection(file_path: Union[str, Path]) -> List[Tuple[int, str, float]]:
    """
    Detecta streams de audio que no tienen etiqueta de idioma válida.

    Args:
        file_path: Ruta del archivo a analizar.

    Returns:
        List[Tuple[int, str, float]]: Lista de tuplas (stream_index_absoluto, codec, duration).
    """
    try:
        # Validar entrada
        validated_path = validate_file_path(file_path)

        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=index,codec_type,codec_name:stream_tags=language",
            "-of",
            "json",
            str(validated_path),
        ]

        result = safe_subprocess_run(cmd, capture_output=True, text=True, timeout=30)

        if result.returncode != 0:
            log(f"Error en ffprobe: {result.stderr}", "ERROR")
            return []


        # Normalización de nombres ahora definida a nivel módulo



        data = json.loads(result.stdout)
        streams = data.get("streams", [])

        streams_to_detect = []
        audio_index = 0
        duration = None

        for stream in streams:
            if stream.get("codec_type") == "audio":
                tags = stream.get("tags", {})
                language = tags.get("language", "").lower()

                # Buscar idioma en mayúsculas también
                if not language:
                    language = tags.get("LANGUAGE", "").lower()

                if not language or language in ["und", "unknown", "zxx"]:
                    absolute_index = stream.get("index", audio_index)
                    codec_name = stream.get("codec_name", "unknown")

                    # Duración del archivo: un solo ffprobe aunque haya varias pistas sin idioma
                    if duration is None:
                        duration = get_file_duration(file_path)

                    streams_to_detect.append((absolute_index, codec_name, duration))
                    log(f"  Stream {absolute_index} (audio #{audio_index}): {codec_name} - necesita detección", "INFO")

                audio_index += 1

        return streams_to_detect

    except Exception as e:
        log(f"Error detectando streams: {e}", "ERROR")
        traceback.print_exc()
        return []


def get_file_duration(file_path: Union[str, Path]) -> float:
    """
    Obtiene la duración del archivo multimedia en segundos.

    Args:
        file_path: Ruta del archivo.

    Returns:
        float: Duración en segundos, o 0.0 si hay error.
    """
    try:
        # Validar entrada
        validated_path = validate_file_path(file_path)

        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(validated_path),
        ]

        result = safe_subprocess_run(cmd, capture_output=True, text=True, timeout=30)

        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())

        return 0.0

    except Exception as e:
        log(f"Error obteniendo duración: {e}", "WARNING")
        return 0.0


# ============================================================================
# DETECCIÓN DE IDIOMA CON WHISPER
# ============================================================================


def load_whisper_model():
    """
    Carga el modelo Whisper con optimizaciones de memoria.

    Returns:
        whisper.model: Modelo cargado o None si hay error.
    """
    try:
        import os
        try:
            import whisper as _whisper
        except Exception as e:
            log(f"whisper no disponible: {e}", "ERROR")
            return None

        model_name = config.language_detection.whisper_model
        log(f"{EmojiGenerator.refresh()} Cargando modelo Whisper ({model_name})...", "INFO")

        # Optimizaciones de memoria:
        # 1. Usar CPU en lugar de GPU para evitar OOM en GPU
        # 2. Reducir fragmentación de memoria
        os.environ['PYTORCH_CUDA_ALLOC_CONF'] = 'max_split_size_mb:256'
        os.environ['PYTORCH_MPS_HIGH_WATERMARK_RATIO'] = '0.0'

        # Cargar modelo en CPU con fp16 para reducir consumo
        model = _whisper.load_model(model_name, device="cpu", in_memory=False)
        log(f"{EmojiGenerator.success()} Modelo Whisper cargado exitosamente", "SUCCESS")
        return model
    except MemoryError as e:
        log(f"Error de memoria cargando Whisper: {e} - Sistema sin RAM disponible", "ERROR")
        log(f"Intenta: liberar memoria, reducir workers, o usar modelo más pequeño", "WARNING")
        return None
    except Exception as e:
        log(f"Error cargando modelo Whisper: {e}", "ERROR")
        traceback.print_exc()
        return None


def extract_audio_sample(file_path: Union[str, Path], stream_index: int, start_time: Union[int, float], duration: int = 30) -> Optional[str]:
    """
    Extrae una muestra de audio de un archivo multimedia.

    Args:
        file_path: Ruta del archivo original.
        stream_index: Índice del stream de audio a extraer.
        start_time: Tiempo de inicio en segundos.
        duration: Duración de la muestra en segundos.

    Returns:
        str: Ruta del archivo temporal .wav generado, o None si hay error.
    """
    try:
        # Validar entrada
        validated_path = validate_file_path(file_path)
        if not isinstance(stream_index, int) or stream_index < 0:
            raise ValueError("Índice de stream inválido")
        if not isinstance(start_time, (int, float)) or start_time < 0:
            raise ValueError("Tiempo de inicio inválido")
        if not isinstance(duration, (int, float)) or duration <= 0:
            raise ValueError("Duración inválida")

        # Usar TMP_DIR configurado o fallback al directorio tmp relativo al script
        try:
            temp_file = tempfile.NamedTemporaryFile(suffix=".wav", delete=False, dir=str(TMP_DIR))
        except Exception:
            fallback_dir = Path(__file__).parent / "tmp"
            fallback_dir.mkdir(parents=True, exist_ok=True)
            temp_file = tempfile.NamedTemporaryFile(suffix=".wav", delete=False, dir=str(fallback_dir))
        temp_path = temp_file.name
        temp_file.close()

        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            str(start_time),
            "-i",
            str(validated_path),
            "-t",
            str(duration),
            "-map",
            f"0:{stream_index}",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-vn",
            temp_path,
        ]

        result = safe_subprocess_run(cmd, capture_output=True, text=True, timeout=300)

        if result.returncode != 0:
            log(f"Error extrayendo muestra: {result.stderr}", "ERROR")
            if os.path.exists(temp_path):
                os.remove(temp_path)
            return None

        return temp_path

    except Exception as e:
        log(f"Error en extract_audio_sample: {e}", "ERROR")
        return None


def detect_language_with_whisper(model, audio_file: str) -> Tuple[Optional[str], Dict]:
    """
    Detecta el idioma de un archivo de audio usando Whisper.

    Args:
        model: Modelo Whisper cargado.
        audio_file: Ruta al archivo de audio.

    Returns:
        Tuple[Optional[str], Dict]: Idioma detectado y diccionario de probabilidades.
    """
    try:
        # Transcribir solo para detección de idioma (sin transcripción completa)
        audio = model.transcribe(audio_file, language=None, task="transcribe", fp16=False)
        detected_language = audio.get("language", "unknown")

        # Obtener probabilidades si están disponibles
        probs = {}

        # La API nueva de Whisper maneja la detección de idioma internamente
        # No necesitamos llamar a get_mel directamente

        return detected_language, probs

    except Exception as e:
        log(f"Error en detección Whisper: {e}", "ERROR")
        traceback.print_exc()
        return None, {}


def _avisar_muestra(contexto: str, muestra: int, total: int, stream_index: int) -> None:
    """Actualiza el detalle de la etapa "idiomas" (solo dentro del lote, que la inició).

    Cada archivo tarda minutos: sin esto el panel lo vería "sin señal" a los 60 s.
    """
    if contexto:
        progreso.actualizar("idiomas", detalle=f"muestra {muestra} de {total}, pista {stream_index} · {contexto}")


def detect_language_with_multiple_samples(
    model, file_path: Union[str, Path], stream_index: int, total_duration: float, contexto: str = ""
) -> Optional[str]:
    """
    Detecta el idioma usando múltiples muestras aleatorias y votación.

    Toma varias muestras del archivo, detecta el idioma en cada una y realiza
    una votación ponderada para determinar el idioma final.

    Args:
        model: Modelo Whisper cargado.
        file_path: Ruta del archivo multimedia.
        stream_index: Índice del stream de audio.
        total_duration: Duración total del archivo en segundos.
        contexto: "archivo X de N" del lote; si viene, cada muestra actualiza el avance.

    Returns:
        Optional[str]: Código ISO 639-2 (3 letras), o None si no se pudo analizar
        ninguna muestra (no se asume español: etiquetarlo así engaña a Sonarr/Jellyfin).
    """
    if total_duration < 60:
        log(f"Archivo muy corto ({total_duration:.1f}s), usando muestra única", "WARNING")
        return detect_single_sample(model, file_path, stream_index, total_duration, contexto)

    # Calcular número de muestras (máximo 15, duración 30s cada una)
    num_samples = min(15, int(total_duration / 30))
    log(f"🎙️ Analizando {num_samples} muestras aleatorias de 30s...", "DETECT")

    # Generar timestamps aleatorios (evitar primeros y últimos 15s)
    safe_start = 15
    safe_end = total_duration - 45  # -30s para la muestra, -15s de margen

    if safe_end <= safe_start:
        safe_end = total_duration - 30 if total_duration > 30 else 0

    timestamps = sorted(random.sample(range(int(safe_start), int(safe_end)), num_samples))

    # Analizar cada muestra
    language_votes = {}
    temp_files = []

    for i, timestamp in enumerate(timestamps, 1):
        _avisar_muestra(contexto, i, num_samples, stream_index)
        temp_audio = extract_audio_sample(file_path, stream_index, timestamp, 30)

        if not temp_audio:
            continue

        temp_files.append(temp_audio)

        detected_lang, probs = detect_language_with_whisper(model, temp_audio)

        if detected_lang:
            language_votes[detected_lang] = language_votes.get(detected_lang, 0) + 1

            # Mostrar top 3 probabilidades
            if probs:
                sorted_probs = sorted(probs.items(), key=lambda x: x[1], reverse=True)[:3]
                probs_str = ", ".join([f"{lang}: {prob*100:.1f}%" for lang, prob in sorted_probs])
                log(f"  Muestra {i-1}/{num_samples} (min {timestamp//30}): {detected_lang} ({probs_str})", "INFO")
            else:
                log(f"  Muestra {i-1}/{num_samples} (min {timestamp//30}): {detected_lang}", "INFO")

    # Limpiar archivos temporales
    for temp_file in temp_files:
        try:
            if os.path.exists(temp_file):
                os.remove(temp_file)
        except Exception as e:
            log(f"Error eliminando temporal {temp_file}: {e}", "WARNING")

    # Determinar idioma ganador
    if not language_votes:
        log("No se pudieron analizar muestras, idioma desconocido", "WARNING")
        return None

    # Ordenar por votos
    sorted_votes = sorted(language_votes.items(), key=lambda x: x[1], reverse=True)
    winner_lang, winner_votes = sorted_votes[0]
    total_votes = sum(language_votes.values())
    winner_percentage = (winner_votes / total_votes) * 100

    # Reglas especiales para español
    spanish_votes = language_votes.get("es", 0)
    spanish_percentage = (spanish_votes / total_votes) * 100 if total_votes > 0 else 0

    # Si español tiene ≥30% de votos, es español
    if spanish_percentage >= 30:
        final_lang = "es"
        log(
            f"{EmojiGenerator.success()} Votación: Español {spanish_percentage:.1f}% (≥30% umbral) - {sorted_votes}",
            "SUCCESS",
        )
    # Si el ganador tiene <50% y español está presente, usar español
    elif winner_percentage < 50 and spanish_votes > 0:
        final_lang = "es"
        log(
            f"{EmojiGenerator.success()} Votación: {winner_lang} {winner_percentage:.1f}% (<50%), Español presente → 'spa' - {sorted_votes}",
            "SUCCESS",
        )
    else:
        final_lang = winner_lang
        log(f"{EmojiGenerator.success()} Votación: {winner_lang} {winner_percentage:.1f}% - {sorted_votes}", "SUCCESS")

    # Convertir a código ISO 639-2 (3 letras)
    return LANGUAGE_CODE_MAP.get(final_lang, final_lang)


def detect_single_sample(
    model, file_path: Union[str, Path], stream_index: int, total_duration: float, contexto: str = ""
) -> Optional[str]:
    """
    Detecta el idioma usando una única muestra (para archivos cortos).

    Args:
        model: Modelo Whisper cargado.
        file_path: Ruta del archivo multimedia.
        stream_index: Índice del stream de audio.
        total_duration: Duración total del archivo en segundos.

    Returns:
        str: Código de idioma detectado en formato ISO 639-2.
    """
    start_time = min(10, total_duration / 2) if total_duration > 15 else 0
    _avisar_muestra(contexto, 1, 1, stream_index)

    temp_audio = extract_audio_sample(file_path, stream_index, start_time, min(30, total_duration))

    if not temp_audio:
        return None

    try:
        detected_lang, probs = detect_language_with_whisper(model, temp_audio)

        if detected_lang:
            final_lang = LANGUAGE_CODE_MAP.get(detected_lang, detected_lang)

            if probs:
                sorted_probs = sorted(probs.items(), key=lambda x: x[1], reverse=True)[:3]
                probs_str = ", ".join([f"{lang}: {prob*100:.1f}%" for lang, prob in sorted_probs])
                log(f"✅ Idioma detectado: {final_lang} ({probs_str})", "SUCCESS")
            else:
                log(f"✅ Idioma detectado: {final_lang}", "SUCCESS")

            return final_lang

        return None

    finally:
        try:
            if os.path.exists(temp_audio):
                os.remove(temp_audio)
        except Exception as e:
            log(f"Error eliminando temporal: {e}", "WARNING")


# ============================================================================
# PROCESO PRINCIPAL
# ============================================================================


def validate_media_paths() -> bool:
    """
    Valida que los directorios de medios configurados existan.

    Returns:
        bool: True si todos los directorios existen, False si falta alguno.
    """
    missing_paths = []
    for path in MEDIA_PATHS:
        if not Path(path).exists():
            missing_paths.append(path)

    if missing_paths:
        log(f"Directorios de medios no encontrados: {missing_paths}", "WARNING")
        return False
    return True


def scan_files_needing_detection() -> List[Dict[str, Any]]:
    """
    Escanea archivos pendientes y terminados y detecta cuáles necesitan análisis de idioma.

    Revisa los pendientes y los done/skipped (sin re-etiquetado fallido ni un
    intento de Whisper sin resultado en los últimos LANGUAGE_RETRY_DAYS días)
    y se queda con los que tienen pistas de audio sin etiqueta de idioma.

    Returns:
        List[Dict[str, Any]]: Lista de diccionarios con información de archivos a analizar.
    """
    # Validar que los directorios de medios existan
    if not validate_media_paths():
        log("Algunos directorios de medios no existen", "ERROR")
        return []

    files_to_analyze = []

    log(f"Leyendo archivos pendientes desde DB en: {MediaJellyPaths.get_tmp_dir() / 'mediajelly.db'}", "INFO")

    # Leer archivos pendientes
    try:
        # get_pending_files() retorna objetos Path, convertir a strings
        pending_paths = [str(line).strip() for line in get_pending_files() if line and str(line).strip()]
    except Exception as e:
        log(f"Error leyendo pendientes desde DB: {e}", "ERROR")
        import traceback
        log(f"Traceback: {traceback.format_exc()}", "ERROR")
        return files_to_analyze

    # Terminados (done/skipped): se revisan para escribir en el archivo los idiomas
    # que sigan como und (detectados ahora o importados por la migración)
    try:
        pending_set = set(pending_paths)
        processed_paths = [r.path for r in list_language_candidates() if r.path not in pending_set]
    except Exception as e:
        log(f"Error leyendo archivos terminados desde DB: {e}", "ERROR")
        processed_paths = []

    log(
        f"Encontrados {len(pending_paths)} archivos pendientes y {len(processed_paths)} terminados por revisar",
        "INFO",
    )
    processed_set = set(processed_paths)
    processed_tagged = 0

    # Procesar cada archivo pendiente o terminado
    for file_path_str in pending_paths + processed_paths:
        file_path = Path(file_path_str)
        is_processed = file_path_str in processed_set

        # Verificar que el archivo existe
        if not file_path.exists():
            if not is_processed:
                log(f"Archivo pendiente no existe: {file_path}", "WARNING")
            continue

        # Verificar que es un archivo de video
        if not file_path.suffix.lower() in [".mkv", ".mp4", ".avi", ".m4v", ".mov"]:
            if not is_processed:
                log(f"Archivo pendiente no es video: {file_path}", "WARNING")
            continue

        # CORRECCIÓN: Usar ruta absoluta para get_streams_needing_detection
        absolute_path = str(file_path.resolve())

        # Verificar si necesita detección de idioma
        streams = get_streams_needing_detection(absolute_path)

        if streams:
            # Convertir a ruta relativa al directorio media para consistencia en cache
            if "/media/" in absolute_path:
                relative_path = absolute_path.split("/media/", 1)[1]
            else:
                # Fallback: usar el nombre del archivo como identificador
                relative_path = file_path.name

            files_to_analyze.append(
                {"absolute_path": absolute_path, "relative_path": relative_path, "streams": streams}
            )
            log(f"{file_path.name}: {len(streams)} stream(s) sin idioma", "INFO")
        elif is_processed:
            processed_tagged += 1
        else:
            log(f"{file_path.name}: ya tiene etiquetas de idioma", "SKIP")

    if processed_tagged:
        log(f"{processed_tagged} archivo(s) terminados ya tienen etiquetas de idioma", "SKIP")

    return files_to_analyze


def process_files(files_to_analyze: List[Dict[str, Any]], cache: Dict[str, Any]) -> Dict[str, Any]:
    """
    Procesa los archivos detectando idiomas de sus streams.

    Args:
        files_to_analyze: Lista de archivos a analizar.
        cache: Caché actual de idiomas.

    Returns:
        Dict[str, Any]: Caché actualizado.
    """
    if not files_to_analyze:
        log("No hay archivos que necesiten análisis de idioma", "INFO")
        return cache

    log(f"Total de archivos a analizar: {len(files_to_analyze)}", "INFO")

    # Cargar modelo Whisper UNA SOLA VEZ
    model = load_whisper_model()

    if not model:
        log("No se pudo cargar Whisper, abortando análisis", "ERROR")
        return cache

    # Procesar cada archivo (el avance de la etapa se cierra pase lo que pase)
    try:
        for idx, file_info in enumerate(files_to_analyze, 1):
            absolute_path = file_info["absolute_path"]
            relative_path = file_info["relative_path"]
            streams = file_info["streams"]

            log(f"\n{'='*80}", "INFO")
            log(f"[{idx}/{len(files_to_analyze)}] {os.path.basename(absolute_path)}", "INFO")
            log(f"{'='*80}", "INFO")
            progreso.iniciar(
                "idiomas", os.path.basename(absolute_path), None, f"archivo {idx} de {len(files_to_analyze)}"
            )

            # Inicializar entrada en caché usando ruta absoluta resuelta
            if absolute_path not in cache:
                cache[absolute_path] = {}

            detected_in_file: Dict[int, str] = dict(file_info.get("known") or {})
            undetected = False

            # Analizar cada stream
            for stream_index, codec, duration in streams:
                log(f"\n🎙️ Analizando stream {stream_index} ({codec}, {duration:.1f}s)...", "DETECT")

                # Verificar cache primero
                cached_lang = get_cached_language(absolute_path, stream_index)
                if cached_lang:
                    detected_lang = cached_lang
                    log(f"Stream {stream_index}: {detected_lang} (cacheado)", "SUCCESS")
                else:
                    detected_lang = detect_language_with_multiple_samples(
                        model, absolute_path, stream_index, duration,
                        contexto=f"archivo {idx} de {len(files_to_analyze)}",
                    )
                    if not detected_lang:
                        # Sin guardar: un pendiente se reintenta en la próxima corrida;
                        # uno terminado espera LANGUAGE_RETRY_DAYS días (ver abajo)
                        log(f"Stream {stream_index}: idioma no detectado", "WARNING")
                        undetected = True
                        continue
                    set_cached_language(absolute_path, stream_index, detected_lang)
                    log(f"Stream {stream_index}: {detected_lang}", "SUCCESS")

                detected_in_file[stream_index] = normalize_lang(detected_lang)
                # Compatibilidad: la fuente de verdad es la BD; el dict solo refleja la corrida
                cache[absolute_path][str(stream_index)] = detected_lang

            # Terminado con alguna pista sin detectar: no se vuelve a pasar por Whisper en cada ciclo
            if undetected:
                try:
                    record = get_by_path(absolute_path)
                    if record is not None and record.status in ("done", "skipped"):
                        record_language_attempt(record.fingerprint)
                except Exception as e:
                    log(f"Error anotando el intento de detección de {absolute_path}: {e}", "WARNING")

            # Si el archivo ya está procesado, escribir la etiqueta de inmediato
            if detected_in_file:
                retag_if_processed(absolute_path, detected_in_file)
    finally:
        progreso.terminar("idiomas")

    return cache


def main():
    """Función principal."""
    try:
        log("\n" + "=" * 80, "INFO")
        log("MEDIAJELLY LANGUAGE DETECTOR", "INFO")
        log("=" * 80 + "\n", "INFO")

        if migration_pending():
            log(MIGRATION_PENDING_MESSAGE, "ERROR")
            return

        # Ajustar nombres en pending antes de analizar (normalizar nomenclatura)
        try:
            adjust_pending_names()
        except Exception as _e:
            log(f"Advertencia: fallo ajustando nombres en pending: {_e}", "WARNING")

        # Los idiomas viven en la BD por huella; language_cache.json solo lo lee la migración
        cache: Dict[str, Any] = {}

        # Escanear archivos
        files_to_analyze = scan_files_needing_detection()

        # Filtrar archivos ya analizados
        files_pending = []
        for file_info in files_to_analyze:
            absolute_path = file_info["absolute_path"]
            streams = file_info["streams"]

            # Verificar qué streams ya tienen idioma guardado en la BD (por huella)
            known = {s[0]: get_cached_language(absolute_path, s[0]) for s in streams}
            pending_streams = [s for s in streams if known[s[0]] is None]
            already = {i: l for i, l in known.items() if l}
            if pending_streams:
                # Un solo remux por archivo: los idiomas conocidos viajan a process_files
                files_pending.append(
                    {
                        "absolute_path": absolute_path,
                        "relative_path": file_info["relative_path"],
                        "streams": pending_streams,
                        "known": already,
                    }
                )
            elif already:
                # Idiomas ya detectados antes (p. ej. antes de un renombrado): escribirlos si el archivo está procesado
                retag_if_processed(absolute_path, already)

        if files_pending:
            log(f"Archivos pendientes de análisis: {len(files_pending)}", "INFO")
            process_files(files_pending, cache)
        else:
            log("Todos los archivos ya están analizados", "SUCCESS")

        log("\n" + "=" * 80, "INFO")
        log("ANÁLISIS COMPLETADO", "SUCCESS")
        log("=" * 80 + "\n", "INFO")

    except KeyboardInterrupt:
        log("Ejecución interrumpida por el usuario", "WARNING")
        sys.exit(1)
    except Exception as e:
        log(f"Error fatal en la ejecución: {e}", "ERROR")
        traceback.print_exc()
        sys.exit(1)


def detect_language_with_whisper_for_processor(file_path: Union[str, Path], stream_index: int) -> Optional[str]:
    """
    Función wrapper para que el procesador pueda detectar idiomas usando Whisper.
    Retorna el código de idioma ISO 639-2 (3 letras) o None si falla.

    Args:
        file_path: Ruta al archivo multimedia
        stream_index: Índice del stream de audio a analizar

    Returns:
        Optional[str]: Código ISO 639-2 del idioma detectado (ej: 'eng', 'spa', 'jpn') o None
    """
    try:
        file_path_obj = validate_file_path(file_path)

        # Verificar si ya está en caché
        cached = get_cached_language(file_path_obj, stream_index)
        if cached:
            log(f"Idioma desde caché: {cached} para stream {stream_index}", "INFO")
            return cached

        # Obtener duración del archivo
        duration = get_file_duration(file_path_obj)
        if not duration or duration < 10:
            log(f"Archivo muy corto o duración inválida: {duration}s", "WARNING")
            return None

        # Cargar modelo Whisper
        model = load_whisper_model()
        if not model:
            log("No se pudo cargar el modelo Whisper", "ERROR")
            return None

        # Detectar idioma con múltiples muestras
        detected_lang = detect_language_with_multiple_samples(model, file_path_obj, stream_index, duration)

        if detected_lang:
            # Guardar en caché
            set_cached_language(file_path_obj, stream_index, detected_lang)
            log(f"Idioma detectado y guardado en caché: {detected_lang} para stream {stream_index}", "SUCCESS")
            return detected_lang
        else:
            log(f"No se pudo detectar idioma para stream {stream_index}", "WARNING")
            return None

    except Exception as e:
        log(f"Error en detect_language_with_whisper_for_processor: {e}", "ERROR")
        traceback.print_exc()
        return None


if __name__ == "__main__":
    main()
