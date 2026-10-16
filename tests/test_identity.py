import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import mediajelly_identity as identity  # noqa: E402


def _write_random(path, size):
    path.write_bytes(os.urandom(size))
    return path


def test_fingerprint_stable_after_rename(tmp_path):
    original = _write_random(tmp_path / "a.mkv", 5 * 1024 * 1024)
    before = identity.fingerprint(original)
    renamed = tmp_path / "b.mkv"
    original.rename(renamed)
    assert identity.fingerprint(renamed) == before


def test_fingerprint_changes_with_content(tmp_path):
    size = 5 * 1024 * 1024
    path = _write_random(tmp_path / "a.mkv", size)
    before = identity.fingerprint(path)
    data = bytearray(path.read_bytes())
    data[size // 2] ^= 0xFF
    path.write_bytes(bytes(data))
    assert identity.fingerprint(path) != before


def test_fingerprint_small_file_uses_whole_content(tmp_path):
    base = bytearray(10 * 1024)
    other = bytearray(base)
    other[5000] = 1
    a = tmp_path / "a.mkv"
    b = tmp_path / "b.mkv"
    a.write_bytes(bytes(base))
    b.write_bytes(bytes(other))
    assert identity.fingerprint(a) != identity.fingerprint(b)


def test_fingerprint_format(tmp_path):
    path = _write_random(tmp_path / "a.mkv", 4 * 1024 * 1024)
    fp = identity.fingerprint(path)
    assert re.fullmatch(r"\d+:[0-9a-f]{40}", fp)
    assert fp.split(":")[0] == str(path.stat().st_size)


def test_is_settled(tmp_path):
    path = _write_random(tmp_path / "a.mkv", 1024)
    mtime = path.stat().st_mtime
    assert identity.is_settled(path, now=mtime + 60) is False
    assert identity.is_settled(path, now=mtime + 121) is True
    assert identity.is_settled(tmp_path / "no_existe.mkv", now=mtime + 500) is False
