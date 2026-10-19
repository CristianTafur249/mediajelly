#!/usr/bin/env python3
"""
mediajelly_language_detection.py

Mixin con la detección de idioma de pistas de audio usando Whisper
(carga lazy del modelo, muestreo múltiple del archivo, idiomas guardados
por huella en la BD, normalización a códigos ISO de 3 letras).

Extraído de MediaJellyProcessor (mediajelly_processor.py) para reducir el
tamaño de esa clase y aislar esta responsabilidad.

Requiere que la clase que lo use tenga disponibles (provistos por
MediaJellyProcessor.__init__): `self.logger`, `self.whisper_enabled`,
`self.whisper_model` y demás configuración de muestreo (max_samples_per_file, etc.) usada por
estos métodos.
"""

import json
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from mediajelly_emoji import EmojiGenerator
from mediajelly_politica import es_espanol, idioma_por_metadatos

SHORT_TIMEOUT = 10
try:
    from mediajelly_config import get_config
except Exception:
    get_config = None

try:
    from mediajelly_language_detector import (
        get_cached_language,
        set_cached_language,
        detect_language_with_whisper_for_processor,
    )
    LANGUAGE_DETECTOR_AVAILABLE = True
except Exception:
    LANGUAGE_DETECTOR_AVAILABLE = False
    get_cached_language = None
    set_cached_language = None
    detect_language_with_whisper_for_processor = None

class LanguageDetectionMixin:
    """Detección de idioma de pistas de audio mediante Whisper."""

    def _load_whisper_model(self):
        """Carga el modelo Whisper de forma lazy (solo cuando se necesita)"""
        if not self.whisper_enabled:
            return False

        if self.whisper_model is None:
            try:
                self.logger.info(f"{EmojiGenerator.refresh()} Cargando modelo Whisper (base)...")
                # Usar modelo 'base' (ya descargado - 140MB)
                # Se carga ANTES de iniciar compresiones para evitar conflictos de memoria
                try:
                    import whisper as _whisper  # type: ignore
                    self.whisper_model = _whisper.load_model("base")
                except Exception as e:
                    self.logger.error(f"Error importando/cargando Whisper: {e}")
                    raise
                self.logger.info(f"{EmojiGenerator.success()} Modelo Whisper cargado exitosamente")
                return True
            except Exception as e:
                import traceback

                self.logger.error(f"{EmojiGenerator.error()} Error cargando modelo Whisper: {e}")
                self.logger.error(f"Traceback: {traceback.format_exc()}")
                # Deshabilitar permanentemente para esta sesión si falla
                self.whisper_enabled = False
                self.whisper_model = None
                return False
        return True

    def _ensure_three_letter_codes(self, audio_languages: Dict[int, str]) -> Dict[int, str]:
        """Asegura que los valores de audio_languages sean códigos ISO 639-2 (tres letras).

        Usa la configuración para invertir el mapeo si se encuentran códigos ISO 639-1 (dos letras).
        Agrega logging que permita depurar si los valores no están en el formato esperado.
        """
        if not audio_languages:
            return {}

        try:
            cfg = get_config() if get_config else None
            inv_map = {}
            if cfg:
                inv_map = {v: k for k, v in cfg.language_detection.language_code_map.items()}
            # Fallback a small default map en caso de no tener configuración
            if not inv_map:
                inv_map = {
                    'en': 'eng',
                    'es': 'spa',
                    'fr': 'fra',
                    'de': 'deu',
                    'it': 'ita',
                    'pt': 'por',
                    'ja': 'jpn',
                    'zh': 'chi',
                    'ru': 'rus',
                    'ar': 'ara',
                }

            new_map = {}
            for idx, val in audio_languages.items():
                if not val:
                    new_map[idx] = val
                    continue
                # val puede ser ya 3 letras (p.ej. 'eng') o 2 letras ('en')
                if len(val) == 3:
                    new_map[idx] = val
                elif len(val) == 2 and val in inv_map:
                    new_map[idx] = inv_map[val]
                    self.logger.info(
                        f"{EmojiGenerator.clipboard()} Mapeando código idioma 2-letras '{val}' a 3-letras '{new_map[idx]}' para stream {idx}"
                    )
                else:
                    # No se reconoce, dejar tal cual y avisar
                    new_map[idx] = val
                    self.logger.warning(
                        f"{EmojiGenerator.warning_msg()} Código idioma inesperado '{val}' para stream {idx}, no se pudo mapear a 3 letras"
                    )
            return new_map
        except Exception as e:
            self.logger.warning(f"{EmojiGenerator.warning_msg()} Error mapeando idiomas a 3-letras: {e}")
            return audio_languages

    def _detect_language_with_whisper(self, audio_file: Path, file_duration: float = 0) -> Tuple[Optional[str], Dict[str, float]]:
        """
        Detecta el idioma usando Whisper analizando el archivo de audio.
        Retorna el código ISO 639-2 del idioma detectado y las probabilidades.
        """
        try:
            if not self._load_whisper_model():
                return None, {}

            # Mapeo de códigos ISO 639-1 a ISO 639-2 (usado por ffmpeg)
            lang_map = {
                "es": "spa",
                "en": "eng",
                "ja": "jpn",
                "fr": "fra",
                "de": "deu",
                "it": "ita",
                "pt": "por",
                "ko": "kor",
                "zh": "chi",
                "ru": "rus",
                "ar": "ara",
            }

            # Cargar audio
            audio = whisper.load_audio(str(audio_file))
            audio = whisper.pad_or_trim(audio)

            # Convertir a mel spectrogram
            mel = whisper.log_mel_spectrogram(audio).to(self.whisper_model.device)

            # Detectar idioma
            _, probs = self.whisper_model.detect_language(mel)

            # Obtener probabilidades de idiomas relevantes
            spanish_prob = probs.get("es", 0.0)

            # Obtener top 5 idiomas
            sorted_langs = sorted(probs.items(), key=lambda x: x[1], reverse=True)[:5]
            detected_lang = sorted_langs[0][0]
            confidence = sorted_langs[0][1]

            # Log solo del resultado principal (no mostrar top-5 para evitar saturar logs)
            lang_code = lang_map.get(detected_lang, detected_lang)
            # self.logger.info(f"   Whisper detectó: {detected_lang} ({lang_code}): {confidence:.2%}")

            # Retornar idioma detectado y probabilidades para votación
            return detected_lang, probs

        except Exception as e:
            self.logger.error(f"Error en detección Whisper: {e}")
            return None, {}

    def _detect_language_with_multiple_samples(
        self, file_path: Path, stream_index: int, total_duration: float
    ) -> Optional[str]:
        """
        Detecta idioma extrayendo 10 muestras aleatorias de 60s y votando por el idioma más detectado.
        Guarda archivos temporales en tmp/ y los limpia al finalizar.
        """
        import random

        # Mapeo de códigos ISO 639-1 a ISO 639-2
        lang_map = {
            "es": "spa",
            "en": "eng",
            "ja": "jpn",
            "fr": "fra",
            "de": "deu",
            "it": "ita",
            "pt": "por",
            "ko": "kor",
            "zh": "chi",
            "ru": "rus",
            "ar": "ara",
        }

        try:
            # Verificar que el archivo tenga suficiente duración
            if total_duration < 120:  # Menos de 2 minutos
                self.logger.info(
                    f"{EmojiGenerator.warning_msg()} Archivo corto ({total_duration:.0f}s), usando muestra única"
                )
                num_samples = 1
                sample_duration = min(30, total_duration - 10)
            else:
                num_samples = min(10, int(total_duration / 60))  # Máximo 10 muestras
                sample_duration = 60

            self.logger.info(
                f"{EmojiGenerator.audio()} Analizando idioma con {num_samples} muestras de {sample_duration}s"
            )

            # Generar timestamps aleatorios (evitando primeros y últimos 30s)
            safe_start = 30
            safe_end = total_duration - 30 - sample_duration

            if safe_end <= safe_start:
                timestamps = [30]
            else:
                timestamps = sorted(
                    random.sample(range(int(safe_start), int(safe_end)), min(num_samples, int(safe_end - safe_start)))
                )

            # Contador de votos por idioma
            language_votes: Dict[str, int] = {}
            temp_files = []

            for i, start_time in enumerate(timestamps):
                # Crear archivo temporal en tmp/
                temp_audio_path = self.tmp_dir / f"whisper_sample_{file_path.stem}_stream{stream_index}_sample{i}.wav"
                temp_files.append(temp_audio_path)

                try:
                    # Extraer muestra de audio
                    extract_cmd = [
                        "ffmpeg",
                        "-hide_banner",
                        "-y",
                        "-ss",
                        str(start_time),
                        "-i",
                        str(file_path),
                        "-map",
                        f"0:a:{stream_index}",
                        "-t",
                        str(sample_duration),
                        "-ac",
                        "1",
                        "-ar",
                        "16000",
                        "-f",
                        "wav",
                        str(temp_audio_path),
                    ]

                    result = subprocess.run(extract_cmd, capture_output=True, timeout=MEDIUM_TIMEOUT)

                    if result.returncode != 0:
                        self.logger.warning(
                            f"{EmojiGenerator.warning_msg()} Error extrayendo muestra {i+1} en segundo {start_time}"
                        )
                        continue

                    # Detectar idioma con Whisper
                    detection_result = self._detect_language_with_whisper(temp_audio_path, total_duration)

                    if detection_result and len(detection_result) == 2:
                        detected_lang, probs = detection_result

                        # Votar por el idioma detectado
                        if detected_lang:
                            language_votes[detected_lang] = language_votes.get(detected_lang, 0) + 1
                            # Log simplificado (solo cuando cambia el idioma dominante o cada 3 muestras)
                            if i == 0 or i % 3 == 0 or detected_lang != list(language_votes.keys())[-1]:
                                self.logger.info(
                                    f"   Muestra {i+1}/{len(timestamps)}: {detected_lang} ({probs.get(detected_lang, 0):.1%})"
                                )

                except Exception as e:
                    self.logger.warning(f"{EmojiGenerator.warning_msg()} Error procesando muestra {i+1}: {e}")
                    continue

            # Limpiar archivos temporales
            for temp_file in temp_files:
                try:
                    if temp_file.exists():
                        temp_file.unlink()
                except Exception as e:
                    self.logger.warning(
                        f"{EmojiGenerator.warning_msg()} Error eliminando archivo temporal {temp_file}: {e}"
                    )

            if not language_votes:
                self.logger.warning(f"{EmojiGenerator.warning_msg()} No se pudo detectar idioma en ninguna muestra")
                return "spa"  # Fallback a español

            # Determinar idioma ganador por votación
            winner_lang = max(language_votes.items(), key=lambda x: x[1])
            winner_code = lang_map.get(winner_lang[0], winner_lang[0])

            # Log resumido de votación (solo top 3)
            top_3_votes = sorted(language_votes.items(), key=lambda x: x[1], reverse=True)[:3]
            votes_summary = ", ".join([f"{lang_map.get(lang, lang)}:{votes}" for lang, votes in top_3_votes])
            self.logger.info(f"🏆 Votación: {votes_summary} ({len(timestamps)} muestras)")

            # Aplicar reglas específicas para contenido latino
            spanish_votes = language_votes.get("es", 0)
            total_votes = len(timestamps)

            # Si español tiene al menos 30% de los votos, usarlo
            if spanish_votes / total_votes >= 0.30:
                self.logger.info(
                    f"{EmojiGenerator.success()} Español con {spanish_votes}/{total_votes} votos ({spanish_votes/total_votes*100:.1f}%) - Usando español"
                )
                return "spa"

            # Si el ganador tiene menos del 50% de votos y español está presente, usar español
            if winner_lang[1] / total_votes < 0.50 and spanish_votes > 0:
                self.logger.info(
                    f"{EmojiGenerator.success()} Confianza baja del ganador ({winner_lang[1]}/{total_votes}), español presente - Usando español"
                )
                return "spa"

            self.logger.info(f"{EmojiGenerator.success()} Idioma final seleccionado: {winner_lang[0]} ({winner_code})")
            # Guardar el resultado en caché si está disponible la función
            try:
                if set_cached_language:
                    set_cached_language(str(file_path), stream_index, winner_code)
            except Exception as e:
                self.logger.warning(f"{EmojiGenerator.warning_msg()} No se pudo cachear idioma desde processor: {e}")
            return winner_code

        except Exception as e:
            self.logger.error(f"Error en detección multi-muestra: {e}")
            return "spa"  # Fallback a español

    def detect_audio_language(
        self, file_path: Path, stream_index: int, allow_whisper_fallback: bool = True
    ) -> Optional[str]:
        """
        Detecta el idioma de una pista de audio usando el idioma guardado en la BD.
        Si no está guardado, NO asume ningún idioma (retorna None).

        Args:
            allow_whisper_fallback: Si es False, no se carga un modelo Whisper
                cuando caché/metadatos no alcanzan (usado desde el worker de
                compresión, para no sumar la carga de Whisper a un proceso que
                ya está ejecutando ffmpeg y puede estar cerca del límite de
                memoria del contenedor). El paso dedicado
                mediajelly_language_detector.py sigue poblando el caché por
                separado.
        """
        try:
            file_path_resolved = file_path.resolve()

            # Paso 0: idioma guardado por huella en la BD (sobrevive a renombrados)
            if get_cached_language:
                db_lang = get_cached_language(file_path_resolved, stream_index)
                if db_lang:
                    self.logger.info(
                        f"{EmojiGenerator.folder()} Idioma desde BD: {db_lang} (stream {stream_index}) - {file_path.name}"
                    )
                    return db_lang

            # (language_cache.json ya no se consulta: va por ruta y puede traer el
            # idioma de otro archivo que estuvo en la misma ruta)

            # Paso 1: Intentar detección por metadatos (solo si hay metadatos claros).
            # stream_index es el índice absoluto: se selecciona con "N", no con
            # "a:N" (ese es el N-ésimo audio, otra pista o ninguna).
            cmd = [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                str(stream_index),
                "-show_entries",
                "stream=codec_name:stream_tags=title,handler_name",
                "-of",
                "csv=p=0",
                str(file_path),
            ]

            result = subprocess.run(cmd, capture_output=True, timeout=SHORT_TIMEOUT, encoding="utf-8", errors="replace")

            if result.returncode == 0 and result.stdout.strip():
                # Palabras completas (mediajelly_politica): "rest" o "best" no son español
                nombres = {"spa": "español", "eng": "inglés", "jpn": "japonés"}
                lang_meta = idioma_por_metadatos(result.stdout)
                if lang_meta:
                    self.logger.info(
                        f"{EmojiGenerator.clipboard()} Idioma detectado por metadatos ({nombres[lang_meta]}) en stream {stream_index}: {file_path.name}"
                    )
                    return lang_meta

            # Paso 2: Intentar detección con Whisper si está disponible
            if not allow_whisper_fallback:
                self.logger.debug(
                    f"{EmojiGenerator.warning_msg()} Stream {stream_index} sin idioma en caché/metadatos, "
                    f"Whisper deshabilitado en este contexto: {file_path.name}"
                )
                return None

            if LANGUAGE_DETECTOR_AVAILABLE:
                self.logger.info(
                    f"{EmojiGenerator.magnifying_glass()} Stream {stream_index} sin idioma en caché/metadatos, ejecutando detección con Whisper: {file_path.name}"
                )
                try:
                    detected_lang = detect_language_with_whisper_for_processor(file_path, stream_index)
                    if detected_lang:
                        self.logger.info(
                            f"{EmojiGenerator.success()} Idioma detectado con Whisper: {detected_lang} para stream {stream_index}"
                        )
                        return detected_lang
                    else:
                        self.logger.warning(
                            f"{EmojiGenerator.warning_msg()} Whisper no pudo detectar idioma para stream {stream_index}"
                        )
                except Exception as e:
                    self.logger.error(f"Error ejecutando detector de idiomas con Whisper: {e}")

            return None

        except Exception as e:
            self.logger.error(f"Error detectando idioma de audio en stream {stream_index} de {file_path}: {e}")
            return None  # No asumir idioma en caso de error

    def titulos_audio(self, file_path: Path) -> Dict[int, str]:
        """Título de cada pista de audio {índice relativo: título} (o su handler_name).

        Sirve para conservar la variante regional ("Latino", "Castellano") que el
        MP4 descartaría: ver mediajelly_politica.nombre_pista. Un fallo de
        ffprobe da {} (las pistas quedan sin nombre, como antes).
        """
        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-select_streams", "a",
                    "-show_entries", "stream=index:stream_tags=title,handler_name",
                    "-of", "json", str(file_path),
                ],
                capture_output=True, timeout=SHORT_TIMEOUT, encoding="utf-8", errors="replace",
            )
            pistas = json.loads(result.stdout or "{}").get("streams", [])
        except Exception as e:
            self.logger.debug(f"No se pudieron leer los títulos de audio de {file_path.name}: {e}")
            return {}
        titulos = {}
        for relativo, pista in enumerate(pistas):
            etiquetas = {k.lower(): v for k, v in (pista.get("tags") or {}).items()}
            titulo = (etiquetas.get("title") or etiquetas.get("handler_name") or "").strip()
            if titulo:
                titulos[relativo] = titulo
        return titulos

    def detect_language_streams(
        self, file_path: Path, allow_whisper_fallback: bool = True
    ) -> Tuple[bool, List[int], List[int], Dict[int, str]]:
        """
        Detecta streams de audio y subtítulos en español y devuelve índices.
        También detecta y etiqueta streams sin idioma.
        Retorna: (has_spanish, audio_indices, sub_indices, audio_languages)

        Args:
            allow_whisper_fallback: ver detect_audio_language().
        """
        try:
            # Comando para detectar todos los streams (audio y subtítulos)
            cmd = [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=index,codec_type:stream_tags=language",
                "-of",
                "csv=p=0",
                str(file_path),
            ]

            result = subprocess.run(cmd, capture_output=True, timeout=SHORT_TIMEOUT, encoding="utf-8", errors="replace")

            lines = result.stdout.strip().split("\n")
            audio_indices = []
            sub_indices = []
            audio_languages = {}  # Mapeo de índice relativo de audio a idioma
            audio_stream_counter = 0
            subtitle_stream_counter = 0
            seen_indices = set()  # Para evitar duplicados en archivos con múltiples programas

            # Solo loguear si hay streams sin etiquetar
            needs_detection = any(
                "und" in line or line.split(",")[2] == ""
                for line in lines
                if "," in line and len(line.split(",")) >= 3 and line.split(",")[0].isdigit()
            )
            if needs_detection:
                self.logger.info(f"{EmojiGenerator.magnifying_glass()} Detectando idiomas en {file_path.name}")

            for line in lines:
                if line.strip():
                    parts = line.split(",")
                    if len(parts) >= 2 and parts[0].isdigit():  # Verificar que el primer campo sea un número
                        index = int(parts[0])

                        # Evitar procesar el mismo stream múltiples veces (archivos con programas múltiples)
                        if index in seen_indices:
                            continue
                        seen_indices.add(index)

                        codec_type = parts[1] if len(parts) > 1 else ""
                        lang = parts[2].lower() if len(parts) > 2 else ""

                        if codec_type == "audio":
                            # Si no tiene etiqueta de idioma, detectarlo
                            if not lang or lang == "und":
                                self.logger.info(
                                    f"{EmojiGenerator.audio()} Stream audio {audio_stream_counter} (índice absoluto {index}) sin etiqueta ('{lang}'), iniciando detección..."
                                )
                                # Pasar el índice absoluto del stream
                                detected_lang = self.detect_audio_language(
                                    file_path, index, allow_whisper_fallback=allow_whisper_fallback
                                )
                                if detected_lang:
                                    lang = detected_lang
                                    self.logger.info(
                                        f"{EmojiGenerator.success()} Idioma detectado: {detected_lang} para stream audio {audio_stream_counter}"
                                    )
                                else:
                                    self.logger.warning(
                                        f"{EmojiGenerator.warning_msg()} No se pudo detectar idioma para stream audio {audio_stream_counter}"
                                    )

                            # Guardar el idioma del stream usando el índice relativo de audio.
                            # Si no se pudo detectar se deja "und": asumir "spa" escribía una
                            # etiqueta falsa en el archivo y el detector ya no lo re-analizaba.
                            if not lang:
                                lang = "und"
                            audio_languages[audio_stream_counter] = lang

                            # Verificar si es español (código exacto o palabra completa:
                            # "ces" o "est" no cuentan)
                            if es_espanol(lang):
                                audio_indices.append(audio_stream_counter)

                            audio_stream_counter += 1

                        elif codec_type == "subtitle":
                            # Usar un contador relativo para subtítulos (0:s:N espera índices relativos)
                            current_sub_index = subtitle_stream_counter
                            if es_espanol(lang):
                                sub_indices.append(current_sub_index)
                            # Incrementar contador relativo de subtítulos siempre
                            subtitle_stream_counter += 1

            has_spanish = len(audio_indices) > 0 or len(sub_indices) > 0

            return has_spanish, audio_indices, sub_indices, audio_languages

        except Exception as e:
            self.logger.error(f"Error detectando idiomas en {file_path}: {e}")
            return False, [], [], {}
