import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from mediajelly_scanner import MediaScanner  # noqa: E402
from mediajelly_subtitle_translator import (  # noqa: E402
    SubtitleQualityImprover,
    SubtitleTranslator,
    TranslationStalledError,
    _dividir_texto,
    _es_dibujo_ass,
    _limpiar_srt,
    _strip_font_tags,
)
from mediajelly_whisper_extractor import (  # noqa: E402
    MAX_CUE_SECONDS,
    MIN_CUE_SECONDS,
    WhisperAudioExtractorMixin,
)


class _Logger:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


@pytest.fixture
def translator(tmp_path):
    # Sin __init__: evita cargar configuración, logging y traductores externos
    instance = SubtitleTranslator.__new__(SubtitleTranslator)
    instance.logger = _Logger()
    instance.tmp_dir = tmp_path
    instance.allow_retranslate = False
    instance._probe_cache = {}
    return instance


def _stream(codec="subrip", lang=None, title=None, forced=0):
    tags = {}
    if lang:
        tags["language"] = lang
    if title:
        tags["title"] = title
    return {"codec_type": "subtitle", "codec_name": codec, "tags": tags, "disposition": {"forced": forced}}


def _write_srt(path, cues, spacing=4.0, text="Hello there"):
    def ts(seconds):
        return f"{int(seconds // 3600):02d}:{int(seconds % 3600 // 60):02d}:{int(seconds % 60):02d},000"

    blocks = [f"{i + 1}\n{ts(i * spacing)} --> {ts(i * spacing + 2)}\n{text}\n" for i in range(cues)]
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path


def test_rank_prefers_full_english_and_drops_partial_tracks(translator):
    streams = [
        _stream("ass", "eng", "Signs/Songs [sam]", forced=1),
        _stream("ass", "enm", "Honorifics [sam]"),
        _stream("hdmv_pgs_subtitle", "eng", "Full PGS"),
        _stream("ass", "eng", "English SDH"),
        _stream("ass", "eng", "Full Subtitles [sam]"),
        _stream("subrip", "spa", "Español"),
        _stream("subrip"),
    ]

    ranked = translator._rank_embedded_subtitle_streams(streams)

    # Inglés completo, inglés SDH, otro idioma, sin etiqueta. Fuera: forzada, bitmap y español
    assert [index for index, _ in ranked] == [4, 3, 1, 6]


def test_signs_track_is_partial_even_without_forced_flag(translator):
    assert translator._is_partial_subtitle_stream(_stream("ass", "eng", "Signs & Songs"))
    assert translator._is_partial_subtitle_stream(_stream("ass", "eng", None, forced=1))
    assert not translator._is_partial_subtitle_stream(_stream("ass", "eng", "Full Subtitles"))


def test_embedded_spanish_ignores_forced_tracks(translator, monkeypatch):
    video = Path("/media/video.mkv")
    monkeypatch.setattr(translator, "_detect_embedded_subtitle_streams", lambda v: [_stream("subrip", "spa", forced=1)])
    assert not translator._has_embedded_spanish_subtitles(video)

    monkeypatch.setattr(translator, "_detect_embedded_subtitle_streams", lambda v: [_stream("hdmv_pgs_subtitle", "spa")])
    assert translator._has_embedded_spanish_subtitles(video)


def test_subtitle_covers_video(translator, tmp_path):
    duration = 1440.0  # 24 minutos

    full = _write_srt(tmp_path / "full.srt", cues=300)  # llega a ~20 min
    assert translator._subtitle_covers_video(full, duration)

    # Pocas líneas repartidas (carteles): cubre el tiempo pero no la densidad
    sparse = _write_srt(tmp_path / "sparse.srt", cues=20, spacing=70.0)
    assert not translator._subtitle_covers_video(sparse, duration)

    # Denso pero solo los primeros minutos (extracción truncada)
    truncated = _write_srt(tmp_path / "truncated.srt", cues=100)
    assert not translator._subtitle_covers_video(truncated, duration)

    # Sin duración conocida basta con el mínimo de cues
    assert translator._subtitle_covers_video(truncated, None)
    assert not translator._subtitle_covers_video(_write_srt(tmp_path / "tiny.srt", cues=2), None)


def test_insufficient_sidecar_is_not_used_and_is_backed_up(translator, tmp_path, monkeypatch):
    video = tmp_path / "Movie.mkv"
    video.touch()
    sidecar = _write_srt(tmp_path / "Movie.srt", cues=20, spacing=70.0)
    monkeypatch.setattr(translator, "_media_duration", lambda v: 1440.0)

    assert translator.find_existing_srt_file(video) is None
    assert not sidecar.exists()
    assert (tmp_path / "Movie.srt.bak").exists()


def test_sufficient_sidecar_is_used(translator, tmp_path, monkeypatch):
    video = tmp_path / "Movie.mkv"
    video.touch()
    sidecar = _write_srt(tmp_path / "Movie.en.srt", cues=300)
    monkeypatch.setattr(translator, "_media_duration", lambda v: 1440.0)

    assert translator.find_existing_srt_file(video) == sidecar


def test_embedded_extraction_takes_first_track_that_covers_video(translator, tmp_path, monkeypatch):
    video = tmp_path / "Show.mkv"
    video.touch()
    streams = [_stream("ass", "eng", "Dialogue (typeset only)"), _stream("ass", "eng", "Full")]
    extracted = []

    def fake_extract(video_file, relative_index, lang):
        extracted.append(relative_index)
        out = tmp_path / f"cand{relative_index}.srt"
        # La primera pista resulta ser parcial aunque su título no lo diga
        return _write_srt(out, cues=10, spacing=100.0) if relative_index == 0 else _write_srt(out, cues=300)

    monkeypatch.setattr(translator, "_detect_embedded_subtitle_streams", lambda v: streams)
    monkeypatch.setattr(translator, "_media_duration", lambda v: 1440.0)
    monkeypatch.setattr(translator, "_extract_embedded_subtitles", fake_extract)

    result = translator._try_extract_embedded_subtitles(video)

    assert extracted == [0, 1]
    assert result == tmp_path / "Show.en.srt"
    assert not (tmp_path / "cand0.srt").exists()


def test_videos_pending_compression_are_left_for_later(translator, tmp_path, monkeypatch):
    pending = tmp_path / "Pending.mkv"
    ready = tmp_path / "Ready.mkv"
    pending.touch()
    ready.touch()
    monkeypatch.setattr("mediajelly_subtitle_translator.get_pending_files", lambda: [pending])
    monkeypatch.setattr(translator, "_get_processed_files_info", lambda: {})
    stats = {"errors": 0}

    assert translator._filter_valid_files([pending, ready], stats) == [ready]
    assert stats["errors"] == 0  # no es un error: sigue en la cola de subtítulos


def test_probe_media_runs_ffprobe_once_per_file(translator, monkeypatch):
    calls = []

    class _Result:
        returncode = 0
        stderr = ""
        stdout = '{"format": {"duration": "60.0"}, "streams": [{"codec_type": "audio", "tags": {"language": "SPA"}}]}'

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _Result()

    monkeypatch.setattr("mediajelly_subtitle_translator.subprocess.run", fake_run)
    video = Path("/media/video.mkv")

    assert translator._media_duration(video) == 60.0
    assert translator._detect_embedded_subtitle_streams(video) == []
    assert not translator._is_video_corrupted(video)
    assert len(calls) == 1


def test_split_words_into_cues_respects_limits():
    # 40 palabras de 0.5 s seguidas: 20 s de habla continua
    words = [(i * 0.5, i * 0.5 + 0.45, f" palabra{i}") for i in range(40)]
    # Pausa larga y una frase más
    words += [(30.0, 30.4, " Hola"), (30.4, 30.9, " mundo")]

    cues = WhisperAudioExtractorMixin._split_words_into_cues(words)

    assert all(cue["end"] - cue["start"] <= MAX_CUE_SECONDS for cue in cues)
    assert cues[-1] == {"start": 30.0, "end": 30.9, "text": "Hola mundo"}
    assert " ".join(cue["text"] for cue in cues[:-1]) == " ".join(f"palabra{i}" for i in range(40))


def test_split_words_joins_orphan_word_before_a_pause():
    # "聞" queda marcada antes del silencio y el resto de la frase llega 5 s después
    words = [(158.9, 159.1, "聞"), (164.2, 164.8, "かせろ"), (164.8, 165.5, "ルリー")]

    cues = WhisperAudioExtractorMixin._split_words_into_cues(words)

    assert cues == [{"start": 164.2, "end": 165.5, "text": "聞かせろルリー"}]


def test_normalize_cues_removes_overlaps_and_long_cues():
    segments = [
        {"start": 10.0, "end": 40.0, "text": " estirado sobre un silencio "},
        {"start": 0.0, "end": 3.0, "text": "primero"},
        {"start": 2.0, "end": 2.1, "text": "solapado"},
        {"start": 5.0, "end": 6.0, "text": "   "},
    ]

    cues = WhisperAudioExtractorMixin._normalize_cues(segments)

    assert [cue["text"] for cue in cues] == ["primero", "solapado", "estirado sobre un silencio"]
    assert cues[0]["end"] == 2.0  # recortado al inicio del siguiente
    assert cues[1]["end"] == pytest.approx(2.0 + MIN_CUE_SECONDS)
    assert cues[2]["end"] == 10.0 + MAX_CUE_SECONDS


def test_scanner_keeps_non_spanish_generic_srt_pending(tmp_path, monkeypatch):
    scanner = MediaScanner.__new__(MediaScanner)
    scanner.logger = _Logger()

    class _NoEmbedded:
        returncode = 0
        stdout = '{"streams": []}'

    monkeypatch.setattr("mediajelly_scanner.subprocess.run", lambda *a, **k: _NoEmbedded())

    english = tmp_path / "English.mkv"
    english.touch()
    _write_srt(tmp_path / "English.srt", cues=30, text="I think we should go there with the others")
    assert not scanner._has_spanish_subtitle(english)

    spanish = tmp_path / "Spanish.mkv"
    spanish.touch()
    _write_srt(tmp_path / "Spanish.srt", cues=30, text="Creo que el plan de la casa es para los que van con un amigo")
    assert scanner._has_spanish_subtitle(spanish)


def test_scanner_ignores_forced_embedded_spanish(tmp_path, monkeypatch):
    scanner = MediaScanner.__new__(MediaScanner)
    scanner.logger = _Logger()
    video = tmp_path / "Video.mkv"
    video.touch()

    class _Probe:
        returncode = 0
        stdout = '{"streams": [{"tags": {"language": "spa"}, "disposition": {"forced": 1}}]}'

    monkeypatch.setattr("mediajelly_scanner.subprocess.run", lambda *a, **k: _Probe())
    assert not scanner._has_spanish_subtitle(video)

    _Probe.stdout = '{"streams": [{"tags": {"language": "spa"}, "disposition": {"forced": 0}}]}'
    assert scanner._has_spanish_subtitle(video)


def test_cues_stay_on_screen_long_enough_to_read():
    text = "Aunque dijiste que Kattegat se dedicaría al comercio"  # 52 caracteres, ~3 s de lectura
    segments = [
        {"start": 10.0, "end": 11.2, "text": text},
        {"start": 30.0, "end": 30.4, "text": "Sí."},
        {"start": 30.9, "end": 31.5, "text": "Vamos."},
    ]

    cues = WhisperAudioExtractorMixin._normalize_cues(segments)

    assert cues[0]["end"] == pytest.approx(10.0 + len(text) / 17.0)
    assert cues[1]["end"] == pytest.approx(30.9)  # querría 1 s, pero no pisa al siguiente
    assert cues[2]["end"] == pytest.approx(30.9 + MIN_CUE_SECONDS)


def test_spanish_source_is_saved_without_translating(translator, tmp_path, monkeypatch):
    srt = _write_srt(tmp_path / "Show S01E01.srt", cues=40, text="Creo que el plan de la casa es para los que van con un amigo")
    monkeypatch.setattr(translator, "_translation_dependencies_available", lambda: pytest.fail("no debe traducir"))
    monkeypatch.setattr(translator, "_improve_translated_subtitles_quality", lambda path: None)

    result = translator.translate_srt(srt)

    assert result == tmp_path / "Show S01E01.es.srt"
    assert result.read_text(encoding="utf-8") == srt.read_text(encoding="utf-8")


def test_non_spanish_source_still_goes_to_translation(translator, tmp_path, monkeypatch):
    srt = _write_srt(tmp_path / "Show S01E02.srt", cues=40, text="I think we should go there with the others tonight")
    called = []
    monkeypatch.setattr(translator, "_translation_dependencies_available", lambda: called.append(1) or False)

    assert translator.translate_srt(srt) is None
    assert called == [1]


def _extractor(tmp_path):
    class _Extractor(WhisperAudioExtractorMixin):
        logger = _Logger()
        tmp_dir = tmp_path

    return _Extractor()


def test_hints_for_episode_come_from_series_nfo(tmp_path):
    season = tmp_path / "Vikings" / "Season 6"
    season.mkdir(parents=True)
    (tmp_path / "Vikings" / "tvshow.nfo").write_text(
        "<tvshow><title>Vikings</title><actor><name>X</name><role>Bjorn Ironside</role></actor>"
        "<actor><name>Y</name><role>Ubbe (voice)</role></actor><actor><name>Z</name><role>Ubbe</role></actor></tvshow>",
        encoding="utf-8",
    )
    video = season / "Vikings S06E02.mp4"
    video.with_suffix(".nfo").write_text("<episodedetails><title>The Prophet</title></episodedetails>", encoding="utf-8")

    assert _extractor(tmp_path)._transcription_hints(video) == "Personajes: Bjorn Ironside, Ubbe, The Prophet, Vikings."


def test_hints_for_movie_and_missing_nfo(tmp_path):
    folder = tmp_path / "Movie (2005)"
    folder.mkdir()
    video = folder / "Movie (2005).mp4"
    assert _extractor(tmp_path)._transcription_hints(video) is None

    video.with_suffix(".nfo").write_text(
        "<movie><title>Cruzada</title><actor><role>Balian de Ibelin</role></actor></movie>", encoding="utf-8"
    )
    assert _extractor(tmp_path)._transcription_hints(video) == "Personajes: Balian de Ibelin, Cruzada."


def test_hints_respect_length_limit(tmp_path):
    folder = tmp_path / "Big"
    folder.mkdir()
    video = folder / "Big.mp4"
    roles = "".join(f"<actor><role>Personaje Numero {i:03d}</role></actor>" for i in range(200))
    video.with_suffix(".nfo").write_text(f"<movie>{roles}</movie>", encoding="utf-8")

    hints = _extractor(tmp_path)._transcription_hints(video)

    assert len(hints) <= 400
    assert hints.startswith("Personajes: Personaje Numero 000, Personaje Numero 001")


def _words(text, start=0.0, step=0.4):
    return [(start + i * step, start + i * step + 0.35, " " + w) for i, w in enumerate(text.split())]


def test_cues_close_at_sentence_end():
    words = _words("No habrá clemencia si este muro cae. Si bajan las armas, sus familias morirán.")

    cues = WhisperAudioExtractorMixin._split_words_into_cues(words)

    assert [cue["text"] for cue in cues] == [
        "No habrá clemencia si este muro cae.",
        "Si bajan las armas, sus familias morirán.",
    ]


def test_long_cue_breaks_at_comma_not_mid_idea():
    text = (
        "Aunque dijiste que Kattegat se dedicaría solamente al comercio con todos los pueblos vecinos, "
        "no a conquistar otras tierras lejanas"
    )
    cues = WhisperAudioExtractorMixin._split_words_into_cues(_words(text, step=0.2))

    assert len(cues) == 2
    assert cues[0]["text"].endswith("vecinos,")
    assert cues[1]["text"].startswith("no a conquistar")


def test_continuation_line_keeps_lowercase():
    improver = SubtitleQualityImprover(logger=None)
    srt = (
        "1\n00:00:01,000 --> 00:00:03,000\nNo habrá clemencia si bajan las armas sus\n\n"
        "2\n00:00:03,000 --> 00:00:05,000\nfamilias morirán.\n\n"
        "3\n00:00:06,000 --> 00:00:08,000\nretírate o moriremos.\n"
    )

    out = improver.improve_subtitle_quality(srt, "es")

    assert "\nfamilias morirán." in out  # continúa la frase anterior: sin mayúscula forzada
    assert "\nRetírate o moriremos." in out  # tras un punto sí empieza frase


def test_scanner_queues_text_embedded_spanish_without_sidecar(tmp_path, monkeypatch):
    # El Roku no muestra mov_text incrustado: el video queda pendiente para exportar el .es.srt
    scanner = MediaScanner.__new__(MediaScanner)
    scanner.logger = _Logger()
    video = tmp_path / "Video.mp4"
    video.touch()

    class _Probe:
        returncode = 0
        stdout = '{"streams": [{"codec_name": "mov_text", "tags": {"language": "spa"}, "disposition": {"forced": 0}}]}'

    monkeypatch.setattr("mediajelly_scanner.subprocess.run", lambda *a, **k: _Probe())
    assert not scanner._has_spanish_subtitle(video)

    _write_srt(tmp_path / "Video.es.srt", cues=30)
    assert scanner._has_spanish_subtitle(video)


def test_strip_font_tags_keeps_italics():
    texto = '<font face="Trebuchet MS" size="24"><i>Nuestra máxima prioridad</i></font>'
    assert _strip_font_tags(texto) == "<i>Nuestra máxima prioridad</i>"


def test_export_embedded_spanish_writes_clean_sidecar(translator, tmp_path, monkeypatch):
    video = tmp_path / "Show S01E01.mp4"
    video.touch()
    streams = [_stream("mov_text", "spa", forced=1), _stream("mov_text", "spa"), _stream("mov_text", "spa")]
    extracted = []

    def fake_extract(video_file, relative_index, lang):
        extracted.append(relative_index)
        out = tmp_path / f"cand{relative_index}.srt"
        return _write_srt(out, cues=300, text='<font face="Trebuchet MS" size="24">Hola</font>')

    monkeypatch.setattr(translator, "_detect_embedded_subtitle_streams", lambda v: streams)
    monkeypatch.setattr(translator, "_media_duration", lambda v: 1200.0)
    monkeypatch.setattr(translator, "_extract_embedded_subtitles", fake_extract)

    result = translator._export_embedded_spanish(video)

    assert result == tmp_path / "Show S01E01.es.srt"
    assert extracted == [1]  # la forzada se salta y basta la primera completa
    content = result.read_text(encoding="utf-8")
    assert "<font" not in content and "Hola" in content
    assert not (tmp_path / "cand1.srt").exists()


def test_export_embedded_spanish_ignores_image_tracks(translator, tmp_path, monkeypatch):
    video = tmp_path / "Movie.mkv"
    video.touch()
    monkeypatch.setattr(
        translator, "_detect_embedded_subtitle_streams", lambda v: [_stream("hdmv_pgs_subtitle", "spa")]
    )
    monkeypatch.setattr(translator, "_extract_embedded_subtitles", lambda *a: pytest.fail("no debe extraer"))

    assert translator._export_embedded_spanish(video) is None


def test_process_video_keeps_existing_spanish_sidecar(translator, tmp_path, monkeypatch):
    video = tmp_path / "Show.mp4"
    video.touch()
    monkeypatch.setattr(translator, "quick_check_needs_processing", lambda v: True)
    monkeypatch.setattr(translator, "_is_video_corrupted", lambda v: False)
    monkeypatch.setattr(translator, "check_audio_tracks", lambda v: (False, []))
    monkeypatch.setattr(translator, "check_existing_spanish_subtitles", lambda v: True)
    monkeypatch.setattr(translator, "_export_embedded_spanish", lambda v: pytest.fail("no debe exportar"))

    result = translator.process_video(video)

    assert result["had_spanish_subtitles"] and not result["extracted"]


def test_process_video_exports_embedded_spanish_without_translating(translator, tmp_path, monkeypatch):
    video = tmp_path / "Show.mp4"
    video.touch()
    sidecar = tmp_path / "Show.es.srt"
    monkeypatch.setattr(translator, "quick_check_needs_processing", lambda v: True)
    monkeypatch.setattr(translator, "_is_video_corrupted", lambda v: False)
    monkeypatch.setattr(translator, "check_audio_tracks", lambda v: (False, []))
    monkeypatch.setattr(translator, "check_existing_spanish_subtitles", lambda v: False)
    monkeypatch.setattr(translator, "_export_embedded_spanish", lambda v: sidecar)
    monkeypatch.setattr(translator, "translate_srt", lambda *a, **k: pytest.fail("no debe traducir"))
    translator.translation_stats = SimpleNamespace(subtitles_extracted=0)

    result = translator.process_video(video)

    assert result["extracted"] and result["subtitle_file"] == str(sidecar)
    assert translator.translation_stats.subtitles_extracted == 1


class _FakeYandex:
    """Responde como Yandex: 502 sin letras, 413 por encima de 3000 caracteres."""

    def __init__(self):
        self.calls = []

    def translate(self, text, dest):
        self.calls.append(text)
        if not any(ch.isalpha() for ch in text):
            raise RuntimeError("502 | Unknown error. Error code: 502")
        if len(text) > 3000:
            raise RuntimeError("413 | ERR_TEXT_TOO_LONG")
        return SimpleNamespace(result=text.upper())


def _srt_lines(*textos):
    lines = []
    for n, texto in enumerate(textos, start=1):
        lines += [f"{n}\n", f"00:00:{n:02d},000 --> 00:00:{n:02d},900\n", f"{texto}\n", "\n"]
    return lines


def test_es_dibujo_ass_con_casos_reales():
    assert _es_dibujo_ass('<font face="Cabin F"size="50">m 64.78 11.69 l 64.4 6.85 b 64.37 6.3 64.44 5.66</font>')
    assert _es_dibujo_ass('<font size="50">{=10}</font><b>m -237.47 -6.89 l -237.99 -6.43 -237.34 -5.87</b>')
    assert not _es_dibujo_ass("m 5 km de aquí")
    assert not _es_dibujo_ass("Hola")


def test_limpiar_srt_quita_dibujos_y_renumera():
    srt = (
        "1\n00:00:01,000 --> 00:00:02,000\nHola\n\n"
        "2\n00:00:02,000 --> 00:00:02,000\nm 1 2 l 3 4 5 6\n\n"
        "3\n00:00:03,000 --> 00:00:04,000\nAdiós\n"
    )

    assert _limpiar_srt(srt) == "1\n00:00:01,000 --> 00:00:02,000\nHola\n\n2\n00:00:03,000 --> 00:00:04,000\nAdiós\n"
    assert _limpiar_srt("1\n00:00:01,000 --> 00:00:02,000\nHola\n") == "1\n00:00:01,000 --> 00:00:02,000\nHola\n"


def test_cues_sin_letras_no_se_envian_y_el_archivo_se_completa(translator):
    yandex = _FakeYandex()
    lines = _srt_lines("hello", *["…"] * 7, "。", "bye")

    translated, made = translator._translate_lines_batch(lines, yandex)

    assert made == 2
    assert "HELLO\n" in translated and "BYE\n" in translated and "…\n" in translated
    assert all(any(ch.isalpha() for ch in call) for call in yandex.calls)


def test_linea_larga_se_divide_y_vuelve_unida(translator):
    yandex = _FakeYandex()
    larga = "Hola mundo. " * 600

    translated, made = translator._translate_lines_batch(_srt_lines(larga.strip()), yandex)

    assert made == 1 and len(yandex.calls) == 3
    assert all(len(call) <= 3000 for call in yandex.calls)
    assert translated[2].strip() == larga.strip().upper()


def test_dividir_texto_japones_sin_espacios():
    assert [len(t) for t in _dividir_texto("あ" * 7000)] == [3000, 3000, 1000]
    assert _dividir_texto("corto") == ["corto"]


def test_texto_repetido_se_traduce_una_vez(translator):
    yandex = _FakeYandex()
    translator._cache_traduccion = {}

    translated, made = translator._translate_lines_batch(_srt_lines(*["Dead"] * 50), yandex)

    assert yandex.calls == ["Dead"]
    assert made == 50 and translated.count("DEAD\n") == 50


def test_cinco_fallos_seguidos_siguen_abandonando(translator):
    class _Caido:
        def translate(self, text, dest):
            raise TimeoutError("sin respuesta")

    with pytest.raises(TranslationStalledError):
        translator._translate_lines_batch(_srt_lines(*[f"line {n}" for n in range(6)]), _Caido())
