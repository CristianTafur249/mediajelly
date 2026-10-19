"""Política de pistas de MediaJelly: qué audio y qué subtítulos se conservan.

Funciones puras, sin ffprobe ni disco. Las usa el procesador al comprimir y el
panel para mostrar de antemano lo que hará el procesador, así que cualquier
cambio aquí cambia lo que hace la compresión en producción.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

# Códecs de subtítulos de texto que se pueden convertir a mov_text. Los de
# imagen (hdmv_pgs_subtitle, dvd_subtitle...) no entran en un MP4.
TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text"}

# Idiomas que reconoce la política. Se compara por palabra completa (nunca por
# subcadena: "ces" o "est" no son español). En la etiqueta de idioma cuenta el
# código del principio ("es-419" -> "es"); en títulos, cualquier palabra.
CODIGOS_IDIOMA = {
    "spa": {"spa", "es", "esp", "lat"},
    "eng": {"eng", "en"},
    "jpn": {"jpn", "ja", "jap"},
}
PALABRAS_IDIOMA = {
    "spa": {"spanish", "español", "espanol", "castellano", "latino"},
    "eng": {"english", "inglés", "ingles"},
    "jpn": {"japanese", "japonés", "japones"},
}
# En los metadatos de una pista (códec, título, handler) no se usan los códigos
# de dos letras: "es" o "en" son palabras comunes en un título.
_CODIGOS_METADATOS = {
    "spa": {"spa", "esp", "lat"},
    "eng": {"eng"},
    "jpn": {"jpn", "jap"},
}
_PALABRA = re.compile(r"[a-z0-9ñáéíóúü]+")
# Otros códigos de dos letras -> ISO 639-2, como el mapa por defecto de
# _ensure_three_letter_codes (spa, eng y jpn los resuelve idioma_base).
_DOS_A_TRES = {
    "fr": "fra", "de": "deu", "it": "ita", "pt": "por", "zh": "chi",
    "ru": "rus", "ar": "ara", "ko": "kor",
}

# Variantes regionales. El MP4 solo guarda el idioma como ISO 639-2 ("spa") y
# descarta el título de la pista: la variante va como nombre de la pista
# (handler_name), que es lo que muestran Jellyfin y el Roku.
# (nombre, idiomas ISO de la región, códigos de la etiqueta, palabras del título)
# Por título, la región solo cuenta si la pista es de ese idioma o no se sabe:
# una pista "eng" titulada "Spain commentary" no es español de España.
_REGIONES = (
    ("Español (Latinoamérica)", {"spa"},
     {"es-419", "es-mx", "es-ar", "es-co", "es-cl", "es-pe", "es-ve", "es-us", "es-la", "spa-lat", "lat"},
     {"latino", "latina", "latinoamérica", "latinoamerica", "latam", "lat", "mexicano", "méxico", "mexico"}),
    ("Español (España)", {"spa"}, {"es-es", "spa-es"}, {"castellano", "castilian", "españa", "espana", "spain"}),
    ("English (US)", {"eng"}, {"en-us"}, {"american"}),
    ("English (UK)", {"eng"}, {"en-gb", "en-uk"}, {"british"}),
    ("Português (Brasil)", {"por"}, {"pt-br", "por-br"}, {"brazilian", "brasil", "brasileiro", "brasileño"}),
    ("Português (Portugal)", {"por"}, {"pt-pt", "por-pt"}, {"europeu"}),
    ("Français (Canada)", {"fre", "fra"}, {"fr-ca", "fre-ca", "fra-ca"}, {"canadien", "québécois", "quebecois"}),
)
# Frases de título que indican la región aunque sus palabras sueltas no basten.
_FRASES_REGION = {"latin america": "Español (Latinoamérica)", "latin american": "Español (Latinoamérica)"}
# Nombres que ponen los programas por defecto: no describen la pista.
_HANDLERS_POR_DEFECTO = re.compile(r"handler|core media|produced by", re.IGNORECASE)
# Programas de muxing que ponen el nombre del archivo de origen como nombre de
# pista ("Jason.bourne.lat_fixed50.aac"): sirve para buscar la región, pero no
# como nombre.
_PARECE_ARCHIVO = re.compile(
    r"\.(aac|ac3|eac3|dts|thd|truehd|flac|mka|mp3|opus|wav|h264|h265|264|265|hevc|mkv|mp4|m4a|srt|ass|ssa)$",
    re.IGNORECASE,
)

# Archivos de más de 5 GiB o 4K/UHD: se omiten los subtítulos para ahorrar memoria.
UMBRAL_ARCHIVO_GRANDE = 5 * 1024**3
UHD_WIDTH_THRESHOLD = 3800   # cubre 3840x2160 (2160p) y variantes cercanas
UHD_HEIGHT_THRESHOLD = 2000


@dataclass
class PlanPistas:
    """Pistas que conserva la compresión de un archivo."""

    audio: List[int]
    idiomas_audio: Dict[int, str]
    subtitulos_espanol: List[int]
    omitir_subtitulos: bool
    # Índice de audio -> nombre de la pista (variante regional o título original).
    nombres_audio: Dict[int, str] = field(default_factory=dict)


def es_anime(ruta: Path) -> bool:
    """True si la ruta del archivo indica que es anime."""
    return 'anime' in str(ruta).lower()


def es_uhd(ancho: Optional[int], alto: Optional[int]) -> bool:
    """True si la resolución corresponde a 4K/UHD (2160p+). Sin dato cuenta como 0."""
    return (ancho or 0) >= UHD_WIDTH_THRESHOLD or (alto or 0) >= UHD_HEIGHT_THRESHOLD


def omitir_subtitulos(tamano_bytes: int, ancho: Optional[int], alto: Optional[int]) -> bool:
    """True si el archivo pesa más de 5 GiB o es 4K/UHD: la compresión omite los subtítulos."""
    return tamano_bytes > UMBRAL_ARCHIVO_GRANDE or es_uhd(ancho, alto)


def _palabras(texto: Optional[str]) -> List[str]:
    # NFC: "Español" con la tilde como carácter combinado (NFD) es la misma palabra
    return _PALABRA.findall(unicodedata.normalize("NFC", str(texto or "")).lower())


def idioma_base(idioma: Optional[str]) -> Optional[str]:
    """'spa', 'eng' o 'jpn' si la etiqueta de idioma es de uno de ellos; si no, None.

    Acepta códigos de 2 o 3 letras con región ("es-419", "en-us") y nombres
    completos ("Spanish", "japonés"), sin mayúsculas/minúsculas.
    """
    palabras = _palabras(idioma)
    if not palabras:
        return None
    for base in ("spa", "eng", "jpn"):
        if palabras[0] in CODIGOS_IDIOMA[base] or any(p in PALABRAS_IDIOMA[base] for p in palabras):
            return base
    return None


def idioma_iso(idioma: Optional[str]) -> Optional[str]:
    """Código ISO 639-2 (3 letras) que se escribe en el MP4, o None si no se conoce.

    spa/eng/jpn por idioma_base ("es-419" -> "spa", "Castellano" -> "spa",
    "japanese" -> "jpn"); otro código de 3 letras se deja tal cual ("fre",
    "kor"); uno de 2 letras conocido se convierte ("fr-ca" -> "fra"). Lo
    demás ("xx-yy", "und", vacío) da None: no se fuerza idioma.
    """
    base = idioma_base(idioma)
    if base:
        return base
    palabras = _palabras(idioma)
    if not palabras or palabras[0] == "und":
        return None
    codigo = palabras[0]  # "fre-ca" -> "fre"
    if re.fullmatch(r"[a-z]{3}", codigo):
        return codigo
    return _DOS_A_TRES.get(codigo)


def _codigo_region(idioma: Optional[str]) -> str:
    """Etiqueta normalizada para comparar regiones: "es_MX" -> "es-mx"."""
    return unicodedata.normalize("NFC", str(idioma or "")).strip().lower().replace("_", "-")


def nombre_pista(idioma: Optional[str], titulo: Optional[str] = None) -> Optional[str]:
    """Nombre que se escribe en la pista: su variante regional o el título original.

    La región sale de la etiqueta ("es-419", "es-MX", "lat", "pt-BR") o del
    título ("Latino 5.1", "Castellano", "Spanish (Latin America)") y da un nombre
    fijo ("Español (Latinoamérica)"). Sin región se respeta el título original;
    sin título (o con uno por defecto como "SoundHandler" o un nombre de
    archivo como "pelicula.eng.ac3"), None.
    """
    codigo = _codigo_region(idioma)
    iso = idioma_iso(idioma)
    palabras = set(_palabras(titulo))
    texto = " ".join(_palabras(titulo))
    for nombre, idiomas_region, codigos, palabras_region in _REGIONES:
        if codigo in codigos:
            return nombre
        if palabras & palabras_region and (iso is None or iso in idiomas_region):
            return nombre
    for frase, nombre in _FRASES_REGION.items():
        if frase in texto and (iso is None or iso == "spa"):
            return nombre
    titulo = (titulo or "").strip()
    if not titulo or _HANDLERS_POR_DEFECTO.search(titulo) or _PARECE_ARCHIVO.search(titulo):
        return None
    return titulo


def es_espanol(idioma: Optional[str]) -> bool:
    """True si la etiqueta de idioma de una pista es español (código exacto o palabra completa)."""
    return idioma_base(idioma) == "spa"


def idioma_por_metadatos(texto: Optional[str]) -> Optional[str]:
    """'spa', 'eng' o 'jpn' según el códec/título/handler de una pista; None si no lo dicen.

    El orden de búsqueda es español, inglés y japonés, como hacía el procesador.
    """
    palabras = set(_palabras(texto))
    for base in ("spa", "eng", "jpn"):
        if palabras & (_CODIGOS_METADATOS[base] | PALABRAS_IDIOMA[base]):
            return base
    return None


def planificar_pistas(
    audio_languages: Dict[int, str],
    audio_espanol: List[int],
    subs_espanol: List[int],
    es_anime: bool,
    archivo_grande_o_uhd: bool,
    titulos_audio: Optional[Dict[int, str]] = None,
) -> PlanPistas:
    """Elige las pistas de audio según las reglas acordadas y decide los subtítulos.

    Audio: todas las pistas en español, en su orden. Si no hay, una sola:
    anime -> la primera japonesa, si no la primera inglesa; el resto -> la
    primera inglesa; si no hay, la pista 0. Cada pista conservada lleva su
    idioma como código ISO 639-2 (idioma_iso: "es-419" -> "spa"); si no se
    conoce (vacío, "und", "xx-yy") no se fuerza ninguno.

    Args:
        audio_languages: índice relativo de audio -> idioma (de TODAS las pistas).
        audio_espanol: índices de audio en español (los que da detect_language_streams).
        subs_espanol: índices de subtítulos en español.
        es_anime: si el archivo es anime (prefiere japonés cuando no hay español).
        archivo_grande_o_uhd: archivo de más de 5 GB o 4K/UHD; se omiten los
            subtítulos para ahorrar memoria.
        titulos_audio: índice relativo de audio -> título original de la pista.
            Con la etiqueta, da el nombre de cada pista conservada (nombre_pista).
    """
    titulos_audio = titulos_audio or {}
    if audio_espanol:
        audio = list(audio_espanol)
    else:
        preferidos = ("jpn", "eng") if es_anime else ("eng",)
        audio = [0]
        for base in preferidos:
            candidatos = [i for i in sorted(audio_languages) if idioma_base(audio_languages[i]) == base]
            if candidatos:
                audio = [candidatos[0]]
                break

    idiomas_audio = {}
    for idx in audio:
        codigo = idioma_iso(audio_languages.get(idx))
        if codigo:
            idiomas_audio[idx] = codigo
    nombres_audio = {}
    for idx in audio:
        nombre = nombre_pista(audio_languages.get(idx), titulos_audio.get(idx))
        if nombre:
            nombres_audio[idx] = nombre

    if archivo_grande_o_uhd:
        return PlanPistas(audio, idiomas_audio, [], True, nombres_audio)
    return PlanPistas(audio, idiomas_audio, list(subs_espanol), False, nombres_audio)


def _es_texto(codec: Optional[str]) -> bool:
    return bool(codec) and codec.lower() in TEXT_SUB_CODECS


def elegir_subtitulos(
    subs_espanol: List[int], codecs: List[Optional[str]], omitir: bool = False
) -> List[int]:
    """Subtítulos que se mapean al MP4.

    Si se omiten (archivo grande o UHD), ninguno. Si hay pistas en español, se quedan las que son de texto (si ninguna lo es,
    no se mapea nada: no se cae a otro idioma). Si no hay ninguna en español,
    el primer subtítulo de texto disponible. Si no, ninguno.

    Args:
        subs_espanol: índices relativos de subtítulos en español.
        codecs: códec de cada subtítulo, en orden de índice relativo.
        omitir: el plan omite los subtítulos (PlanPistas.omitir_subtitulos).
    """
    if omitir:
        return []
    if subs_espanol:
        return [
            idx for idx in subs_espanol
            if _es_texto(codecs[idx] if idx < len(codecs) else None)
        ]
    for i, codec in enumerate(codecs):
        if _es_texto(codec):
            return [i]
    return []
