import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import mediajelly_db  # noqa: E402


@pytest.fixture(autouse=True)
def _base_de_datos_aislada(tmp_path_factory, monkeypatch):
    """Ningún test usa la BD real de scripts/tmp: cada uno trabaja sobre una copia vacía.

    Los tests que necesitan su propia ruta la siguen redirigiendo con monkeypatch,
    que se aplica después de este fixture.
    """
    db_path = tmp_path_factory.mktemp("db") / "mediajelly.db"
    monkeypatch.setattr(mediajelly_db, "_get_db_path", lambda: db_path)


@pytest.fixture(autouse=True)
def _progreso_aislado(tmp_path_factory, monkeypatch):
    """Ningún test escribe el avance en scripts/tmp/progreso: el panel lo leería como real."""
    try:
        import mediajelly_progreso
    except ImportError:  # versiones anteriores al módulo de progreso
        return
    monkeypatch.setattr(mediajelly_progreso, "DIR_PROGRESO", tmp_path_factory.mktemp("progreso"))
