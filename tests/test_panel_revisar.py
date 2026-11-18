"""Vista «A revisar» del panel e insignias de avisos en el detalle."""
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import mediajelly_db  # noqa: E402
from panel.app import crear_app  # noqa: E402
from panel.servicios import detalle  # noqa: E402


@pytest.fixture
def cliente():
    c = TestClient(crear_app(password="clave", secret="s"), follow_redirects=False)
    assert c.post("/login", data={"password": "clave"}).status_code == 303
    return c


def _registrado(tmp_path):
    video = tmp_path / "Avatar S01E01.mp4"
    video.write_bytes(os.urandom(64 * 1024))
    viejo = time.time() - 600
    os.utime(video, (viejo, viejo))
    rec, _ = mediajelly_db.register(video)
    return rec.fingerprint


def test_revisar_lista_avisos(cliente, tmp_path):
    fp = _registrado(tmp_path)
    mediajelly_db.marcar_aviso(fp, "audio_desfasado", "audio -52 s respecto al video")

    r = cliente.get("/revisar")

    assert r.status_code == 200
    assert "Audio desfasado" in r.text
    assert "Avatar S01E01.mp4" in r.text
    assert "audio -52 s" in r.text


def test_revisar_sin_tabla(cliente):
    ruta = mediajelly_db._get_db_path()
    conn = sqlite3.connect(str(ruta))
    conn.execute("CREATE TABLE media_files (fingerprint TEXT PRIMARY KEY, path TEXT)")
    conn.close()

    r = cliente.get("/revisar")

    assert r.status_code == 200
    assert "Nada por revisar" in r.text


def test_video_muestra_insignia(cliente, tmp_path, monkeypatch):
    fp = _registrado(tmp_path)
    mediajelly_db.marcar_aviso(fp, "revisar_subtitulo", "cobertura 0.52 < 0.75")
    monkeypatch.setattr(detalle, "_sondear", lambda ruta: ({"streams": [], "format": {}}, None))

    r = cliente.get(f"/video/{fp}")

    assert r.status_code == 200
    assert "Subtítulo a revisar" in r.text


def test_revisar_requiere_sesion():
    c = TestClient(crear_app(password="clave", secret="s"), follow_redirects=False)
    r = c.get("/revisar")
    assert r.status_code in (303, 307)
    assert "/login" in r.headers["location"]
