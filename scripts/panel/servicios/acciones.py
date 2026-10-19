"""Acciones del panel sobre la cola: escriben solo por la API de mediajelly_db."""
import logging
from dataclasses import dataclass, field

import mediajelly_db

registro = logging.getLogger(__name__)

PRIORIDAD_PRIMERO = 10


@dataclass
class Resultado:
    ok: int = 0
    cambiados: list[str] = field(default_factory=list)
    no_encontrados: list[str] = field(default_factory=list)
    en_proceso: list[str] = field(default_factory=list)


def _aplicar(fps: list[str], cambiar) -> Resultado:
    """Aplica `cambiar(registro)` a cada huella; las que ya no existen no lanzan."""
    resultado = Resultado()
    for fp in dict.fromkeys(fps):  # sin repetidas, en el mismo orden
        reg = mediajelly_db.get_by_fingerprint(fp)
        if reg is None:
            resultado.no_encontrados.append(fp)
            continue
        if reg.status == "processing":  # el cron lo está comprimiendo: no se toca
            resultado.en_proceso.append(fp)
            continue
        cambiar(reg)
        resultado.cambiados.append(fp)
    resultado.ok = len(resultado.cambiados)
    return resultado


def _volver_a_pendiente(reg) -> None:
    mediajelly_db.reiniciar_fallos(reg.fingerprint)
    mediajelly_db.set_status(reg.fingerprint, "pending")


def reprocesar(fps: list[str]) -> Resultado:
    return _aplicar(fps, _volver_a_pendiente)


def sacar_de_cuarentena(fps: list[str]) -> Resultado:
    return _aplicar(fps, _volver_a_pendiente)


def omitir(fps: list[str]) -> Resultado:
    return _aplicar(fps, lambda reg: mediajelly_db.set_status(reg.fingerprint, "skipped"))


def procesar_primero(fps: list[str]) -> Resultado:
    def subir(reg):
        mediajelly_db.set_prioridad(reg.fingerprint, PRIORIDAD_PRIMERO)
        if reg.status == "failed":  # como al sacar de cuarentena: si no, vuelve a ella al primer fallo
            mediajelly_db.reiniciar_fallos(reg.fingerprint)
        if reg.status != "pending":
            mediajelly_db.set_status(reg.fingerprint, "pending")
    return _aplicar(fps, subir)


ACCIONES = {
    "reprocesar": reprocesar,
    "procesar-primero": procesar_primero,
    "sacar-de-cuarentena": sacar_de_cuarentena,
    "omitir": omitir,
}


def lanzar_ciclo() -> tuple[int, bool]:
    """Encola un ciclo; (id, creada). Si ya hay uno en cola devuelve el existente."""
    return mediajelly_db.crear_tarea("ciclo", {})


def detectar_idioma(fp: str) -> bool | None:
    """Encola la detección de idiomas del archivo; None si la huella ya no existe.

    Devuelve si la tarea se creó (False: ya había una igual en cola)."""
    if mediajelly_db.get_by_fingerprint(fp) is None:
        return None
    return mediajelly_db.crear_tarea("detectar", {"fp": fp})[1]


def corregir_idioma(fp: str, pista: int, idioma: str) -> bool | None:
    """Encola el re-etiquetado de una pista (índice absoluto); None si la huella ya no existe."""
    if mediajelly_db.get_by_fingerprint(fp) is None:
        return None
    return mediajelly_db.crear_tarea("retag", {"fp": fp, "pista": int(pista), "idioma": idioma})[1]
