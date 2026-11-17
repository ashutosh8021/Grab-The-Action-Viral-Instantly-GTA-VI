"""Fake a live stream with ffmpeg's test source and run it through the real recorder + clipper."""
import re
import subprocess
import time

import pytest

from gtavi import recorder as rec_mod
from gtavi.clipper import ffmpeg_exe
from gtavi.session import ClipSession
from gtavi.sources import Source


def duration_of(path):
    err = subprocess.run([ffmpeg_exe(), "-i", str(path)], capture_output=True, text=True).stderr
    h, m, s = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err).groups()
    return int(h) * 3600 + int(m) * 60 + float(s)


def size_of(path):
    err = subprocess.run([ffmpeg_exe(), "-i", str(path)], capture_output=True, text=True).stderr
    return tuple(map(int, re.search(r"Video: .*?(\d{3,})x(\d{3,})", err).groups()))


@pytest.fixture
def fake_stream(monkeypatch):
    def fake_puller(url, engine, quality):
        return [
            ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-re",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=440",
            "-t", "30", "-c:v", "libx264", "-preset", "ultrafast", "-g", "30",
            "-c:a", "aac", "-f", "mpegts", "pipe:1",
        ]
    monkeypatch.setattr(rec_mod, "puller_command", fake_puller)


def test_buffer_and_clip(fake_stream, tmp_path):
    s = ClipSession(Source("other", "fake://", "test"), tmp_path, buffer_seconds=6)
    s.start()
    try:
        deadline = time.time() + 30
        while s.recorder.now < 10 and time.time() < deadline:
            time.sleep(0.5)
        assert s.recorder.running, s.recorder.error
        assert s.recorder.now >= 10
        # rolling buffer keeps only ~6s on disk
        assert s.recorder.buffered <= 6 + 2.5
        assert len(list(s.buffer_dir.glob("seg_*.ts"))) <= 6

        clip = s.clip_last(4, fmt="original").result(timeout=60)
        assert abs(duration_of(clip.path) - 4) < 0.5

        vert = s.clip_last(3, fmt="vertical-blur", caption="wait for it").result(timeout=60)
        assert size_of(vert.path) == (1080, 1920)
        assert "#shorts" in open(vert.kit, encoding="utf-8").read()

        fast = s.clip_last(4, fmt="original", fast=True).result(timeout=60)
        assert duration_of(fast.path) > 2

        assert (s.clip_dir / "clips.jsonl").read_text().count("\n") == 3
    finally:
        s.stop()
    assert not s.running
