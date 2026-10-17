import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import mediajelly_db as db  # noqa: E402
import mediajelly_processor as processor_module  # noqa: E402
from mediajelly_processor import MediaJellyProcessor  # noqa: E402

requiere_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg no disponible")


class _Logger:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _proc():
    proc = MediaJellyProcessor.__new__(MediaJellyProcessor)
    proc.logger = _Logger()
    return proc


def _video(path: Path, comment: str = "") -> Path:
    cmd = ["ffmpeg", "-v", "error", "-y",
           "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=2",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
           "-c:v", "libx264", "-c:a", "aac", "-metadata:s:a:0", "language=spa"]
    if comment:
        cmd += ["-metadata", f"comment={comment}"]
    subprocess.run(cmd + [str(path)], check=True)
    return path


def _comment(path: Path) -> str:
    return subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format_tags=comment", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True).stdout.strip()


@requiere_ffmpeg
@pytest.mark.parametrize("builder,kwargs", [
    ("_build_remux_command", {"sub_indices": []}),
    ("_build_ffmpeg_command", {"sub_indices": []}),
    ("_build_fallback_ffmpeg_command", {"sub_indices": []}),
    ("_build_ffmpeg_command_no_subtitles", {}),
])
def test_every_builder_writes_processed_marker(tmp_path, builder, kwargs):
    src = _video(tmp_path / "a.mp4")
    cmd = getattr(_proc(), builder)(src, tmp_path / "out.mp4", audio_indices=[0], audio_languages={0: "spa"}, **kwargs)

    assert cmd[-1] == str(tmp_path / "out.mp4")
    i = cmd.index("-metadata", cmd.index(str(src)))
    assert f"comment={processor_module.MARCA_PROCESADO}" in cmd[i:]


def _conforming_stubs(proc, monkeypatch):
    monkeypatch.setattr(proc, "_get_video_codec", lambda p: "h264")
    monkeypatch.setattr(proc, "_get_video_bitrate", lambda p: {"bitrate_kbps": 2000})
    monkeypatch.setattr(proc, "_get_video_resolution", lambda p: {"width": 1920, "height": 1080, "pix_fmt": "yuv420p"})
    monkeypatch.setattr(proc, "detect_language_streams", lambda *a, **k: (True, [0], [], {0: "spa"}))


@requiere_ffmpeg
def test_fresh_download_without_marker_does_not_conform(tmp_path, monkeypatch):
    """Un .mp4 recién descargado (sin la marca) nunca 'ya cumple': debe procesarse."""
    proc = _proc()
    _conforming_stubs(proc, monkeypatch)

    assert proc._already_conforms(_video(tmp_path / "nuevo.mp4")) is False


@requiere_ffmpeg
def test_processed_output_with_marker_conforms(tmp_path, monkeypatch):
    proc = _proc()
    _conforming_stubs(proc, monkeypatch)

    assert proc._already_conforms(_video(tmp_path / "hecho.mp4", processor_module.MARCA_PROCESADO)) is True


@requiere_ffmpeg
def test_retag_keeps_processed_marker(tmp_path):
    import mediajelly_retag

    video = _video(tmp_path / "hecho.mp4", processor_module.MARCA_PROCESADO)
    assert mediajelly_retag.retag_audio(video, {1: "jpn"}) is True

    assert _comment(video) == processor_module.MARCA_PROCESADO


def test_requeue_sets_pending_for_file_and_folder(tmp_path):
    import mediajelly_requeue

    carpeta = tmp_path / "Serie" / "Season 1"
    carpeta.mkdir(parents=True)
    a, b = carpeta / "a.mp4", carpeta / "b.mkv"
    a.write_bytes(b"a" * 4096)
    b.write_bytes(b"b" * 4096)
    for f in (a, b):
        rec, _ = db.register(f, require_settled=False)
        db.set_status(rec.fingerprint, "done")

    assert mediajelly_requeue.requeue([a]) == 1
    assert db.get_by_path(a).status == "pending"
    assert db.get_by_path(b).status == "done"

    assert mediajelly_requeue.requeue([tmp_path / "Serie"]) == 2
    assert db.get_by_path(b).status == "pending"
