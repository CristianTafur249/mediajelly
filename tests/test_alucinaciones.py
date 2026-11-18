import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from mediajelly_alucinaciones import es_alucinacion, filtrar_cues, filtrar_srt, limpiar_texto  # noqa: E402
from mediajelly_whisper_extractor import WhisperAudioExtractorMixin  # noqa: E402


class _Logger:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _cues(*textos):
    return [{"start": float(i), "end": float(i) + 1, "text": t} for i, t in enumerate(textos)]


def test_bucle_de_whisper_queda_en_un_cue():
    cues, cambios = filtrar_cues(_cues(*["Y ahora, vamos a preparar el pan."] * 42, "Otra frase"))

    assert [c["text"] for c in cues] == ["Y ahora, vamos a preparar el pan.", "Otra frase"]
    assert cambios == 41


def test_dos_repeticiones_seguidas_no_se_tocan():
    cues, cambios = filtrar_cues(_cues("No, no.", "No, no.", "Vamos."))

    assert len(cues) == 3 and cambios == 0


def test_creditos_inventados_y_cues_sin_letras_se_quitan():
    cues, _ = filtrar_cues(_cues("Hola", "Subtítulos por la comunidad de Amara.Org", "…", "。", "ご視聴ありがとうございました"))

    assert [c["text"] for c in cues] == ["Hola"]


def test_cue_solo_con_numeros_se_conserva():
    assert not es_alucinacion("103.")
    assert es_alucinacion("<i>…</i>")


def test_repeticiones_de_caracteres_y_palabras_se_reducen():
    assert limpiar_texto("¿A? Ahhhhhhhh.. Por eso estoy aquí.") == "¿A? Ahhh.. Por eso estoy aquí."
    assert limpiar_texto("척" * 40) == "척척척"
    assert limpiar_texto("No, no, no, no, no, no, no, no vine") == "No, no, no vine"
    # Seis repeticiones pueden ser diálogo real
    assert limpiar_texto("No, no, no, no, no, no.") == "No, no, no, no, no, no."


def test_filtrar_srt_renumera_y_conserva_tiempos():
    srt = (
        "1\n00:00:01,000 --> 00:00:02,000\nHola\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\n…\n\n"
        "3\n00:00:05,000 --> 00:00:06,000\nAdiós\n"
    )

    limpio, cambios = filtrar_srt(srt)

    assert cambios == 1
    assert limpio == "1\n00:00:01,000 --> 00:00:02,000\nHola\n\n2\n00:00:05,000 --> 00:00:06,000\nAdiós\n"


def test_filtrar_srt_sin_alucinaciones_devuelve_lo_mismo():
    srt = "1\n00:00:01,000 --> 00:00:02,000\nHola\n"

    assert filtrar_srt(srt) == (srt, 0)


def test_srt_de_whisper_sale_sin_alucinaciones(tmp_path):
    class _Extractor(WhisperAudioExtractorMixin):
        logger = _Logger()
        tmp_dir = tmp_path

        def _improve_extracted_subtitles_quality(self, srt_file, language):
            pass

    out = tmp_path / "Video.srt"
    segmentos = _cues("Hola", *["Bucle"] * 5, "…")

    result = _Extractor()._generate_srt_from_transcription({"segments": segmentos, "language": "es"}, out)

    texto = result.read_text(encoding="utf-8")
    assert texto.count("Bucle") == 1 and "…" not in texto


def test_reparacion_incluye_srt_con_idioma(tmp_path):
    from repair_whisper_hallucinations import subtitulos_whisper

    (tmp_path / "Show.mkv").touch()
    for nombre in ("Show.ja.srt", "Show.es.srt", "Show.srt", "Show.temp.srt", "Otro.ja.srt"):
        (tmp_path / nombre).write_text("1\n00:00:00,000 --> 00:00:01,000\nhola\n", encoding="utf-8")

    nombres = [p.name for p in subtitulos_whisper(tmp_path, {"Show"})]
    assert nombres == ["Show.es.srt", "Show.ja.srt", "Show.srt"]
