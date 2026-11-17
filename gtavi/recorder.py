"""Rolling "instant replay" buffer for a live stream.

streamlink (or yt-dlp) pulls the stream -> ffmpeg chops it into ~2s MPEG-TS segments
without re-encoding -> we keep only the last N minutes on disk. Clipping is then just
"pick the segments covering [t0, t1], glue them, trim".
"""
from __future__ import annotations

import csv
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, List, Optional, Tuple

from .clipper import ffmpeg_exe
from .sources import tool_env

SEGMENT_SECONDS = 2
_SEG_RE = re.compile(r"seg_(\d+)\.ts$")


@dataclass
class Segment:
    index: int
    path: Path
    t0: float  # seconds on our own continuous timeline (0 = recording start)
    t1: float
    wall: float  # wall-clock time the segment finished

    @property
    def duration(self) -> float:
        return self.t1 - self.t0


def puller_command(url: str, engine: str, quality: str) -> List[str]:
    if engine == "streamlink":
        return [
            sys.executable, "-m", "streamlink",
            "--stdout", "--loglevel", "warning",
            "--retry-streams", "5", "--retry-open", "3",
            url, quality,
        ]
    if engine == "yt-dlp":
        return [
            sys.executable, "-m", "yt_dlp",
            "--quiet", "--no-part", "--no-live-from-start",
            "-f", "best" if quality == "best" else quality,
            "-o", "-", url,
        ]
    raise ValueError(f"unknown engine {engine!r}")


class RollingRecorder:
    """Keeps the last `buffer_seconds` of a live stream on disk."""

    def __init__(
        self,
        url: str,
        workdir: Path,
        buffer_seconds: int = 300,
        engine: str = "streamlink",
        quality: str = "best",
    ):
        self.url = url
        self.dir = Path(workdir)
        self.buffer_seconds = buffer_seconds
        self.engine = engine
        self.quality = quality

        self._segments: Deque[Segment] = deque()
        self._last_idx = -1
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._procs: List[subprocess.Popen] = []
        self._thread: Optional[threading.Thread] = None
        self.started_at: Optional[float] = None
        self.error: Optional[str] = None

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self.running:
            return
        if self.dir.exists():
            shutil.rmtree(self.dir)
        self.dir.mkdir(parents=True)
        self._segments.clear()
        self._last_idx = -1
        self._stop.clear()
        self.error = None

        list_size = int(self.buffer_seconds / SEGMENT_SECONDS) + 10
        self._puller_log = open(self.dir / "puller.log", "w")
        self._ffmpeg_log = open(self.dir / "ffmpeg.log", "w")
        puller = subprocess.Popen(
            puller_command(self.url, self.engine, self.quality),
            stdout=subprocess.PIPE, stderr=self._puller_log, env=tool_env(),
        )
        ff = subprocess.Popen(
            [
                ffmpeg_exe(), "-hide_banner", "-loglevel", "warning",
                "-fflags", "+genpts+discardcorrupt", "-i", "pipe:0",
                "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy",
                "-f", "segment", "-segment_time", str(SEGMENT_SECONDS),
                "-segment_format", "mpegts",
                "-segment_list", str(self.dir / "segments.csv"),
                "-segment_list_type", "csv", "-segment_list_size", str(list_size),
                str(self.dir / "seg_%07d.ts"),
            ],
            stdin=puller.stdout, stderr=self._ffmpeg_log,
        )
        puller.stdout.close()  # ffmpeg owns the pipe now
        self._procs = [puller, ff]
        self.started_at = time.time()
        self._thread = threading.Thread(target=self._watch, name="gtavi-recorder", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        for p in self._procs:
            if p.poll() is None:
                p.terminate()
        for p in self._procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        self._procs = []
        for log in (getattr(self, "_puller_log", None), getattr(self, "_ffmpeg_log", None)):
            if log and not log.closed:
                log.close()

    @property
    def running(self) -> bool:
        return bool(self._procs) and all(p.poll() is None for p in self._procs)

    # ---------------------------------------------------------------- timeline
    @property
    def now(self) -> float:
        """Latest moment we have on disk, on the recorder timeline."""
        with self._lock:
            return self._segments[-1].t1 if self._segments else 0.0

    @property
    def oldest(self) -> float:
        with self._lock:
            return self._segments[0].t0 if self._segments else 0.0

    @property
    def buffered(self) -> float:
        return self.now - self.oldest

    def segments_for(self, t0: float, t1: float) -> Tuple[List[Path], float]:
        """Segment files covering [t0, t1] plus the offset of t0 into the first file."""
        with self._lock:
            hits = [s for s in self._segments if s.t1 > t0 and s.t0 < t1]
        if not hits:
            return [], 0.0
        return [s.path for s in hits], max(0.0, t0 - hits[0].t0)

    # ---------------------------------------------------------------- internals
    def _watch(self) -> None:
        csv_path = self.dir / "segments.csv"
        while not self._stop.is_set():
            if csv_path.exists():
                self._ingest(csv_path)
            self._prune()
            if self._procs and any(p.poll() is not None for p in self._procs) and not self._stop.is_set():
                self.error = self._tail_logs() or "stream ended"
                self.stop()
                return
            time.sleep(0.5)

    def _ingest(self, csv_path: Path) -> None:
        try:
            rows = list(csv.reader(csv_path.read_text().splitlines()))
        except OSError:
            return
        for row in rows:
            if len(row) < 3:
                continue
            m = _SEG_RE.search(row[0])
            if not m:
                continue
            idx = int(m.group(1))
            if idx <= self._last_idx:
                continue
            try:
                dur = max(0.0, float(row[2]) - float(row[1]))
            except ValueError:
                continue
            with self._lock:
                t0 = self._segments[-1].t1 if self._segments else 0.0
                self._segments.append(Segment(idx, self.dir / row[0], t0, t0 + dur, time.time()))
            self._last_idx = idx

    def _prune(self) -> None:
        with self._lock:
            while self._segments and self._segments[-1].t1 - self._segments[0].t1 > self.buffer_seconds:
                old = self._segments.popleft()
                old.path.unlink(missing_ok=True)

    def _tail_logs(self) -> str:
        out = []
        for name in ("puller.log", "ffmpeg.log"):
            p = self.dir / name
            if p.exists():
                lines = [ln for ln in p.read_text(errors="ignore").splitlines() if ln.strip()]
                out += lines[-4:]
        return "\n".join(out)
