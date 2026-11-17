"""ffmpeg plumbing: stitch buffered segments together and cut / reformat clips."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

# Output formats. "vertical" variants are 1080x1920 for Shorts / Reels / TikTok.
FORMATS = {
    "vertical-blur": "Shorts 9:16 · full frame on blurred background",
    "vertical-crop": "Shorts 9:16 · center crop (fills the screen)",
    "original": "Original 16:9 (as streamed)",
}


SHORTS_MAX_SECONDS = 180  # YouTube Shorts limit

_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/System/Library/Fonts/Supplemental/Impact.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/impact.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]


class FFmpegError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def ffmpeg_exe() -> str:
    """System ffmpeg if installed, otherwise the static build shipped with imageio-ffmpeg."""
    found = os.environ.get("GTAVI_FFMPEG") or shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:  # pragma: no cover - only hit on broken installs
        raise FFmpegError(
            "ffmpeg not found. Install it (brew/apt/winget install ffmpeg) or `pip install imageio-ffmpeg`."
        ) from e


def run_ffmpeg(args: Sequence[str]) -> None:
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FFmpegError(proc.stderr.strip()[-2000:] or f"ffmpeg exited with {proc.returncode}")


@lru_cache(maxsize=1)
def caption_font() -> Optional[str]:
    env = os.environ.get("GTAVI_FONT")
    if env and Path(env).exists():
        return env
    return next((f for f in _FONT_CANDIDATES if Path(f).exists()), None)


def _ff_path(p: Path) -> str:
    """Path quoted for use inside an ffmpeg filter argument."""
    return "'" + str(p).replace("\\", "/").replace(":", "\\:").replace("'", "") + "'"


def video_filter(fmt: str, caption_file: Optional[Path] = None, lines: int = 1) -> Optional[str]:
    if fmt == "original":
        base = "[0:v]null"
        size = 64
    elif fmt == "vertical-blur":
        base = (
            "[0:v]split=2[bg][fg];"
            "[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:5[bgb];"
            "[fg]scale=1080:-2[fgs];"
            "[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1"
        )
        size = 84
    elif fmt == "vertical-crop":
        base = "[0:v]crop=ih*9/16:ih,scale=1080:1920,setsar=1"
        size = 84
    else:
        raise ValueError(f"unknown format {fmt!r}; choose from {', '.join(FORMATS)}")
    if caption_file is None:
        return None if fmt == "original" else base + "[v]"
    font = caption_font()
    font_arg = f"fontfile={_ff_path(Path(font))}:" if font else ""
    top = "h*0.10" if fmt.startswith("vertical") else "h*0.06"
    # one drawtext per line so every line is centered on its own
    draws = [
        f"drawtext={font_arg}textfile={_ff_path(caption_file.with_name(f'{caption_file.stem}{i}.txt'))}:"
        f"expansion=none:fontsize={size}:fontcolor=white:borderw=7:bordercolor=black:"
        f"x=(w-text_w)/2:y={top}+{i * int(size * 1.2)}"
        for i in range(lines)
    ]
    return f"{base},{','.join(draws)}[v]"


def clean_caption(text: str) -> str:
    """Drop emoji / symbols that regular fonts can't draw (they'd show up as boxes)."""
    kept = "".join(
        ch for ch in text
        if unicodedata.category(ch) not in ("So", "Sk", "Cs", "Co", "Cn", "Mn", "Cf") and ord(ch) < 0x2000
    )
    return " ".join(kept.split())


def write_caption_file(text: str, folder: Path, fmt: str) -> Optional[Path]:
    """Writes caption0.txt, caption1.txt... (one per wrapped line); returns caption.txt as the stem."""
    text = clean_caption(text)
    if not text:
        return None
    width = 18 if fmt.startswith("vertical") else 36
    lines = textwrap.wrap(text.upper(), width)[:4]
    for i, line in enumerate(lines):
        (folder / f"caption{i}.txt").write_text(line, encoding="utf-8")
    return folder / "caption.txt"


def concat_ts(segments: Iterable[Path], dest: Path) -> None:
    """MPEG-TS segments can be joined byte-for-byte, no re-mux needed."""
    with open(dest, "wb") as out:
        for seg in segments:
            with open(seg, "rb") as f:
                shutil.copyfileobj(f, out)


def cut(
    src: Path,
    dest: Path,
    start: float,
    duration: float,
    fmt: str = "original",
    fast: bool = False,
    caption: str = "",
) -> Path:
    """Cut `duration` seconds starting at `start` from `src` into an MP4.

    fast=True stream-copies (instant, but snaps to keyframes and can't reformat or caption).
    caption burns a big hook text ("WAIT FOR IT") into the top of the frame.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gtavi-") as tmp:
        cap = write_caption_file(caption, Path(tmp), fmt)
        return _cut(src, dest, start, duration, fmt, fast and cap is None, cap)


def _caption_lines(caption_file: Optional[Path]) -> int:
    if caption_file is None:
        return 0
    return len(list(caption_file.parent.glob("caption[0-9]*.txt")))


def _cut(src: Path, dest: Path, start: float, duration: float, fmt: str, fast: bool,
         caption_file: Optional[Path]) -> Path:
    args: List[str] = ["-ss", f"{max(start, 0):.3f}", "-i", str(src), "-t", f"{duration:.3f}"]
    vf = video_filter(fmt, caption_file, _caption_lines(caption_file))
    if fast and vf is None:
        args += ["-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", "-avoid_negative_ts", "make_zero"]
    else:
        if vf:
            args += ["-filter_complex", vf, "-map", "[v]"]
        else:
            args += ["-map", "0:v:0?"]
        args += [
            "-map", "0:a:0?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k",
        ]
    args += ["-movflags", "+faststart", str(dest)]
    run_ffmpeg(args)
    return dest


def clip_from_segments(
    segments: Sequence[Path],
    offset: float,
    duration: float,
    dest: Path,
    fmt: str = "original",
    fast: bool = False,
    caption: str = "",
) -> Path:
    """Join `segments`, then cut `duration`s starting `offset`s into the first one."""
    if not segments:
        raise ValueError("no buffered video to clip yet")
    with tempfile.TemporaryDirectory(prefix="gtavi-") as tmp:
        joined = Path(tmp) / "joined.ts"
        concat_ts(segments, joined)
        return cut(joined, dest, offset, duration, fmt=fmt, fast=fast, caption=caption)


def download_section(url: str, start: float, end: float, dest: Path, fmt: str = "original",
                     caption: str = "") -> Path:
    """Clip a VOD / YouTube video / past broadcast by time range using yt-dlp."""
    if end <= start:
        raise ValueError("end must be after start")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gtavi-") as tmp:
        raw = Path(tmp) / "section.%(ext)s"
        cmd = [
            sys.executable, "-m", "yt_dlp",
            "--quiet", "--no-warnings", "--no-playlist",
            "--ffmpeg-location", ffmpeg_exe(),
            "--download-sections", f"*{start:.2f}-{end:.2f}",
            "--force-keyframes-at-cuts",
            "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
            "--merge-output-format", "mp4",
            "-o", str(raw), url,
        ]
        from .sources import tool_env

        proc = subprocess.run(cmd, capture_output=True, text=True, env=tool_env())
        if proc.returncode != 0:
            raise FFmpegError(proc.stderr.strip()[-2000:] or "yt-dlp failed")
        got = next(Path(tmp).glob("section.*"), None)
        if got is None:
            raise FFmpegError("yt-dlp produced no file")
        if fmt == "original" and not caption:
            shutil.move(str(got), dest)
            return dest
        return cut(got, dest, 0, end - start, fmt=fmt, caption=caption)


def shorts_kit(clip: Path, source_title: str, credit_url: str, reason: str = "",
               tags: Sequence[str] = (), live: bool = True) -> Path:
    """Write <clip>.txt with a ready-to-paste YouTube Shorts title, description and hashtags."""
    title_base = re.sub(r"\s*\d{4}-\d{2}-\d{2} \d{2}:\d{2}$", "", source_title or "")  # yt-dlp live suffix
    title_base = (title_base or "Live moment").split("|")[0].split(" - ")[0].strip()
    title = f"{title_base[:70]} 😳 #shorts"
    hashtags = " ".join(dict.fromkeys(["#shorts", *(f"#{t.lstrip('#')}" for t in tags if t.strip())]))
    lines = [
        f"TITLE:\n{title}",
        "",
        "DESCRIPTION:",
        f"{reason.capitalize() + ' - ' if reason and reason != 'manual' else ''}"
        f"{'clipped live from' if live else 'Clipped from'}:",
        credit_url,
        "",
        hashtags,
    ]
    out = clip.with_suffix(".txt")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
