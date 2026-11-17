"""A ClipSession = one stream being buffered + (optionally) chat watched for hype."""
from __future__ import annotations

import atexit
import json
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from .clipper import SHORTS_MAX_SECONDS, clip_from_segments, shorts_kit
from .hype import HypeDetector, HypeEvent, make_chat
from .recorder import RollingRecorder
from .sources import Source, resolve


@dataclass
class ClipInfo:
    path: str
    source: str
    reason: str
    created: float
    duration: float
    fmt: str
    kit: str = ""  # path of the Shorts title/description .txt


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "clip"


class ClipSession:
    def __init__(
        self,
        source: Source,
        root: Path = Path("gtavi_data"),
        buffer_seconds: int = 300,
        engine: str = "streamlink",
        quality: str = "best",
        auto_clip: bool = False,
        auto_pre: float = 25.0,
        auto_post: float = 5.0,
        auto_fmt: str = "vertical-blur",
        sensitivity: float = 2.5,
        on_clip: Optional[Callable[[ClipInfo], None]] = None,
        tags: Sequence[str] = (),
    ):
        source = resolve(source)  # YouTube: @handle -> live video id + title (raises NotLiveError)
        self.source = source
        self.tags = list(tags)
        name = _slug(source.channel or source.url)
        self.buffer_dir = Path(root) / "buffer" / name
        self.clip_dir = Path(root) / "clips" / name
        self.recorder = RollingRecorder(source.url, self.buffer_dir, buffer_seconds, engine, quality)
        self.auto_clip = auto_clip
        self.auto_pre = auto_pre
        self.auto_post = auto_post
        self.auto_fmt = auto_fmt
        self.detector = HypeDetector(sensitivity=sensitivity)
        self.chat = None
        self.events: List[HypeEvent] = []
        self.clips: List[ClipInfo] = []
        self.markers: List[float] = []
        self.errors: List[str] = []
        self.on_clip = on_clip
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="gtavi-clip")
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self._stop.clear()
        self.recorder.start()
        atexit.register(self.stop)  # never leave streamlink/ffmpeg orphaned
        self.detector = HypeDetector(sensitivity=self.detector.sensitivity)
        self.chat = make_chat(self.source, lambda _u, text: self.detector.feed(text))
        if self.chat:
            self.chat.start()
            threading.Thread(target=self._hype_loop, name="gtavi-hype", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        if self.chat:
            self.chat.stop()
        self.recorder.stop()

    @property
    def running(self) -> bool:
        return self.recorder.running

    # ---------------------------------------------------------------- clipping
    def clip_last(self, seconds: float, fmt: str = "vertical-blur", fast: bool = False,
                  reason: str = "manual", caption: str = "") -> Future:
        end = self.recorder.now
        return self.clip_range(end - seconds, end, fmt, fast, reason, caption=caption)

    def clip_range(self, t0: float, t1: float, fmt: str = "vertical-blur", fast: bool = False,
                   reason: str = "manual", wait_for_end: bool = False, caption: str = "") -> Future:
        return self._pool.submit(self._do_clip, t0, t1, fmt, fast, reason, wait_for_end, caption)

    def mark(self) -> float:
        t = self.recorder.now
        self.markers.append(t)
        return t

    def _do_clip(self, t0: float, t1: float, fmt: str, fast: bool, reason: str,
                 wait_for_end: bool, caption: str = "") -> ClipInfo:
        if wait_for_end:  # e.g. auto-clip wants a few seconds *after* the hype moment
            deadline = time.time() + (t1 - self.recorder.now) + 15
            while self.recorder.now < t1 and time.time() < deadline and self.recorder.running:
                time.sleep(0.5)
        t0 = max(t0, self.recorder.oldest)
        t1 = min(t1, self.recorder.now)
        if fmt.startswith("vertical") and t1 - t0 > SHORTS_MAX_SECONDS:
            t0 = t1 - SHORTS_MAX_SECONDS  # keep it a valid Short
        if t1 - t0 < 1:
            raise ValueError("Not enough video buffered yet - give it a few seconds.")
        files, offset = self.recorder.segments_for(t0, t1)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = self.clip_dir / f"{stamp}_{_slug(reason)}_{fmt}.mp4"
        clip_from_segments(files, offset, t1 - t0, dest, fmt=fmt, fast=fast, caption=caption)
        kit = shorts_kit(dest, self.source.title or self.source.label, self.source.share_url, reason, self.tags)
        info = ClipInfo(str(dest), self.source.label, reason, time.time(), round(t1 - t0, 1), fmt, str(kit))
        with self._lock:
            self.clips.append(info)
        with open(self.clip_dir / "clips.jsonl", "a") as f:
            f.write(json.dumps(asdict(info)) + "\n")
        if self.on_clip:
            self.on_clip(info)
        return info

    # ---------------------------------------------------------------- auto-clip
    def _hype_loop(self) -> None:
        while not self._stop.wait(1.0):
            ev = self.detector.check()
            if not ev:
                continue
            self.events.append(ev)
            if self.auto_clip and self.recorder.running:
                t = self.recorder.now
                fut = self.clip_range(t - self.auto_pre, t + self.auto_post, self.auto_fmt,
                                      reason=f"hype {ev.describe()}", wait_for_end=True)
                fut.add_done_callback(self._log_failure)

    def _log_failure(self, fut: Future) -> None:
        err = fut.exception()
        if err:
            self.errors.append(f"auto-clip failed: {err}")
