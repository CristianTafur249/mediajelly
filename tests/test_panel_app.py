import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from panel.app import crear_app  # noqa: E402

MENSAJE_503 = "Configura PANEL_PASSWORD en .env para usar el panel."


@pytest.fixture
def cliente():
    return TestClient(crear_app(password="clave-buena", secret="secreto-de-prueba"),
                      follow_redirects=False)


def test_sin_password_503(monkeypatch):
    monkeypatch.delenv("PANEL_PASSWORD", raising=False)
    c = TestClient(crear_app(password=None), follow_redirects=False)
    respuestas = [
        c.get("/"),
        c.get("/login"),
        c.post("/login", data={"password": "x"}),
        c.get("/estaticos/panel.css"),
    ]
    for r in respuestas:
        assert r.status_code == 503
        assert r.text == MENSAJE_503


def test_redirige_a_login(cliente):
    r = cliente.get("/")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_password_incorrecta(cliente):
    r = cliente.post("/login", data={"password": "mala"})
    assert r.status_code == 200
    assert "Contraseña incorrecta" in r.text
    assert cliente.get("/").status_code == 303


def test_login_correcto_y_cookie(cliente):
    r = cliente.post("/login", data={"password": "clave-buena"})
    assert r.status_code == 303
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    r = cliente.get("/")
    assert r.status_code == 200
    # Exclusivo de la página autenticada (el login no tiene menú ni este título).
    assert "<h1>Inicio</h1>" in r.text
    assert 'class="lateral"' in r.text


def test_salir_cierra_sesion(cliente):
    cliente.post("/login", data={"password": "clave-buena"})
    assert cliente.post("/salir").status_code == 303
    assert cliente.get("/").status_code == 303


def test_base_responsive(cliente):
    cliente.post("/login", data={"password": "clave-buena"})
    html = cliente.get("/").text
    assert 'name="viewport"' in html
    assert 'class="lateral"' in html
    assert 'class="inferior"' in html


def test_estaticos_servidos(cliente):
    for nombre in ("panel.css", "htmx.min.js", "sse.js"):
        assert cliente.get(f"/estaticos/{nombre}").status_code == 200


def test_barra_inferior_tiene_mas_y_salir(cliente):
    cliente.post("/login", data={"password": "clave-buena"})
    html = cliente.get("/").text
    inferior = html[html.index('class="inferior"'):]
    inferior = inferior[:inferior.index("</nav>")]
    assert "Más" in inferior
    assert 'href="/mas"' in inferior
    r = cliente.get("/mas")
    assert r.status_code == 200
    principal = r.text[r.text.index("<main>"):r.text.index("</main>")]
    assert 'action="/salir"' in principal
    for servicio in ("Jellyfin", "Sonarr", "Radarr", "Seerr"):
        assert servicio in principal


def test_mas_requiere_sesion(cliente):
    assert cliente.get("/mas").status_code == 303


def test_estaticos_no_sale_de_su_carpeta(cliente):
    for ruta in ("/estaticos/../app.py", "/estaticos/%2e%2e/app.py", "/estaticos/..%2fapp.py"):
        r = cliente.get(ruta)
        assert r.status_code in (400, 404)
        assert "crear_app" not in r.text


def _bd_v2_sin_prioridad(ruta_video):
    """BD como la dejaba la versión anterior: schema v2, sin prioridad ni panel_tareas."""
    import sqlite3

    import mediajelly_db

    mediajelly_db.init_db()
    with sqlite3.connect(mediajelly_db._get_db_path()) as conn:
        conn.execute("ALTER TABLE media_files DROP COLUMN prioridad")
        conn.execute("DROP TABLE panel_tareas")
        conn.execute("UPDATE schema_version SET version = 2")
        conn.execute(
            "INSERT INTO media_files (fingerprint, path, status, attempts, created_at, updated_at) "
            "VALUES ('v2', ?, 'pending', 0, '2026-01-01', '2026-01-01')", (str(ruta_video),))
    with sqlite3.connect(mediajelly_db._get_db_path()) as conn:
        assert "prioridad" not in {r[1] for r in conn.execute("PRAGMA table_info(media_files)")}


def test_arranque_migra_bd_vieja(tmp_path):
    import sqlite3

    import mediajelly_db

    _bd_v2_sin_prioridad(tmp_path / "Viejo.mkv")
    with TestClient(crear_app(password="clave", secret="s"), follow_redirects=False) as c:
        assert c.post("/login", data={"password": "clave"}).status_code == 303
        r = c.get("/video/v2")
        assert r.status_code == 200 and "Viejo.mkv" in r.text
        r = c.get("/cola")
        assert r.status_code == 200 and "Viejo.mkv" in r.text
    with sqlite3.connect(mediajelly_db._get_db_path()) as conn:
        assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 3


def test_arranque_sigue_si_init_db_falla(monkeypatch, caplog):
    import mediajelly_db

    def falla():
        raise RuntimeError("disco de solo lectura")

    monkeypatch.setattr(mediajelly_db, "init_db", falla)
    with caplog.at_level("ERROR"):
        with TestClient(crear_app(password="clave", secret="s"), follow_redirects=False) as c:
            assert c.get("/login").status_code == 200
    assert "disco de solo lectura" in caplog.text
