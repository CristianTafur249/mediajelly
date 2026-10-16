import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import mediajelly_db  # noqa: E402
import mediajelly_language_detector as detector  # noqa: E402
import mediajelly_retag  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg no disponible")


@pytest.fixture
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "t.db"
    monkeypatch.setattr(mediajelly_db, "_get_db_path", lambda: db_path)
    return db_path


def _fake_media(path: Path, size: int = 5000) -> Path:
    path.write_bytes(bytes(range(256)) * (size // 256 + 1))
    return path


def test_cached_language_survives_rename(db, tmp_path):
    """Regresión de Eminence in Shadow: el idioma sigue al contenido, no a la ruta."""
    a = _fake_media(tmp_path / "a.mp4")
    detector.set_cached_language(a, 1, "ja")
    b = tmp_path / "b.mp4"
    a.rename(b)
    assert detector.get_cached_language(b, 1) == "jpn"


def test_cached_language_missing_returns_none(db, tmp_path):
    a = _fake_media(tmp_path / "a.mp4")
    assert detector.get_cached_language(a, 1) is None


def _probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=index,codec_type:stream_tags=language",
         "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)["streams"]


def _make_mp4(path: Path):
    meta = path.with_suffix(".ffmeta")
    meta.write_text(";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1500\ntitle=uno\n"
                    "[CHAPTER]\nTIMEBASE=1/1000\nSTART=1500\nEND=3000\ntitle=dos\n")
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-v", "error", "-y",
         "-f", "lavfi", "-i", "color=c=black:s=64x64:d=3:r=10",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
         "-i", str(meta), "-map", "0:v", "-map", "1:a", "-map_metadata", "2",
         "-metadata:s:a:0", "language=und", "-c:v", "libx264", "-c:a", "aac", str(path)],
        check=True)


@needs_ffmpeg
def test_retag_audio_writes_tag_and_keeps_streams(tmp_path):
    p = tmp_path / "v.mp4"
    _make_mp4(p)
    before = [s["codec_type"] for s in _probe(p)]
    audio_idx = [s["index"] for s in _probe(p) if s["codec_type"] == "audio"][0]

    assert mediajelly_retag.retag_audio(p, {audio_idx: "jpn"}) is True

    after = _probe(p)
    assert [s["codec_type"] for s in after] == before
    audio = [s for s in after if s["codec_type"] == "audio"][0]
    assert audio["tags"]["language"] == "jpn"


@needs_ffmpeg
def test_retag_audio_validation_failure_leaves_original(tmp_path, monkeypatch):
    p = tmp_path / "v.mp4"
    _make_mp4(p)
    audio_idx = [s["index"] for s in _probe(p) if s["codec_type"] == "audio"][0]
    original = p.read_bytes()
    monkeypatch.setattr(mediajelly_retag, "_validate", lambda *a, **k: False)

    assert mediajelly_retag.retag_audio(p, {audio_idx: "jpn"}) is False

    assert p.read_bytes() == original
    assert not list(tmp_path.glob("*.retag*"))


@needs_ffmpeg
def test_retag_audio_ignores_und(tmp_path):
    p = tmp_path / "v.mp4"
    _make_mp4(p)
    audio_idx = [s["index"] for s in _probe(p) if s["codec_type"] == "audio"][0]
    assert mediajelly_retag.retag_audio(p, {audio_idx: "und"}) is False


def test_retag_after_detection_only_for_done_and_und(db, tmp_path, monkeypatch):
    p = _fake_media(tmp_path / "e.mp4")
    rec, _ = mediajelly_db.register(p, require_settled=False)
    calls = []
    monkeypatch.setattr(detector, "retag_audio", lambda path, langs: calls.append(langs) or True)
    monkeypatch.setattr(detector, "_audio_tags", lambda path: {1: "und"})

    # pending: no re-etiqueta
    assert detector.retag_if_processed(p, {1: "jpn"}) is False
    assert calls == []

    mediajelly_db.set_status(rec.fingerprint, "done")
    assert detector.retag_if_processed(p, {1: "und"}) is False  # nunca und
    assert calls == []
    assert detector.retag_if_processed(p, {1: "jpn"}) is True
    assert calls == [{1: "jpn"}]
    assert mediajelly_db.get_by_path(p).status == "done"


# ---- Ronda de corrección 1 ----

@needs_ffmpeg
def test_retag_temp_is_hidden(tmp_path, monkeypatch):
    p = tmp_path / "v.mp4"
    _make_mp4(p)
    audio_idx = [s["index"] for s in _probe(p) if s["codec_type"] == "audio"][0]
    outputs = []
    real_run = subprocess.run

    def spy(cmd, *a, **k):
        if cmd and cmd[0] == "ffmpeg":
            outputs.append(Path(cmd[-1]))
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(mediajelly_retag.subprocess, "run", spy)
    assert mediajelly_retag.retag_audio(p, {audio_idx: "jpn"}) is True
    assert outputs and outputs[0].name.startswith(".") and outputs[0].name.endswith(".retag.tmp")
    assert outputs[0].parent == tmp_path
    assert not outputs[0].exists()


@needs_ffmpeg
def test_retag_cleans_orphan_temp(tmp_path):
    p = tmp_path / "v.mp4"
    _make_mp4(p)
    audio_idx = [s["index"] for s in _probe(p) if s["codec_type"] == "audio"][0]
    orphan = tmp_path / ".otro.mp4.retag.tmp"
    orphan.write_bytes(b"basura")
    assert mediajelly_retag.retag_audio(p, {audio_idx: "jpn"}) is True
    assert not orphan.exists()


@needs_ffmpeg
def test_retag_timeout_leaves_original(tmp_path, monkeypatch):
    p = tmp_path / "v.mp4"
    _make_mp4(p)
    audio_idx = [s["index"] for s in _probe(p) if s["codec_type"] == "audio"][0]
    original = p.read_bytes()
    real_run = subprocess.run

    def slow(cmd, *a, **k):
        if cmd and cmd[0] == "ffmpeg":
            Path(cmd[-1]).write_bytes(b"parcial")
            raise subprocess.TimeoutExpired(cmd, k.get("timeout", 0))
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(mediajelly_retag.subprocess, "run", slow)
    assert mediajelly_retag.retag_audio(p, {audio_idx: "jpn"}) is False
    assert p.read_bytes() == original
    assert not list(tmp_path.glob(".*retag*"))


def _done_file(tmp_path, name="e.mp4"):
    p = _fake_media(tmp_path / name)
    rec, _ = mediajelly_db.register(p, require_settled=False)
    mediajelly_db.set_status(rec.fingerprint, "done")
    return p, rec


def test_failed_retag_not_retried(db, tmp_path, monkeypatch):
    p, rec = _done_file(tmp_path)
    calls = []
    monkeypatch.setattr(detector, "retag_audio", lambda path, langs: calls.append(langs) or False)
    monkeypatch.setattr(detector, "_audio_tags", lambda path: {1: "und"})
    assert detector.retag_if_processed(p, {1: "jpn"}) is False
    assert detector.retag_if_processed(p, {1: "jpn"}) is False
    assert len(calls) == 1
    assert mediajelly_db.has_retag_failure(rec.fingerprint) is True


def test_retag_normalizes_and_ignores_und(tmp_path, monkeypatch):
    p = tmp_path / "v.mp4"
    p.write_bytes(b"x")
    seen = []
    monkeypatch.setattr(mediajelly_retag, "_audio_indices", lambda path: [1, 2])
    monkeypatch.setattr(mediajelly_retag.subprocess, "run",
                        lambda cmd, *a, **k: seen.append(cmd) or subprocess.CompletedProcess(cmd, 1, "", "x"))
    mediajelly_retag.retag_audio(p, {1: "ja", 2: ""})
    cmd = seen[0]
    assert "language=jpn" in cmd and "language=ja" not in cmd
    assert sum(1 for c in cmd if str(c).startswith("-metadata:s:a:")) == 1


def test_single_remux_per_file_per_run(db, tmp_path, monkeypatch):
    p, rec = _done_file(tmp_path)
    detector.set_cached_language(p, 1, "ja")
    calls = []
    monkeypatch.setattr(detector, "retag_audio", lambda path, langs: calls.append(dict(langs)) or True)
    monkeypatch.setattr(detector, "_audio_tags", lambda path: {1: "und", 2: "und"})
    monkeypatch.setattr(detector, "load_whisper_model", lambda: object())
    monkeypatch.setattr(detector, "detect_language_with_multiple_samples", lambda *a, **k: "en")
    info = {"absolute_path": str(p), "relative_path": "e.mp4", "streams": [(2, "aac", 60.0)],
            "known": {1: "jpn"}}
    detector.process_files([info], {})
    assert calls == [{1: "jpn", 2: "eng"}]


# ---- Revisión final: escaneo de done/skipped, intentos y caché JSON ----

def _probe_json(*langs):
    streams = [{"index": 0, "codec_type": "video", "codec_name": "h264"}]
    for i, lang in enumerate(langs, 1):
        tags = {"language": lang} if lang else {}
        streams.append({"index": i, "codec_type": "audio", "codec_name": "aac", "tags": tags})
    return json.dumps({"streams": streams})


def _fake_ffprobe(monkeypatch, by_name, calls):
    """safe_subprocess_run falso: devuelve las pistas según el nombre del archivo."""
    def run(cmd, **kwargs):
        calls.append(cmd)
        name = Path(cmd[-1]).name
        if "format=duration" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "600.0\n", "")
        return subprocess.CompletedProcess(cmd, 0, by_name[name], "")
    monkeypatch.setattr(detector, "safe_subprocess_run", run)


def _unique_media(path: Path) -> Path:
    """Contenido distinto por archivo (si no, register los toma por duplicados)."""
    path.write_bytes(path.name.encode() * 300)
    return path


def _with_status(path, status):
    rec, _ = mediajelly_db.register(path, require_settled=False)
    mediajelly_db.set_status(rec.fingerprint, status)
    return rec


def test_scan_includes_processed_files_with_und(db, tmp_path, monkeypatch):
    monkeypatch.setattr(detector, "MEDIA_PATHS", [str(tmp_path)])
    monkeypatch.setattr(detector, "log", lambda *a, **k: None)
    files = {
        "pend.mp4": ("pending", _probe_json("und")),
        "done_und.mp4": ("done", _probe_json("jpn", "und")),
        "skip_und.mp4": ("skipped", _probe_json("")),
        "done_ok.mp4": ("done", _probe_json("jpn")),
        "fallo.mp4": ("failed", _probe_json("und")),
    }
    for name, (status, _) in files.items():
        _with_status(_unique_media(tmp_path / name), status)
    calls = []
    _fake_ffprobe(monkeypatch, {n: out for n, (_, out) in files.items()}, calls)

    found = {Path(f["absolute_path"]).name: f["streams"] for f in detector.scan_files_needing_detection()}

    assert sorted(found) == ["done_und.mp4", "pend.mp4", "skip_und.mp4"]
    assert [s[0] for s in found["done_und.mp4"]] == [2]
    # Barato: un ffprobe de pistas por archivo y, si hace falta, uno de duración
    probes = [c for c in calls if "format=duration" not in c]
    assert len(probes) == 4
    assert len(calls) - len(probes) == 3


def test_scan_skips_processed_with_retag_failure_or_recent_attempt(db, tmp_path, monkeypatch):
    monkeypatch.setattr(detector, "MEDIA_PATHS", [str(tmp_path)])
    monkeypatch.setattr(detector, "log", lambda *a, **k: None)
    a = _unique_media(tmp_path / "a.mp4")
    b = _unique_media(tmp_path / "b.mp4")
    mediajelly_db.record_retag_failure(_with_status(a, "done").fingerprint, "error")
    mediajelly_db.record_language_attempt(_with_status(b, "done").fingerprint)
    calls = []
    _fake_ffprobe(monkeypatch, {"a.mp4": _probe_json("und"), "b.mp4": _probe_json("und")}, calls)

    assert detector.scan_files_needing_detection() == []
    assert calls == []


def test_whisper_without_result_records_attempt_for_processed(db, tmp_path, monkeypatch):
    done = _unique_media(tmp_path / "done.mp4")
    pend = _unique_media(tmp_path / "pend.mp4")
    rec_done = _with_status(done, "done")
    rec_pend = _with_status(pend, "pending")
    monkeypatch.setattr(detector, "load_whisper_model", lambda: object())
    monkeypatch.setattr(detector, "detect_language_with_multiple_samples", lambda *a, **k: None)
    monkeypatch.setattr(detector, "retag_audio", lambda *a, **k: pytest.fail("no hay idioma que escribir"))
    infos = [{"absolute_path": str(p), "relative_path": p.name, "streams": [(1, "aac", 600.0)]} for p in (done, pend)]

    detector.process_files(infos, {})

    # El terminado queda anotado (no vuelve a ser candidato); el pendiente no
    assert mediajelly_db.list_language_candidates() == []
    assert mediajelly_db.get_by_path(done).status == "done"
    assert [r.fingerprint for r in mediajelly_db.list_by_status("pending")] == [rec_pend.fingerprint]
    rows = sqlite3.connect(str(db)).execute("SELECT fingerprint FROM language_attempts").fetchall()
    assert rows == [(rec_done.fingerprint,)]


def test_process_files_does_not_write_json_cache(db, tmp_path, monkeypatch):
    cache_file = tmp_path / "language_cache.json"
    monkeypatch.setattr(detector, "CACHE_FILE_PATH", str(cache_file))
    p = _fake_media(tmp_path / "p.mp4")
    _with_status(p, "pending")
    monkeypatch.setattr(detector, "load_whisper_model", lambda: object())
    monkeypatch.setattr(detector, "detect_language_with_multiple_samples", lambda *a, **k: "ja")

    detector.process_files([{"absolute_path": str(p), "relative_path": "p.mp4", "streams": [(1, "aac", 60.0)]}], {})

    assert not cache_file.exists()
    assert detector.get_cached_language(p, 1) == "jpn"


def test_rename_in_detector_keeps_done_state(db, tmp_path):
    old = _unique_media(tmp_path / "[Fansub] Show - 01.mp4")
    _with_status(old, "done")
    pend_old = _unique_media(tmp_path / "otro.mp4")
    _with_status(pend_old, "pending")
    new = tmp_path / "Show - 01.mp4"
    old.rename(new)
    pend_new = tmp_path / "otro2.mp4"
    pend_old.rename(pend_new)

    detector._update_pending_and_completed(old, new)
    detector._update_pending_and_completed(pend_old, pend_new)

    assert mediajelly_db.get_by_path(new).status == "done"
    assert mediajelly_db.get_by_path(pend_new).status == "pending"
    assert mediajelly_db.get_pending_files() == [pend_new]


@needs_ffmpeg
def test_main_writes_known_language_into_done_file(db, tmp_path, monkeypatch):
    """Integración: done con und e idioma ya en la BD → main() escribe la etiqueta sin Whisper."""
    monkeypatch.setattr(detector, "MEDIA_PATHS", [str(tmp_path)])
    monkeypatch.setattr(detector, "CACHE_FILE_PATH", str(tmp_path / "language_cache.json"))
    monkeypatch.setattr(detector, "load_whisper_model", lambda: pytest.fail("no debía cargar Whisper"))
    p = tmp_path / "Serie - S01E01.mp4"
    _make_mp4(p)
    audio_idx = [s["index"] for s in _probe(p) if s["codec_type"] == "audio"][0]
    rec = _with_status(p, "done")
    mediajelly_db.set_stream_language(rec.fingerprint, audio_idx, "jpn", "cache")

    detector.main()

    audio = [s for s in _probe(p) if s["codec_type"] == "audio"][0]
    assert audio["tags"]["language"] == "jpn"
    after = mediajelly_db.get_by_path(p)
    assert after.status == "done"
    assert after.fingerprint != rec.fingerprint
    assert not (tmp_path / "language_cache.json").exists()


@needs_ffmpeg
def test_main_does_not_retry_whisper_within_seven_days(db, tmp_path, monkeypatch):
    monkeypatch.setattr(detector, "MEDIA_PATHS", [str(tmp_path)])
    monkeypatch.setattr(detector, "CACHE_FILE_PATH", str(tmp_path / "language_cache.json"))
    p = tmp_path / "Serie - S01E02.mp4"
    _make_mp4(p)
    _with_status(p, "done")
    whisper_calls = []
    monkeypatch.setattr(detector, "load_whisper_model", lambda: object())
    monkeypatch.setattr(
        detector, "detect_language_with_multiple_samples", lambda *a, **k: whisper_calls.append(1) and None
    )

    detector.main()
    detector.main()

    assert len(whisper_calls) == 1
    audio = [s for s in _probe(p) if s["codec_type"] == "audio"][0]
    assert audio["tags"]["language"] == "und"
