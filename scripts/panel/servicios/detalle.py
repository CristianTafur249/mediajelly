"""Detalle de un video: datos técnicos y qué pistas conservará el procesador.

Lee la BD en solo lectura y hace un único ffprobe. La decisión por pista la
toma mediajelly_politica con las mismas entradas que arma el procesador, así
que el panel muestra lo que hará la compresión sin repetir sus reglas.
"""
import json
import logging
import subprocess
from pathlib import Path

import mediajelly_politica as politica
from panel.servicios import bd
from panel.servicios.cola import hora_local

registro = logging.getLogger(__name__)

IDIOMAS_PERMITIDOS = ("spa", "eng", "jpn", "kor", "chi", "fre", "ger", "ita", "por", "rus")
TIMEOUT_FFPROBE = 30

_CONSULTA_ARCHIVO = (
    "SELECT path, status, attempts, last_error, derived_from, created_at, updated_at, prioridad "
    "FROM media_files WHERE fingerprint = ?"
)


def _leer_bd(fp: str):
    """(archivo, {índice absoluto: idioma detectado}) en solo lectura; (None, {}) si no está."""
    with bd.conexion_solo_lectura() as conn:
        if conn is None:
            return None, {}
        fila = conn.execute(_CONSULTA_ARCHIVO, (fp,)).fetchone()
        if fila is None:
            return None, {}
        ruta, estado, intentos, error, derivado, creado, actualizado, prioridad = fila
        try:
            idiomas = {int(i): lang for i, lang in conn.execute(
                "SELECT stream_index, lang FROM stream_languages WHERE fingerprint = ?", (fp,))}
        except Exception as exc:  # BD antigua sin la tabla
            registro.info("Sin idiomas detectados para %s: %s", fp, exc)
            idiomas = {}
    archivo = {
        "fingerprint": fp, "ruta": ruta, "nombre": Path(ruta).name, "estado": estado,
        "intentos": intentos or 0, "error": error, "derived_from": derivado,
        "creado": hora_local(creado) if creado else creado,
        "actualizado": hora_local(actualizado) if actualizado else actualizado,
        "prioridad": prioridad or 0,
    }
    return archivo, idiomas


def _sondear(ruta: Path):
    """(datos de ffprobe, None) o (None, mensaje de error en español)."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries",
        "format=duration,size,bit_rate:stream=index,codec_type,codec_name,width,height,channels,bit_rate"
        ":stream_tags=language,title,handler_name",
        "-of", "json", str(ruta),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT_FFPROBE,
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return None, f"ffprobe no respondió en {TIMEOUT_FFPROBE} s"
    except OSError as exc:
        return None, f"No se pudo ejecutar ffprobe: {exc}"
    if r.returncode != 0:
        detalle_error = (r.stderr or "").strip().splitlines()
        return None, "ffprobe no pudo leer el archivo" + (f": {detalle_error[-1]}" if detalle_error else "")
    try:
        return json.loads(r.stdout or "{}"), None
    except ValueError:
        return None, "ffprobe devolvió datos que no se entienden"


def _entero(valor):
    try:
        return int(float(valor))
    except (TypeError, ValueError):
        return None


def _idioma_por_metadatos(pista: dict | None) -> str | None:
    """Como detect_audio_language (paso 1): códec, título y handler de ESA pista."""
    if not pista:
        return None
    etiquetas = pista.get("tags") or {}
    texto = ",".join(str(v) for v in (pista.get("codec_name"), etiquetas.get("title"),
                                       etiquetas.get("handler_name")) if v)
    return politica.idioma_por_metadatos(texto)


def _enumerar(textos: list) -> str:
    return textos[0] if len(textos) == 1 else ", ".join(textos[:-1]) + " y " + textos[-1]


def _texto_plan(audio: list, subtitulos: list, plan) -> str:
    conservadas = [a for a in audio if a["se_conserva"]]
    if not audio:
        partes = ["Sin pistas de audio"]
    elif conservadas:
        # El idioma que se escribirá (ISO 639-2); si no se conoce, no se fuerza ninguno.
        # Con variante regional o título, también el nombre que llevará la pista.
        idiomas = []
        for a in conservadas:
            idioma = plan.idiomas_audio.get(a["indice_audio"]) or "sin idioma"
            nombre = plan.nombres_audio.get(a["indice_audio"])
            idiomas.append(f"{idioma}, «{nombre}»" if nombre else idioma)
        numeros = [str(a["indice_absoluto"]) for a in conservadas]
        if len(conservadas) == 1:
            partes = [f"Se conserva la pista {numeros[0]} ({idiomas[0]})"]
        elif len(set(idiomas)) == 1:
            partes = [f"Se conservan las pistas {_enumerar(numeros)} ({idiomas[0]})"]
        else:
            partes = ["Se conservan las pistas "
                      + _enumerar([f"{n} ({i})" for n, i in zip(numeros, idiomas)])]
    else:
        partes = ["No se conserva ninguna pista de audio"]
    quitadas = len(audio) - len(conservadas)
    if quitadas:
        partes.append(f"se quita{'n' if quitadas > 1 else ''} {quitadas} "
                      f"pista{'s' if quitadas > 1 else ''} de audio")
    n = sum(1 for s in subtitulos if s["se_conserva"])
    if plan.omitir_subtitulos:
        subs = "ninguno (archivo grande o UHD)"
    elif n == 0:
        subs = "ninguno"
    elif plan.subtitulos_espanol:
        subs = f"{n} en español"
    else:
        subs = f"{n}, el primero de texto (no hay en español)"
    partes.append(f"subtítulos: {subs}")
    return "; ".join(partes)


def armar_detalle(archivo: dict, sondeo: dict, idiomas_bd: dict) -> dict:
    """Arma el detalle a partir de la fila de la BD, el ffprobe y los idiomas detectados."""
    streams = sondeo.get("streams") or []
    formato = sondeo.get("format") or {}
    videos = [s for s in streams if s.get("codec_type") == "video"]
    pistas_audio = [s for s in streams if s.get("codec_type") == "audio"]
    pistas_subs = [s for s in streams if s.get("codec_type") == "subtitle"]

    tamano = archivo.get("tamano")
    if tamano is None:
        tamano = _entero(formato.get("size")) or 0
    v = videos[0] if videos else {}
    bitrate = _entero(formato.get("bit_rate")) or _entero(v.get("bit_rate"))
    video = {
        "codec": v.get("codec_name"),
        "ancho": _entero(v.get("width")),
        "alto": _entero(v.get("height")),
        "bitrate_kbps": bitrate // 1000 if bitrate else None,
        "duracion": float(formato["duration"]) if _entero(formato.get("duration")) is not None else None,
        "tamano": tamano,
    }

    # Mismas entradas que detect_language_streams arma para el procesador.
    audio, audio_languages, audio_espanol, titulos_audio = [], {}, [], {}
    for relativo, pista in enumerate(pistas_audio):
        # Como titulos_audio del procesador: el título, si no el handler_name.
        etiquetas = {k.lower(): val for k, val in (pista.get("tags") or {}).items()}
        titulo = str(etiquetas.get("title") or etiquetas.get("handler_name") or "").strip()
        if titulo:
            titulos_audio[relativo] = titulo
        absoluto = int(pista.get("index", relativo))
        etiqueta = str((pista.get("tags") or {}).get("language") or "")
        detectado = idiomas_bd.get(absoluto)
        detectado = detectado if detectado and detectado != "und" else None
        idioma = etiqueta.lower()
        if not idioma or idioma == "und":
            # detect_audio_language: primero la BD por huella; si no, los metadatos
            # de la propia pista (por su índice absoluto).
            idioma = detectado or _idioma_por_metadatos(pista) or ""
        idioma = idioma or "und"
        audio_languages[relativo] = idioma
        if politica.es_espanol(idioma):
            audio_espanol.append(relativo)
        audio.append({"indice_absoluto": absoluto, "indice_audio": relativo,
                      "codec": pista.get("codec_name"), "canales": _entero(pista.get("channels")),
                      "etiqueta": etiqueta, "detectado": detectado, "idioma": idioma})

    subs_espanol, codecs_subs = [], []
    for relativo, pista in enumerate(pistas_subs):
        if politica.es_espanol(str((pista.get("tags") or {}).get("language") or "").lower()):
            subs_espanol.append(relativo)
        codecs_subs.append(pista.get("codec_name") or "")

    plan = politica.planificar_pistas(
        audio_languages, audio_espanol, subs_espanol,
        politica.es_anime(Path(archivo["ruta"])),
        politica.omitir_subtitulos(tamano, video["ancho"] or 0, video["alto"] or 0),
        titulos_audio=titulos_audio,
    )
    for a in audio:
        a["se_conserva"] = a["indice_audio"] in plan.audio
        a["titulo"] = titulos_audio.get(a["indice_audio"])
        a["nombre"] = plan.nombres_audio.get(a["indice_audio"]) if a["se_conserva"] else None
    elegidos = politica.elegir_subtitulos(plan.subtitulos_espanol, codecs_subs,
                                          omitir=plan.omitir_subtitulos)
    subtitulos = [
        {"indice": relativo, "indice_absoluto": int(p.get("index", relativo)),
         "codec": p.get("codec_name"), "etiqueta": str((p.get("tags") or {}).get("language") or ""),
         "se_conserva": relativo in elegidos}
        for relativo, p in enumerate(pistas_subs)
    ]
    return {"archivo": archivo, "video": video, "audio": audio, "subtitulos": subtitulos,
            "plan": _texto_plan(audio, subtitulos, plan)}


def huella_derivada(fp: str) -> str | None:
    """Huella del archivo que salió de reescribir `fp` (retag o recompresión), en solo
    lectura; None si no hay ninguno. Sirve para que un enlace viejo no dé 404."""
    with bd.conexion_solo_lectura() as conn:
        if conn is None:
            return None
        fila = conn.execute(
            "SELECT fingerprint FROM media_files WHERE derived_from = ? AND fingerprint <> ? "
            "ORDER BY updated_at DESC LIMIT 1", (fp, fp)).fetchone()
    return fila[0] if fila else None


def obtener_detalle(fp: str) -> dict | None:
    """Detalle del archivo con esa huella, o None si no está en la BD."""
    archivo, idiomas_bd = _leer_bd(fp)
    if archivo is None:
        return None
    ruta = Path(archivo["ruta"])
    try:
        archivo["tamano"] = ruta.stat().st_size
    except OSError:
        archivo["tamano"] = None
        return {"archivo": archivo, "video": {}, "audio": [], "subtitulos": [], "plan": "",
                "error": "El archivo no está en el disco; ffprobe no pudo leerlo."}
    sondeo, error = _sondear(ruta)
    if sondeo is None:
        return {"archivo": archivo, "video": {}, "audio": [], "subtitulos": [], "plan": "",
                "error": error}
    return armar_detalle(archivo, sondeo, idiomas_bd)
