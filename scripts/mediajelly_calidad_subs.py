"""Métricas de calidad de una transcripción y del desfase entre audio y video.

Funciones puras, salvo ``tramos_de_voz``, que usa el VAD de faster-whisper y se
importa en diferido para que los tests no lo necesiten.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Tuple

from mediajelly_alucinaciones import MIN_BUCLE, es_alucinacion

if TYPE_CHECKING:  # pragma: no cover
    import numpy as np

Cue = Dict[str, Any]
Tramo = Tuple[float, float]

COBERTURA_MIN = 0.75
ALUCINACIONES_MAX = 2
LOGPROB_MIN = -0.8
DESFASE_MAX_SEGUNDOS = 5.0
# Umbral del VAD para medir la voz: más estricto que el de la transcripción, para
# que la música o el ruido no cuenten como voz sin subtitular.
VAD_UMBRAL_MEDICION = 0.4


@dataclass
class Evaluacion:
    """Resultado de evaluar una transcripción.

    Attributes:
        ok: True si pasa todos los umbrales.
        cobertura: Fracción de la voz cubierta por algún cue.
        motivos: Umbrales que no se cumplen, en texto legible.
    """

    ok: bool
    cobertura: float
    motivos: List[str] = field(default_factory=list)


def _fusionar(tramos: Sequence[Tramo]) -> List[Tramo]:
    """Une los tramos solapados para no contar dos veces el mismo segundo."""
    fusionados: List[Tramo] = []
    for inicio, fin in sorted((float(a), float(b)) for a, b in tramos if float(b) > float(a)):
        if fusionados and inicio <= fusionados[-1][1]:
            fusionados[-1] = (fusionados[-1][0], max(fusionados[-1][1], fin))
        else:
            fusionados.append((inicio, fin))
    return fusionados


def cobertura_voz(cues: Sequence[Cue], tramos_voz: Sequence[Tramo]) -> float:
    """Fracción de los segundos con voz que caen dentro de algún cue.

    Args:
        cues: Cues con 'start' y 'end' en segundos.
        tramos_voz: Tramos (inicio, fin) donde el VAD detectó voz.

    Returns:
        Valor entre 0 y 1; 1.0 si no hay voz.
    """
    voz = _fusionar(tramos_voz)
    total = sum(fin - inicio for inicio, fin in voz)
    if total <= 0:
        return 1.0
    cubiertos = _fusionar([(c["start"], c["end"]) for c in cues])
    solapado = 0.0
    for v_ini, v_fin in voz:
        for c_ini, c_fin in cubiertos:
            solapado += max(0.0, min(v_fin, c_fin) - max(v_ini, c_ini))
    return min(1.0, solapado / total)


def alucinaciones(cues: Sequence[Cue]) -> int:
    """Cuenta los cues que parecen inventados por Whisper.

    Suma los créditos o cues sin texto (``es_alucinacion``) y, en cada racha de
    MIN_BUCLE o más cues seguidos con el mismo texto, uno por cada repetición
    desde la tercera.
    """
    total = 0
    anterior = None
    racha = 0
    for cue in cues:
        texto = str(cue.get("text", "")).strip()
        if es_alucinacion(texto):
            total += 1
            anterior, racha = None, 0
            continue
        clave = texto.lower()
        racha = racha + 1 if clave == anterior else 1
        anterior = clave
        if racha >= MIN_BUCLE:
            total += 1
    return total


def evaluar(cues: Sequence[Cue], tramos_voz: Sequence[Tramo], logprob_medio: Optional[float]) -> Evaluacion:
    """Decide si una transcripción es aceptable según los umbrales.

    Args:
        cues: Segmentos de la transcripción, sin filtrar.
        tramos_voz: Tramos de voz del audio.
        logprob_medio: Media de avg_logprob de los segmentos; None si no se conoce.

    Returns:
        Evaluacion con los motivos de cada umbral incumplido.
    """
    cobertura = cobertura_voz(cues, tramos_voz)
    motivos: List[str] = []
    if cobertura < COBERTURA_MIN:
        motivos.append(f"cobertura {cobertura:.2f} < {COBERTURA_MIN}")
    n_alucinaciones = alucinaciones(cues)
    if n_alucinaciones > ALUCINACIONES_MAX:
        motivos.append(f"alucinaciones {n_alucinaciones} > {ALUCINACIONES_MAX}")
    if logprob_medio is not None and logprob_medio < LOGPROB_MIN:
        motivos.append(f"logprob {logprob_medio:.2f} < {LOGPROB_MIN}")
    return Evaluacion(ok=not motivos, cobertura=cobertura, motivos=motivos)


def _duracion(valor: Any) -> Optional[float]:
    try:
        duracion = float(valor)
    except (TypeError, ValueError):
        return None
    return duracion if duracion > 0 else None


def desfase_audio(probe_data: Optional[Dict[str, Any]]) -> Optional[float]:
    """Diferencia con signo (audio - video) de la pista de audio más desfasada.

    Args:
        probe_data: Salida JSON de ffprobe con 'streams' y, opcionalmente, 'format'.

    Returns:
        Segundos (negativo si el audio es más corto) o None si faltan datos.
    """
    if not probe_data:
        return None
    streams = probe_data.get("streams") or []
    videos = [s for s in streams if s.get("codec_type") == "video"]
    if not videos:
        return None
    video = _duracion(videos[0].get("duration")) or _duracion((probe_data.get("format") or {}).get("duration"))
    if video is None:
        return None
    diferencias = [
        d - video
        for d in (_duracion(s.get("duration")) for s in streams if s.get("codec_type") == "audio")
        if d is not None
    ]
    if not diferencias:
        return None
    return max(diferencias, key=abs)


def tramos_de_voz(audio: "np.ndarray", sr: int = 16000) -> List[Tramo]:
    """Tramos de voz de un audio mono con el VAD Silero de faster-whisper.

    Args:
        audio: Muestras float32 a ``sr`` Hz.
        sr: Frecuencia de muestreo.

    Returns:
        Lista de (inicio, fin) en segundos.
    """
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    marcas = get_speech_timestamps(audio, VadOptions(threshold=VAD_UMBRAL_MEDICION), sampling_rate=sr)
    return [(m["start"] / sr, m["end"] / sr) for m in marcas]
