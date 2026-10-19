import os
import shutil
import subprocess
import time

import pytest

import mediajelly_progreso as progreso


@pytest.fixture(autouse=True)
def _dir_progreso(tmp_path, monkeypatch):
    destino = tmp_path / "progreso"
    monkeypatch.setattr(progreso, "DIR_PROGRESO", destino)
    return destino


def test_iniciar_y_leer():
    progreso.iniciar("compresion", "a.mp4", 100)
    progreso.actualizar("compresion", 25)
    etapas = progreso.leer()["etapas"]
    assert len(etapas) == 1
    assert etapas[0].etapa == "compresion"
    assert etapas[0].archivo == "a.mp4"
    assert etapas[0].porcentaje == 25.0
    assert etapas[0].sin_senal is False


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="falta ffmpeg")
def test_ffmpeg_real_reporta_avance(tmp_path):
    progreso.iniciar("compresion", "salida.mp4", duracion_s=5)
    cmd = (
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=5:size=160x120:rate=10",
         "-c:v", "libx264"]
        + progreso.ffmpeg_args("compresion")
        + [str(tmp_path / "out.mp4")]
    )
    subprocess.run(cmd, check=True, capture_output=True)
    avance = progreso.leer()["etapas"][0]
    assert avance.porcentaje >= 99


def test_ffmpeg_ultimo_bloque_completo(_dir_progreso):
    progreso.iniciar("compresion", "a.mp4", 100)
    (_dir_progreso / "compresion.ffmpeg").write_text(
        "out_time_us=10000000\nspeed=2.0x\nprogress=continue\n"
        "out_time_us=20000000\nspeed=4.0x\nprogress=continue\n"
        "out_time_us=3000"
    )
    avance = progreso.leer()["etapas"][0]
    assert avance.posicion_s == 20.0
    assert avance.velocidad == 4.0
    assert avance.restante_s == 20.0


def test_ffmpeg_speed_na(_dir_progreso):
    progreso.iniciar("compresion", "a.mp4", 100)
    (_dir_progreso / "compresion.ffmpeg").write_text(
        "out_time_ms=5000000\nspeed=N/A\nprogress=continue\n"
    )
    avance = progreso.leer()["etapas"][0]
    assert avance.posicion_s == 5.0
    assert avance.velocidad is None
    assert avance.restante_s is None


def test_sin_senal_tras_60s(_dir_progreso):
    progreso.iniciar("compresion", "a.mp4", 100)
    ruta = _dir_progreso / "compresion.json"
    viejo = time.time() - 61
    os.utime(ruta, (viejo, viejo))
    assert progreso.leer()["etapas"][0].sin_senal is True


def test_json_corrupto_no_falla(_dir_progreso):
    _dir_progreso.mkdir(parents=True)
    (_dir_progreso / "compresion.json").write_text("{no json")
    assert progreso.leer()["etapas"] == []


def test_terminar_borra(_dir_progreso):
    progreso.iniciar("compresion", "a.mp4", 100)
    (_dir_progreso / "compresion.ffmpeg").write_text("progress=end\n")
    progreso.terminar("compresion")
    assert not (_dir_progreso / "compresion.json").exists()
    assert not (_dir_progreso / "compresion.ffmpeg").exists()
    assert progreso.leer()["etapas"] == []


def test_iniciar_borra_ffmpeg_viejo(_dir_progreso):
    _dir_progreso.mkdir(parents=True)
    (_dir_progreso / "compresion.ffmpeg").write_text("progress=end\n")
    progreso.iniciar("compresion", "a.mp4", 100)
    assert not (_dir_progreso / "compresion.ffmpeg").exists()


def test_actualizar_sin_json_no_hace_nada(_dir_progreso):
    progreso.actualizar("compresion", 10)
    assert not (_dir_progreso / "compresion.json").exists()


def test_fase_y_fin():
    progreso.fase("escaneo")
    assert progreso.leer()["ciclo"]["fase"] == "escaneo"
    progreso.fase(None)
    ciclo = progreso.leer()["ciclo"]
    assert ciclo["fase"] is None
    assert "fin" in ciclo


def test_leer_sin_directorio():
    assert progreso.leer() == {"ciclo": None, "etapas": []}


def test_ffmpeg_args(_dir_progreso):
    assert progreso.ffmpeg_args("x") == ["-progress", str(_dir_progreso / "x.ffmpeg"), "-nostats"]


def _trabajador_concurrente(dir_progreso: str, indice: int) -> None:
    import mediajelly_progreso as p
    from pathlib import Path
    p.DIR_PROGRESO = Path(dir_progreso)
    for i in range(300):
        if i % 50 == 0:
            p.iniciar("compresion", f"a{indice}.mp4", 100)
        else:
            p.actualizar("compresion", i % 100, detalle=f"p{indice}")


def test_escrituras_concurrentes_no_lanzan(_dir_progreso):
    import json
    import multiprocessing
    ctx = multiprocessing.get_context("spawn")
    procesos = [ctx.Process(target=_trabajador_concurrente, args=(str(_dir_progreso), n))
                for n in range(4)]
    for proceso in procesos:
        proceso.start()
    for proceso in procesos:
        proceso.join(120)
    assert [proceso.exitcode for proceso in procesos] == [0, 0, 0, 0]
    datos = json.loads((_dir_progreso / "compresion.json").read_text(encoding="utf-8"))
    assert datos["etapa"] == "compresion"
    assert [e.etapa for e in progreso.leer()["etapas"]] == ["compresion"]


def test_actualizar_no_revive_etapa_terminada(_dir_progreso, monkeypatch):
    import json
    import threading
    progreso.iniciar("compresion", "a.mp4", 100)
    real = json.loads
    hilos = []

    def con_terminar_en_medio(*args, **kwargs):
        resultado = real(*args, **kwargs)
        if not hilos:
            hilo = threading.Thread(target=progreso.terminar, args=("compresion",))
            hilos.append(hilo)
            hilo.start()
            time.sleep(0.3)
        return resultado

    monkeypatch.setattr(json, "loads", con_terminar_en_medio)
    progreso.actualizar("compresion", 50)
    hilos[0].join(10)
    assert not (_dir_progreso / "compresion.json").exists()
    assert progreso.leer()["etapas"] == []


def test_leer_ignora_lock_y_tmp(_dir_progreso):
    progreso.iniciar("compresion", "a.mp4", 100)
    (_dir_progreso / ".compresion.lock").write_text("")
    (_dir_progreso / ".compresion.abc.tmp").write_text("{no json")
    assert [e.etapa for e in progreso.leer()["etapas"]] == ["compresion"]


def test_no_lanza_si_no_se_puede_escribir(tmp_path, monkeypatch):
    archivo = tmp_path / "es_un_archivo"
    archivo.write_text("")
    monkeypatch.setattr(progreso, "DIR_PROGRESO", archivo / "sub")
    progreso.iniciar("compresion", "a.mp4", 100)
    progreso.actualizar("compresion", 1)
    progreso.terminar("compresion")
    progreso.fase("escaneo")
    progreso.fase(None)


def test_progress_end_lleva_al_100(_dir_progreso):
    progreso.iniciar("compresion", "a.mp4", 5)
    (_dir_progreso / "compresion.ffmpeg").write_text(
        "out_time_us=4800000\nspeed=2.0x\nprogress=end\n"
    )
    assert progreso.leer()["etapas"][0].porcentaje == 100


@pytest.mark.parametrize("valor", ["nan", "inf", "-inf"])
def test_valores_no_finitos_son_none(_dir_progreso, valor):
    progreso.iniciar("compresion", "a.mp4", 100)
    (_dir_progreso / "compresion.ffmpeg").write_text(
        f"out_time_us={valor}\nspeed={valor}x\nprogress=continue\n"
    )
    avance = progreso.leer()["etapas"][0]
    assert avance.velocidad is None
    assert avance.posicion_s == 0
    assert avance.porcentaje == 0


def _bloque_ffmpeg(i):
    return (f"frame={i}\nfps=24.00\nbitrate=1200.0kbits/s\ntotal_size={i * 1000}\n"
            f"out_time_us={i * 1_000_000}\nout_time_ms={i * 1_000_000}\nout_time=00:00:{i % 60:02d}.000000\n"
            f"dup_frames=0\ndrop_frames=0\nspeed={1 + i % 7}.5x\nprogress=continue\n")


def test_ffmpeg_grande_se_lee_solo_el_final(_dir_progreso, monkeypatch):
    progreso.iniciar("compresion", "a.mp4", 20_000)
    ruta = _dir_progreso / "compresion.ffmpeg"
    ruta.write_text("".join(_bloque_ffmpeg(i) for i in range(1, 15_001)))
    assert ruta.stat().st_size > 2 * 1024 * 1024
    leidos = [0]
    import builtins
    real = builtins.open

    class Contador:
        def __init__(self, fh):
            self._fh = fh

        def read(self, n=-1):
            datos = self._fh.read(n)
            leidos[0] += len(datos)
            return datos

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._fh.close()

        def __getattr__(self, nombre):
            return getattr(self._fh, nombre)

    def contando(*a, **k):
        fh = real(*a, **k)
        return Contador(fh) if str(a[0]) == str(ruta) else fh

    monkeypatch.setattr(builtins, "open", contando)
    leer_texto = type(ruta).read_text

    def sin_leer_ffmpeg(self, *a, **k):
        if self == ruta:
            pytest.fail("leyó el .ffmpeg completo")
        return leer_texto(self, *a, **k)

    monkeypatch.setattr(type(ruta), "read_text", sin_leer_ffmpeg)
    avance = progreso.leer()["etapas"][0]
    assert avance.posicion_s == 15_000.0
    assert avance.velocidad == 1 + 15_000 % 7 + 0.5
    assert 0 < leidos[0] <= 8 * 1024


def test_ffmpeg_final_cortado_coincide_con_lectura_completa(_dir_progreso):
    """Sea donde sea que caiga el corte de 8 KiB, el resultado es el de leer todo el archivo."""
    progreso.iniciar("compresion", "a.mp4", 20_000)
    ruta = _dir_progreso / "compresion.ffmpeg"
    base = "".join(_bloque_ffmpeg(i) for i in range(1, 200))
    for extra in range(0, 300, 7):
        texto = "x" * extra + base + "out_time_us=99"  # el último bloque, a medio escribir
        ruta.write_text(texto)
        esperado = progreso._ultimo_bloque_ffmpeg(texto)
        avance = progreso.leer()["etapas"][0]
        assert avance.posicion_s == int(esperado["out_time_us"]) / 1_000_000
        assert avance.velocidad == float(esperado["speed"].rstrip("x"))


def test_ffmpeg_pequeno_sigue_igual(_dir_progreso):
    progreso.iniciar("compresion", "a.mp4", 100)
    (_dir_progreso / "compresion.ffmpeg").write_text("out_time_us=5000000\nspeed=1.0x\nprogress=continue\n")
    assert progreso.leer()["etapas"][0].posicion_s == 5.0
