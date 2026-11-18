"""Filtro de alucinaciones de Whisper en los subtítulos transcritos.

Funciones puras, sin disco. Solo se aplican a lo que sale de Whisper: la
rotulación de un ASS también repite cues a propósito y no debe tocarse.
"""

import re
from typing import Any, Dict, List, Tuple

# Un carácter repetido más de 5 veces se deja en 3 ("ahhhhhhh" -> "ahhh").
_CARACTER_REPETIDO_RE = re.compile(r"(.)\1{5,}")
# Una palabra repetida más de 6 veces seguidas se deja en 3 ("no, no, no, ...").
_PALABRA_REPETIDA_RE = re.compile(r"\b(\w+)\b((?:[,\s]+)\1\b){6,}", re.IGNORECASE)
# Créditos que Whisper inventa al final o en los silencios (los aprendió de subtítulos amateur).
_CREDITOS_RE = re.compile(
    r"amara\.org|argenteam|ご視聴ありがとうございました|チャンネル登録|thanks? for watching"
    r"|suscr[ií]bete|subt[ií]tulos (?:realizados )?por",
    re.IGNORECASE,
)
_ETIQUETAS_RE = re.compile(r"<[^>]*>|\{[^}]*\}")
# A partir de cuántos cues seguidos con el mismo texto se considera un bucle.
MIN_BUCLE = 3


def _visible(texto: str) -> str:
    return _ETIQUETAS_RE.sub("", texto).strip()


def limpiar_texto(texto: str) -> str:
    """Reduce los caracteres y las palabras repetidas de más a 3."""
    texto = _CARACTER_REPETIDO_RE.sub(lambda m: m.group(1) * 3, texto)

    def tres(m: re.Match) -> str:
        # El texto original hasta la tercera aparición: conserva mayúsculas y comas
        apariciones = list(re.finditer(rf"\b{re.escape(m.group(1))}\b", m.group(0), re.IGNORECASE))
        return m.group(0)[: apariciones[2].end()]

    return _PALABRA_REPETIDA_RE.sub(tres, texto)


def es_alucinacion(texto: str) -> bool:
    """True si el cue no tiene letras ni números, o es un crédito inventado."""
    visible = _visible(texto)
    if not any(ch.isalnum() for ch in visible):
        return True
    return bool(_CREDITOS_RE.search(visible))


def filtrar_cues(cues: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    """Limpia una lista de cues (dicts con 'text') y quita alucinaciones y bucles.

    De cada racha de MIN_BUCLE o más cues seguidos con el mismo texto deja solo
    el primero: Whisper repite la última frase cuando pierde el audio.

    Returns:
        (cues filtrados, número de cues quitados o modificados).
    """
    cambios = 0
    limpios: List[Dict[str, Any]] = []
    for cue in cues:
        original = str(cue.get("text", "")).strip()
        if not original:
            limpios.append(cue)
            continue
        if es_alucinacion(original):
            cambios += 1
            continue
        texto = limpiar_texto(original)
        if texto != original:
            cambios += 1
            cue = {**cue, "text": texto}
        limpios.append(cue)

    resultado: List[Dict[str, Any]] = []
    i = 0
    while i < len(limpios):
        j = i
        clave = _visible(str(limpios[i].get("text", ""))).lower()
        while j + 1 < len(limpios) and clave and _visible(str(limpios[j + 1].get("text", ""))).lower() == clave:
            j += 1
        racha = j - i + 1
        if racha >= MIN_BUCLE:
            resultado.append(limpios[i])
            cambios += racha - 1
        else:
            resultado.extend(limpios[i:j + 1])
        i = j + 1
    return resultado, cambios


def filtrar_srt(contenido: str) -> Tuple[str, int]:
    """Aplica filtrar_cues a un SRT ya escrito; conserva tiempos y renumera.

    Si no hay cambios, devuelve el contenido sin tocar.
    """
    bloques = [b.splitlines() for b in re.split(r"\n\s*\n", contenido.strip()) if b.strip()]
    # Un bloque sin línea de tiempos no es un cue: al reescribir se descarta
    cues = [
        {"tiempo": lineas[1], "text": "\n".join(lineas[2:])}
        for lineas in bloques
        if len(lineas) >= 2 and "-->" in lineas[1]
    ]
    filtrados, cambios = filtrar_cues(cues)
    if not cambios:
        return contenido, 0

    salida = [f"{n}\n{c['tiempo']}\n{c['text']}" for n, c in enumerate(filtrados, start=1)]
    return "\n\n".join(salida) + "\n", cambios
