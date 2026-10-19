"""Traducción de subtítulos: nombres de personajes y separador de líneas.

El transcriptor ya recibe los personajes del .nfo; el traductor los mandaba a
Yandex como texto normal ("Trunks" -> "Baúles", "Mr. Satan" -> "Sr. Satanás").
Ahora se cambian por marcadores [[Nk]] que Yandex respeta y se restauran.

Además, Yandex rompía el separador " ||| " ("| | |", "/ / /") y dos líneas de
un mismo subtítulo quedaban pegadas en una sola.
"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from mediajelly_subtitle_translator import SubtitleTranslator  # noqa: E402

# Lo que haría Yandex con los nombres si los viera como palabras
_LITERALES = {"Trunks": "Baúles", "Piccolo": "Flautín", "Mr. Satan": "Sr. Satanás"}


class _Logger:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class _Resultado:
    def __init__(self, result):
        self.result = result


class _YandexFalso:
    """Traduce "traduciendo" nombres como palabras y respetando [[...]]."""

    def __init__(self, rompe_separador=False):
        self.rompe_separador = rompe_separador
        self.textos = []

    def translate(self, text, lang):
        self.textos.append(text)
        salida = text.replace("I saw", "Vi").replace("yesterday", "ayer")
        for nombre, literal in _LITERALES.items():
            salida = salida.replace(nombre, literal)
        salida = salida.replace("Where is", "¿Dónde está").replace("is fighting", "está peleando")
        if self.rompe_separador:
            salida = re.sub(r"\s*\[\[S\]\]\s*", " / / / ", salida)
        return _Resultado(salida)


@pytest.fixture
def traductor(tmp_path):
    instancia = SubtitleTranslator.__new__(SubtitleTranslator)
    instancia.logger = _Logger()
    instancia.tmp_dir = tmp_path
    instancia.allow_retranslate = False
    instancia._probe_cache = {}
    instancia.current_stats = None
    return instancia


def _serie(tmp_path, roles):
    carpeta = tmp_path / "anime" / "Dragon Ball" / "Season 1"
    carpeta.mkdir(parents=True)
    actores = "".join(f"<actor><name>X</name><role>{r}</role></actor>" for r in roles)
    (carpeta.parent / "tvshow.nfo").write_text(
        f"<tvshow><title>Dragon Ball</title>{actores}</tvshow>", encoding="utf-8")
    return carpeta / "Dragon Ball S01E01.en.srt"


def test_glosario_sale_del_nfo_de_la_serie(traductor, tmp_path):
    srt = _serie(tmp_path, ["Trunks", "Piccolo (voice)", "Mr. Satan", "Son Goku", "Elder Kai"])

    glosario = traductor._glosario_nombres(srt)

    for nombre in ["Trunks", "Piccolo", "Mr. Satan", "Son Goku", "Goku", "Elder Kai"]:
        assert nombre in glosario
    # Partes que son palabras comunes no se protegen solas
    assert "Son" not in glosario and "Elder" not in glosario and "Mr." not in glosario
    # Primero los más largos, para que "Son Goku" gane sobre "Goku"
    assert glosario.index("Son Goku") < glosario.index("Goku")


def test_sin_nfo_no_hay_glosario(traductor, tmp_path):
    assert traductor._glosario_nombres(tmp_path / "peli.en.srt") == []


def test_los_nombres_no_se_traducen(traductor, tmp_path):
    traductor._glosario_actual = ["Mr. Satan", "Piccolo", "Trunks"]
    lineas = ["1\n", "00:00:01,000 --> 00:00:02,000\n", "Trunks is fighting Mr. Satan.\n",
              "Where is Piccolo?\n", "\n"]

    traducidas, hechas = traductor._translate_lines_batch(lineas, _YandexFalso())

    assert traducidas[2] == "Trunks está peleando Mr. Satan.\n"
    assert traducidas[3] == "¿Dónde está Piccolo?\n"
    assert hechas == 2


def test_texto_japones_no_lleva_marcadores(traductor):
    # Con kana/kanji Yandex pierde el contexto del marcador ("Beerus Tu qi")
    traductor._glosario_actual = ["Beerus"]

    assert traductor._proteger_nombres("Beerus様と戦う") == "Beerus様と戦う"


def test_marcador_con_espacios_se_restaura(traductor):
    traductor._glosario_actual = ["Beerus"]

    assert traductor._restaurar_nombres("Lord [[ N0 ]] pelea") == "Lord Beerus pelea"


@pytest.mark.parametrize("traducido, esperado", [
    ("y el hijo Gokusan está lejos", "y el hijo Goku-san está lejos"),
    ("¡oh, son Goku!", "¡oh, Son Goku!"),
    ("Conocer a Beerus-Sama.", "Conocer a Beerus-sama."),
    ("Beerussama llega", "Beerus-sama llega"),
    ("el satan de la historia", "el satan de la historia"),  # una palabra: no toca mayúsculas
    ("Kaio-sama y Kaisan", "Kaio-sama y Kai-san"),
])
def test_normaliza_nombres_tras_traducir(traductor, traducido, esperado):
    traductor._glosario_actual = ["Son Goku", "Beerus", "Satan", "Goku", "Kaio", "Kai"]

    assert traductor._normalizar_nombres(traducido) == esperado


def test_no_protege_dentro_de_otras_palabras(traductor):
    traductor._glosario_actual = ["Kai"]

    assert traductor._proteger_nombres("Kaio and Kai") == "Kaio and [[N0]]"


def test_separador_roto_no_pega_lineas(traductor):
    lineas = ["1\n", "00:00:01,000 --> 00:00:02,000\n", "- Still nothing?\n", "- It is not a signal.\n", "\n"]
    yandex = _YandexFalso(rompe_separador=True)

    traducidas, _ = traductor._translate_lines_batch(lineas, yandex)

    # Dos líneas siguen siendo dos líneas (segundo intento, con saltos de línea)
    assert traducidas[2:4] == ["- Still nothing?\n", "- It is not a signal.\n"]
    assert not any("/ / /" in l for l in traducidas)
    assert yandex.textos[1] == "- Still nothing?\n- It is not a signal."


def test_si_tampoco_cuadra_traduce_linea_por_linea(traductor):
    class _UneTodo(_YandexFalso):
        def translate(self, text, lang):
            self.textos.append(text)
            return _Resultado(re.sub(r"\s*(\[\[S\]\]|\n)\s*", " ", text))

    lineas = ["1\n", "00:00:01,000 --> 00:00:02,000\n", "uno\n", "dos\n", "\n"]
    yandex = _UneTodo()

    traducidas, _ = traductor._translate_lines_batch(lineas, yandex)

    assert traducidas[2:4] == ["uno\n", "dos\n"]
    assert yandex.textos[2:] == ["uno", "dos"]


def test_separador_nuevo_conserva_el_contexto(traductor):
    lineas = ["1\n", "00:00:01,000 --> 00:00:02,000\n", "Soldier, I ordered you to\n",
              "move those catapults.\n", "\n"]
    yandex = _YandexFalso()

    traductor._translate_lines_batch(lineas, yandex)

    # Las dos líneas del subtítulo van en una sola petición
    assert yandex.textos == ["Soldier, I ordered you to [[S]] move those catapults."]


def test_solo_se_protegen_los_nombres_que_el_traductor_cambia(traductor):
    glosario = ["Mr. Satan", "Piccolo", "Trunks", "Goku"]

    cambian = traductor._nombres_que_cambian(_YandexFalso(), glosario)

    # El falso traduce Trunks, Piccolo y Mr. Satan; Goku lo deja igual
    assert cambian == {"Mr. Satan", "Piccolo", "Trunks"}


def test_si_la_prueba_falla_se_protegen_todos(traductor):
    class _Caido:
        def translate(self, text, lang):
            raise ConnectionError("sin red")

    assert traductor._nombres_que_cambian(_Caido(), ["Goku", "Trunks"]) == {"Goku", "Trunks"}


def test_nombre_que_no_cambia_no_lleva_marcador(traductor):
    traductor._glosario_actual = ["Trunks", "Goku"]
    traductor._nombres_protegidos = {"Trunks"}

    assert traductor._proteger_nombres("Goku and Trunks") == "Goku and [[N0]]"
