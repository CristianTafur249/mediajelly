"""Avisos por huella (audio desfasado, subtítulo a revisar) en solo lectura."""

import logging
import sqlite3
from pathlib import Path
from typing import Dict, List

from panel.servicios import bd
from panel.servicios.cola import hora_local

registro = logging.getLogger(__name__)

ETIQUETAS = {"audio_desfasado": "Audio desfasado", "revisar_subtitulo": "Subtítulo a revisar"}

_CONSULTA = (
    "SELECT a.fingerprint, a.tipo, COALESCE(a.detalle, ''), a.creado, m.path "
    "FROM avisos a LEFT JOIN media_files m ON m.fingerprint = a.fingerprint"
)


def _filas(sufijo: str = "", parametros: tuple = ()) -> List[dict]:
    with bd.conexion_solo_lectura() as conn:
        if conn is None:
            return []
        try:
            filas = conn.execute(f"{_CONSULTA} {sufijo} ORDER BY a.creado DESC", parametros).fetchall()
        except sqlite3.OperationalError as exc:  # BD anterior a la tabla avisos
            registro.info("Sin tabla de avisos: %s", exc)
            return []
    return [
        {
            "fp": fp,
            "tipo": tipo,
            "etiqueta": ETIQUETAS.get(tipo, tipo),
            "detalle": detalle,
            "creado": hora_local(creado) if creado else creado,
            "nombre": Path(ruta).name if ruta else fp,
        }
        for fp, tipo, detalle, creado, ruta in filas
    ]


def listar() -> Dict[str, List[dict]]:
    """Avisos agrupados por tipo, en el orden de ETIQUETAS."""
    grupos: Dict[str, List[dict]] = {}
    for aviso in _filas():
        grupos.setdefault(aviso["tipo"], []).append(aviso)
    return {
        tipo: grupos[tipo] for tipo in sorted(grupos, key=lambda t: list(ETIQUETAS).index(t) if t in ETIQUETAS else 99)
    }


def de_huella(fp: str) -> List[dict]:
    """Avisos de un archivo, para las insignias del detalle."""
    return _filas("WHERE a.fingerprint = ?", (fp,))
